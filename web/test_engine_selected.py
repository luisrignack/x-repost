#!/usr/bin/env python3
"""Prueba offline de X_ONLY_URLS_FILE con un browser-use simulado.

Verifica que el panel puede aplicar exactamente el listado previsualizado sin
volver a buscar en X y sin abrir Chrome.
"""

from __future__ import annotations

import csv
import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / "xrepost.sh"

FAKE = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    import os, sys, time

    src = sys.stdin.read()
    log_path = os.environ["FAKE_LOG"]
    state = {"unretweet": False}

    def log(line):
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\\n")

    def goto_url(url):
        state["unretweet"] = False
        log("GOTO " + url)

    def wait_for_load():
        pass

    def js(expr):
        if expr == 'document.querySelectorAll(\\'article[data-testid="tweet"]\\').length':
            return 0
        if expr == 'document.querySelectorAll(\\'[data-testid="retweet"]\\').length':
            return 0 if state["unretweet"] else 1
        if expr == 'document.querySelectorAll(\\'[data-testid="unretweet"]\\').length':
            return 1 if state["unretweet"] else 0
        if expr == '!!document.querySelector(\\'[data-testid="retweetConfirm"]\\')':
            return True
        if expr == '!!document.querySelector(\\'[data-testid="unretweetConfirm"]\\')':
            return True
        if "retweetConfirm" in expr and ".click()" in expr:
            state["unretweet"] = True
            return None
        if "unretweetConfirm" in expr and ".click()" in expr:
            state["unretweet"] = False
            return None
        if '"retweet"' in expr and ".click()" in expr:
            return None
        raise AssertionError("js inesperado: " + expr)

    time.sleep = lambda *args, **kwargs: None
    ns = {
        "__name__": "__main__",
        "js": js,
        "goto_url": goto_url,
        "wait_for_load": wait_for_load,
    }
    try:
        exec(compile(src, "xrepost.py", "exec"), ns)
    except SystemExit as exc:
        raise SystemExit(exc.code)
    '''
)

POSTS = [
    {
        "fecha": "2024-01-01 10:00",
        "autor": "Autor Uno",
        "usuario": "@foo",
        "texto": "primer post seleccionado",
        "url": "https://x.com/foo/status/11",
        "estado": "solo listado",
    },
    {
        "fecha": "2024-01-02 11:30",
        "autor": "Autor Dos",
        "usuario": "@bar",
        "texto": "segundo post seleccionado",
        "url": "https://x.com/bar/status/22",
        "estado": "solo listado",
    },
]


class EngineSelectedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xrepost-engine-test-")
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "browser-use"
        fake.write_text(FAKE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.selected = self.root / "selected.json"
        self.selected.write_text(json.dumps(POSTS, ensure_ascii=False), encoding="utf-8")
        self.history = self.root / "history.jsonl"
        self.csv = self.root / "posts.csv"
        self.md = self.root / "posts.md"
        self.log = self.root / "browser.log"

    def tearDown(self):
        self.tmp.cleanup()

    def run_engine(self, limit=10):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}:{env.get('PATH', '')}",
                "FAKE_LOG": str(self.log),
                "X_ONLY_URLS_FILE": str(self.selected),
                "X_APPLY": "1",
                "X_USERS": "foo",
                "X_FROM": "2024-01-01",
                "X_TO": "2024-01-31",
                "X_HISTORY": str(self.history),
                "X_CSV": str(self.csv),
                "X_MD": str(self.md),
                "X_LIMIT": str(limit),
                "X_DELAY": "0",
                "NO_COLOR": "1",
            }
        )
        return subprocess.run(
            [str(SH), "--csv", str(self.csv), "--md", str(self.md)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_applies_exact_selected_urls_without_searching(self):
        result = self.run_engine()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("listado de la previsualización", result.stdout)

        visited = [
            line[5:]
            for line in self.log.read_text("utf-8").splitlines()
            if line.startswith("GOTO ")
        ]
        self.assertEqual(visited, [post["url"] for post in POSTS])
        self.assertFalse(any("search?q=" in url for url in visited))

        rows = list(csv.DictReader(self.csv.read_text("utf-8").splitlines()))
        self.assertEqual([row["url"] for row in rows], [post["url"] for post in POSTS])
        self.assertTrue(all(row["estado"] == "reposteado" for row in rows))
        self.assertIn("segundo post seleccionado", self.md.read_text("utf-8"))

        events = [
            json.loads(line)
            for line in self.history.read_text("utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual([e["id"] for e in events], ["11", "22"])
        self.assertTrue(all(e["action"] == "repost" and e["result"] == "reposted" for e in events))

    def test_ignores_non_x_urls(self):
        data = [POSTS[0], {"url": "https://evil.example/status/1", "texto": "no"}]
        self.selected.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_engine()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("URL no válida", result.stdout)
        visited = [
            line[5:]
            for line in self.log.read_text("utf-8").splitlines()
            if line.startswith("GOTO ")
        ]
        self.assertEqual(visited, [POSTS[0]["url"]])
        rows = list(csv.DictReader(self.csv.read_text("utf-8").splitlines()))
        self.assertEqual([row["url"] for row in rows], [POSTS[0]["url"]])

    def test_limit_caps_selected_list(self):
        result = self.run_engine(limit=1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        visited = [
            line[5:]
            for line in self.log.read_text("utf-8").splitlines()
            if line.startswith("GOTO ")
        ]
        self.assertEqual(visited, [POSTS[0]["url"]])
        self.assertIn("límite de 1 alcanzado", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
