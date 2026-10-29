#!/usr/bin/env python3
"""Pruebas del panel web con un motor simulado (sin red ni Chrome)."""

from __future__ import annotations

import csv
import http.client
import json
import os
import stat
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from app import App, create_server, main, read_history


STUB = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    import csv, datetime as dt, json, os, sys, time

    HISTORY = os.environ.get("X_HISTORY", "")
    LIMIT = int(os.environ.get("X_LIMIT", "10"))
    ONLY = os.environ.get("X_ONLY_URLS_FILE", "")
    SLEEP = float(os.environ.get("STUB_SLEEP", "0") or 0)

    def now():
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def url_id(url):
        return url.rstrip("/").rsplit("/", 1)[-1]

    def save(action, result, url):
        if not HISTORY:
            return
        os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
        rec = {
            "ts": now(),
            "run": "run-test",
            "action": action,
            "result": result,
            "id": url_id(url),
            "url": url,
            "author": "Autor",
            "handle": "foo",
            "posted_at": "2024-01-01T10:00:00Z",
        }
        with open(HISTORY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\\n")

    def write(path, rows):
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if path.endswith(".md"):
            lines = ["| Fecha | Autor | Texto | Enlace | Estado |", "|---|---|---|---|---|"]
            for r in rows:
                lines.append(
                    f"| {r['fecha']} | {r['autor']} {r['usuario']} | {r['texto']} | "
                    f"[post]({r['url']}) | {r['estado']} |"
                )
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\\n".join(lines) + "\\n")
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(
                fh, fieldnames=["fecha", "autor", "usuario", "texto", "url", "estado"]
            )
            w.writeheader()
            w.writerows(rows)

    def main():
        if os.environ.get("STUB_FAIL") == "1":
            print("fallo simulado del motor")
            return 3

        args = sys.argv[1:]
        if "--undo" in args:
            url = args[args.index("--undo") + 1]
            time.sleep(SLEEP)
            save("undo", "undone", url)
            print("deshecho:", url)
            return 0
        if "--undo-last" in args:
            n = 0
            idx = args.index("--undo-last")
            if idx + 1 < len(args) and args[idx + 1].isdigit():
                n = int(args[idx + 1])
            entries = []
            try:
                with open(HISTORY, encoding="utf-8") as fh:
                    entries = [json.loads(line) for line in fh if line.strip()]
            except OSError:
                pass
            done = {}
            for e in entries:
                if e.get("action") == "repost" and e.get("result") == "reposted":
                    done[e["id"]] = e
                elif e.get("action") == "undo" and e.get("result") == "undone":
                    done.pop(e.get("id"), None)
            targets = list(done.values())[-n:] if n else list(done.values())
            time.sleep(SLEEP)
            for e in targets:
                save("undo", "undone", e["url"])
                print("deshecho:", e["url"])
            return 0

        csv_path = ""
        md_path = ""
        if "--csv" in args:
            csv_path = args[args.index("--csv") + 1]
        if "--md" in args:
            md_path = args[args.index("--md") + 1]

        selected = []
        if ONLY:
            with open(ONLY, encoding="utf-8") as fh:
                selected = json.load(fh)
        rows = []
        for item in selected:
            rows.append(
                {
                    "fecha": item.get("fecha", "2024-01-01 10:00"),
                    "autor": item.get("autor", "Autor"),
                    "usuario": item.get("usuario", "@foo"),
                    "texto": item.get("texto", "texto"),
                    "url": item.get("url", ""),
                    "estado": "reposteado" if os.environ.get("X_APPLY") == "1" else "solo listado",
                }
            )
        if not ONLY:
            rows = [
                {
                    "fecha": "2024-01-01 10:00",
                    "autor": "Autor",
                    "usuario": "@foo",
                    "texto": "primer post",
                    "url": "https://x.com/foo/status/1",
                    "estado": "solo listado",
                },
                {
                    "fecha": "2024-01-02 11:30",
                    "autor": "Otra",
                    "usuario": "@bar",
                    "texto": "segundo post",
                    "url": "https://x.com/bar/status/2",
                    "estado": "solo listado",
                },
            ]

        if os.environ.get("X_APPLY") == "1":
            time.sleep(SLEEP)
            done = 0
            for row in rows:
                if done >= LIMIT:
                    break
                save("repost", "reposted", row["url"])
                done += 1
                print("reposteado:", row["url"])
                time.sleep(max(0.0, SLEEP / 4))
            write(csv_path, rows)
            write(md_path, rows)
            print("Total reposteado:", done)
            return 0

        time.sleep(SLEEP)
        write(csv_path, rows)
        write(md_path, rows)
        print("DRY-RUN:", len(rows), "posts")
        return 0

    sys.exit(main())
    '''
)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xrepost-web-test-")
        self.root = Path(self.tmp.name)
        self.engine = self.root / "engine.py"
        self.engine.write_text(STUB, encoding="utf-8")
        self.engine.chmod(self.engine.stat().st_mode | stat.S_IXUSR)
        self.history = self.root / "history.jsonl"
        self.runs = self.root / "runs"
        self.token = "token-de-prueba"
        self.app = App(
            engine=self.engine,
            history=self.history,
            runs=self.runs,
            token=self.token,
        )
        self.httpd = create_server(self.app, 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.saved_env = {}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def set_env(self, key, value):
        if key not in self.saved_env:
            self.saved_env[key] = os.environ.get(key)
        os.environ[key] = value

    def request(self, method, path, body=None, *, token=True, headers=None, origin=None):
        hdrs = dict(headers or {})
        if token:
            hdrs["X-Token"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if origin:
            hdrs["Origin"] = origin
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, dict(resp.getheaders()), payload
        finally:
            conn.close()

    def json_request(self, method, path, body=None, **kwargs):
        status, headers, payload = self.request(method, path, body, **kwargs)
        data = json.loads(payload.decode("utf-8")) if payload else None
        return status, headers, data

    def wait_job(self, job_id, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, job = self.json_request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200, job)
            if job["status"] != "running":
                return job
            time.sleep(0.05)
        self.fail(f"la ejecución {job_id} no terminó a tiempo")

    def params(self, **overrides):
        data = {
            "users": "@foo,bar",
            "from": "2024-01-01",
            "to": "2024-01-31",
            "scope": "originals",
            "limit": 10,
            "delay": 20,
            "match": "",
            "exclude": "",
        }
        data.update(overrides)
        return data

    def preview(self, **overrides):
        status, _, data = self.json_request("POST", "/api/preview", self.params(**overrides))
        self.assertEqual(status, 202, data)
        return self.wait_job(data["id"])

    def apply(self, preview, **overrides):
        body = self.params(**overrides)
        body.update({"preview_id": preview["id"], "confirm": True})
        status, _, data = self.json_request("POST", "/api/apply", body)
        self.assertEqual(status, 202, data)
        return self.wait_job(data["id"])

    # --- autenticación y seguridad -------------------------------------

    def test_main_refuses_non_loopback_host(self):
        self.assertEqual(main(["--host", "0.0.0.0", "--no-open"]), 2)

    def test_health_requires_token(self):
        status, _, data = self.json_request("GET", "/api/health", token=False)
        self.assertEqual(status, 401)
        self.assertIn("error", data)

        status, _, health = self.json_request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertTrue(health["engine_ok"])
        self.assertEqual(health["history"], str(self.history))
        self.assertFalse(health["busy"])

    def test_index_token_sets_cookie_and_serves_panel(self):
        status, headers, _ = self.request("GET", "/?token=malo", token=False)
        self.assertEqual(status, 401)

        status, headers, payload = self.request("GET", f"/?token={self.token}", token=False)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/")
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("xrepost_token=", cookie)
        self.assertIn("HttpOnly", cookie)

        status, headers, page = self.request(
            "GET", "/", token=False, headers={"Cookie": f"xrepost_token={self.token}"}
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("xrepost", page.decode("utf-8"))

    def test_static_files_need_token_and_block_traversal(self):
        status, _, _ = self.request("GET", "/static/app.js", token=False)
        self.assertEqual(status, 401)

        status, headers, payload = self.request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn(b"use strict", payload)

        status, _, _ = self.request("GET", "/static/%2e%2e/app.py")
        self.assertEqual(status, 404)

    def test_host_origin_and_content_type_checks(self):
        status, _, _ = self.request(
            "GET", "/api/health", headers={"Host": "evil.example"}
        )
        self.assertEqual(status, 403)

        status, _, _ = self.request(
            "POST", "/api/preview", self.params(), origin="http://evil.example"
        )
        self.assertEqual(status, 403)

        status, _, _ = self.request(
            "POST",
            "/api/preview",
            token=False,
            headers={"Content-Type": "text/plain", "X-Token": self.token},
        )
        self.assertEqual(status, 415)

    # --- previsualización y exportaciones ------------------------------

    def test_preview_generates_posts_and_exports(self):
        job = self.preview()
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["kind"], "preview")
        self.assertEqual(len(job["posts"]), 2)
        self.assertEqual(job["posts"][0]["url"], "https://x.com/foo/status/1")
        self.assertNotIn("key", job["params"])
        self.assertTrue(job["exports"]["csv"])
        self.assertTrue(job["exports"]["md"])

        status, headers, csv_body = self.request("GET", f"/api/jobs/{job['id']}/export/csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        rows = list(csv.DictReader(csv_body.decode("utf-8").splitlines()))
        self.assertEqual([row["url"] for row in rows], [p["url"] for p in job["posts"]])

        status, _, md_body = self.request("GET", f"/api/jobs/{job['id']}/export/md")
        self.assertEqual(status, 200)
        self.assertIn(b"| Fecha |", md_body)
        self.assertFalse(self.history.exists())

    def test_preview_validation_errors(self):
        cases = [
            {"users": ""},
            {"users": "mal-handle"},
            {"users": ",".join(f"u{i}" for i in range(11))},
            {"from": "01/02/2024"},
            {"from": "2024-02-30"},
            {"from": "2024-03-01", "to": "2024-02-01"},
            {"scope": "otro"},
            {"limit": 0},
            {"limit": 51},
            {"delay": -1},
            {"match": "x" * 201},
        ]
        for case in cases:
            with self.subTest(case=case):
                status, _, data = self.json_request(
                    "POST", "/api/preview", self.params(**case)
                )
                self.assertEqual(status, 400, data)
                self.assertTrue(data["error"])

    def test_accounts_endpoint_requires_token_and_suggests_used(self):
        status, _, _ = self.request("GET", "/api/accounts?q=ete", token=False)
        self.assertEqual(status, 401)

        self.app.record_accounts("@ETECSA_Cuba, ana, luis")
        self.assertTrue(self.app.accounts.exists())
        self.assertEqual(self.app.accounts.parent, self.history.parent)

        status, _, data = self.json_request("GET", "/api/accounts?q=ete")
        self.assertEqual(status, 200)
        self.assertEqual([item["handle"] for item in data["accounts"]], ["ETECSA_Cuba"])

        status, _, data = self.json_request("GET", "/api/accounts?q=et&exclude=ETECSA_Cuba")
        self.assertEqual(status, 200)
        self.assertEqual(data["accounts"], [])

        status, _, data = self.json_request("GET", "/api/accounts?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["accounts"]), 2)

    def test_preview_records_accounts_for_autocomplete(self):
        self.preview(users="@nueva_cuenta, otra_cuenta")
        status, _, data = self.json_request("GET", "/api/accounts?q=nueva")
        self.assertEqual(status, 200)
        self.assertEqual([item["handle"] for item in data["accounts"]], ["nueva_cuenta"])

        status, _, data = self.json_request("GET", "/api/accounts?q=otra")
        self.assertEqual([item["handle"] for item in data["accounts"]], ["otra_cuenta"])

    def test_unknown_job_and_export(self):
        status, _, _ = self.request("GET", "/api/jobs/deadbeef")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/api/jobs/deadbeef/export/csv")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/api/nope")
        self.assertEqual(status, 404)

    # --- aplicar -------------------------------------------------------

    def test_apply_requires_confirmation_preview_and_minimum_delay(self):
        preview = self.preview()

        body = self.params()
        body["preview_id"] = preview["id"]
        status, _, data = self.json_request("POST", "/api/apply", body)
        self.assertEqual(status, 400)
        self.assertIn("confirmación", data["error"])

        body["confirm"] = True
        body["delay"] = 1
        status, _, _ = self.json_request("POST", "/api/apply", body)
        self.assertEqual(status, 400)

        body["delay"] = 20
        body["preview_id"] = "nope"
        status, _, data = self.json_request("POST", "/api/apply", body)
        self.assertEqual(status, 400)
        self.assertIn("previsualizar", data["error"])

        body["preview_id"] = preview["id"]
        body["users"] = "@otra"
        status, _, data = self.json_request("POST", "/api/apply", body)
        self.assertEqual(status, 400)
        self.assertIn("cambiaron", data["error"])

    def test_apply_uses_exact_preview_list_and_records_history(self):
        preview = self.preview()
        job = self.apply(preview)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["kind"], "apply")
        self.assertEqual(
            [p["url"] for p in job["posts"]], [p["url"] for p in preview["posts"]]
        )
        self.assertTrue(all(p["estado"] == "reposteado" for p in job["posts"]))
        self.assertNotIn("only_urls_file", job["params"])
        self.assertTrue((preview["posts"][0]["url"]).startswith("https://x.com/"))

        selected = self.runs / preview["id"] / "selected.json"
        self.assertTrue(selected.exists())
        self.assertEqual(
            [item["url"] for item in json.loads(selected.read_text("utf-8"))],
            [p["url"] for p in preview["posts"]],
        )

        lines = [json.loads(line) for line in self.history.read_text("utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(e["action"] == "repost" and e["result"] == "reposted" for e in lines))

        status, _, history = self.json_request("GET", "/api/history?n=10")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["entries"]), 2)
        self.assertTrue(all(entry["undoable"] for entry in history["entries"]))
        self.assertEqual(history["entries"][0]["result_label"], "reposteado")

    def test_apply_records_stub_failure(self):
        preview = self.preview()
        self.set_env("STUB_FAIL", "1")
        body = self.params()
        body.update({"preview_id": preview["id"], "confirm": True})
        status, _, data = self.json_request("POST", "/api/apply", body)
        self.assertEqual(status, 202, data)
        job = self.wait_job(data["id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("código 3", job["error"])
        self.assertFalse(self.history.exists())

    # --- una ejecución a la vez y cancelación --------------------------

    def test_only_one_job_and_cancel(self):
        self.set_env("STUB_SLEEP", "3")
        status, _, data = self.json_request("POST", "/api/preview", self.params())
        self.assertEqual(status, 202)
        first = data["id"]

        deadline = time.time() + 3
        while time.time() < deadline:
            current = self.app.current
            if current and current.id == first and current.proc is not None:
                break
            time.sleep(0.05)
        else:
            self.fail("el motor simulado no llegó a arrancar")

        status, _, data = self.json_request("POST", "/api/preview", self.params())
        self.assertEqual(status, 409)
        self.assertIn("en curso", data["error"])

        status, _, data = self.json_request("POST", "/api/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["cancelled"])
        job = self.wait_job(first)
        self.assertEqual(job["status"], "cancelled")

        os.environ.pop("STUB_SLEEP", None)
        self.saved_env.pop("STUB_SLEEP", None)
        job = self.preview()
        self.assertEqual(job["status"], "done")

    # --- deshacer e historial ------------------------------------------

    def test_undo_single_and_last(self):
        preview = self.preview()
        self.apply(preview)

        status, _, data = self.json_request(
            "POST", "/api/undo", {"mode": "url", "url": "https://evil.example/status/1"}
        )
        self.assertEqual(status, 400)

        status, _, data = self.json_request(
            "POST", "/api/undo", {"mode": "url", "url": "https://x.com/foo/status/1"}
        )
        self.assertEqual(status, 202)
        job = self.wait_job(data["id"])
        self.assertEqual(job["status"], "done")

        _, _, history = self.json_request("GET", "/api/history?n=10")
        by_id = {entry["id"]: entry for entry in history["entries"]}
        self.assertEqual(by_id["1"]["action"], "undo")
        self.assertFalse(by_id["1"]["undoable"])
        self.assertTrue(by_id["2"]["undoable"])

        status, _, data = self.json_request("POST", "/api/undo", {"mode": "last"})
        self.assertEqual(status, 202)
        self.wait_job(data["id"])
        _, _, history = self.json_request("GET", "/api/history?n=10")
        by_id = {entry["id"]: entry for entry in history["entries"]}
        self.assertFalse(by_id["2"]["undoable"])
        self.assertEqual(by_id["2"]["result_label"], "deshecho")

        for bad in ({"mode": "otro"}, {"mode": "last", "n": 51}, {"mode": "last", "n": "x"}):
            status, _, _ = self.json_request("POST", "/api/undo", bad)
            self.assertEqual(status, 400)

    def test_history_keeps_only_latest_state_per_post(self):
        path = self.root / "manual-history.jsonl"
        events = [
            {"id": "1", "ts": "2024-01-01T10:00:00Z", "action": "repost", "result": "reposted", "url": "https://x.com/a/status/1"},
            {"id": "2", "ts": "2024-01-01T10:01:00Z", "action": "repost", "result": "reposted", "url": "https://x.com/b/status/2"},
            {"id": "1", "ts": "2024-01-01T10:02:00Z", "action": "undo", "result": "undone", "url": "https://x.com/a/status/1"},
        ]
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        entries = read_history(path, 10)
        self.assertEqual([e["id"] for e in entries], ["1", "2"])
        self.assertEqual(entries[0]["action"], "undo")
        self.assertFalse(entries[0]["undoable"])
        self.assertTrue(entries[1]["undoable"])
        self.assertEqual(entries[0]["result_label"], "deshecho")


if __name__ == "__main__":
    unittest.main(verbosity=2)
