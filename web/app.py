#!/usr/bin/env python3
"""
xrepost web — panel local para el motor xrepost.

No sustituye al CLI: lo invoca como subproceso. El servidor escucha solo en
127.0.0.1, exige un token y permite una ejecución a la vez (Chrome es una sola
sesión). Pensado para uso personal, sin exponerlo a la red.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hmac
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

WEB_DIR = Path(__file__).resolve().parent
ROOT = WEB_DIR.parent
STATIC_DIR = WEB_DIR / "static"
DEFAULT_ENGINE = ROOT / "xrepost.sh"
DEFAULT_RUNS = WEB_DIR / ".runs"

# El motor y el archivo de cuentas viven un nivel arriba; app.py se ejecuta
# desde web/, así que su carpeta no está en sys.path por defecto.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import xrepost_accounts  # noqa: E402  (depende del sys.path anterior)

MAX_BODY = 1_000_000
MAX_LOG = 2_000
MAX_JOB_SECONDS = 30 * 60

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
STATUS_RE = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/(?:[A-Za-z0-9_]+|i)/status/\d+/?$"
)

LABELS = {
    "reposted": "reposteado",
    "already": "ya estaba reposteado",
    "no_button": "sin botón de repost",
    "no_menu": "menú no abierto",
    "unconfirmed": "no confirmado",
    "undone": "deshecho",
    "not_reposted": "no estaba reposteado",
    "undo_failed": "no se pudo deshacer",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def default_history() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return Path(os.environ.get("X_HISTORY") or (base / "xrepost" / "history.jsonl")).expanduser()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def fmt_seconds(value: float) -> str:
    value = max(0, int(value))
    if value < 60:
        return f"{value}s"
    return f"{value // 60}m {value % 60:02d}s"


def chrome_ok() -> bool:
    base = os.environ.get("BU_CDP_URL", "http://127.0.0.1:9222").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/json/version", timeout=0.8) as resp:
            return resp.status == 200
    except Exception:
        return False


def normalize_users(users: str) -> str:
    handles = []
    for raw in (users or "").split(","):
        handle = raw.strip().lstrip("@")
        if handle:
            handles.append(handle)
    return ",".join(handles)


def validate_repost_params(data: dict, *, apply: bool) -> dict:
    users = normalize_users(str(data.get("users", "")))
    frm = str(data.get("from", "")).strip()
    to = str(data.get("to", "")).strip() or frm
    scope = str(data.get("scope", "originals"))
    match = str(data.get("match", "")).strip()
    exclude = str(data.get("exclude", "")).strip()

    if not users:
        raise HttpError(400, "Indica al menos una cuenta.")
    if len(users.split(",")) > 10:
        raise HttpError(400, "Como máximo se pueden indicar 10 cuentas.")
    for handle in users.split(","):
        if not HANDLE_RE.fullmatch(handle):
            raise HttpError(400, f"Cuenta no válida: @{handle}. Usa solo letras, números y guion bajo.")
    if not DATE_RE.fullmatch(frm) or not DATE_RE.fullmatch(to):
        raise HttpError(400, "Las fechas deben tener el formato AAAA-MM-DD.")
    try:
        d_from = dt.date.fromisoformat(frm)
        d_to = dt.date.fromisoformat(to)
    except ValueError:
        raise HttpError(400, "Alguna de las fechas no existe en el calendario.")
    if d_from.year < 2006 or d_to.year > 2100:
        raise HttpError(400, "El rango de fechas queda fuera de lo razonable (2006–2100).")
    if d_from > d_to:
        raise HttpError(400, "La fecha inicial no puede ser posterior a la final.")
    if scope not in ("originals", "all"):
        raise HttpError(400, "Alcance no válido.")
    if len(match) > 200 or len(exclude) > 200:
        raise HttpError(400, "Los filtros de texto son demasiado largos.")

    try:
        limit = int(data.get("limit", 10))
    except (TypeError, ValueError):
        raise HttpError(400, "El límite no es un número.")
    try:
        delay = float(data.get("delay", 20))
    except (TypeError, ValueError):
        raise HttpError(400, "La pausa no es un número.")

    if not 1 <= limit <= 50:
        raise HttpError(400, "El límite debe estar entre 1 y 50.")
    if not 0 <= delay <= 300:
        raise HttpError(400, "La pausa debe estar entre 0 y 300 segundos.")
    if apply and delay < 5:
        raise HttpError(400, "Por seguridad, la pausa mínima al repostear es de 5 segundos.")
    if apply and data.get("confirm") is not True:
        raise HttpError(400, "Falta la confirmación explícita para repostear.")

    return {
        "users": users,
        "from": frm,
        "to": to,
        "scope": scope,
        "match": match,
        "exclude": exclude,
        "limit": limit,
        "delay": delay,
    }


def params_key(params: dict) -> str:
    return json.dumps(
        {
            "users": params["users"].lower(),
            "from": params["from"],
            "to": params["to"],
            "scope": params["scope"],
            "match": params["match"],
            "exclude": params["exclude"],
        },
        sort_keys=True,
    )


def read_history(path: Path, limit: int = 30) -> list[dict]:
    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("id"):
                    entries.append(item)
    except FileNotFoundError:
        return []
    except OSError:
        return []

    # Un post puede tener varios eventos (repost y undo). Mostramos solo el
    # último estado de cada uno, conservando el orden de la última aparición.
    latest: dict[str, dict] = {}
    for item in entries:
        latest.pop(item["id"], None)
        latest[item["id"]] = item

    out = []
    for item in reversed(list(latest.values())[-max(1, limit):]):
        result = item.get("result", "")
        undoable = item.get("action") == "repost" and result == "reposted"
        out.append(
            {
                "ts": item.get("ts", ""),
                "action": item.get("action", ""),
                "result": result,
                "result_label": LABELS.get(result, result),
                "undoable": undoable,
                "id": item.get("id", ""),
                "url": item.get("url", ""),
                "handle": item.get("handle", ""),
                "author": item.get("author", ""),
                "posted_at": item.get("posted_at", ""),
            }
        )
    return out


class Job:
    def __init__(self, kind: str, params: dict, runs: Path):
        self.id = secrets.token_hex(8)
        self.kind = kind
        self.params = params
        self.status = "running"
        self.started = time.time()
        self.ended: float | None = None
        self.returncode: int | None = None
        self.cancelled = False
        self.error = ""
        self.lines: list[str] = []
        self.posts: list[dict] = []
        self.dir = runs / self.id
        self.csv_path = self.dir / "posts.csv"
        self.md_path = self.dir / "posts.md"
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def add(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG:
                self.lines = self.lines[-MAX_LOG:]

    def snapshot(self, tail: int = 500) -> dict:
        with self.lock:
            lines = self.lines[-tail:]
            posts = list(self.posts)
        elapsed = (self.ended or time.time()) - self.started
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "elapsed": round(elapsed, 1),
            "elapsed_label": fmt_seconds(elapsed),
            "returncode": self.returncode,
            "cancelled": self.cancelled,
            "error": self.error,
            "lines": lines,
            "posts": posts,
            "exports": {
                "csv": self.csv_path.exists() and bool(self.posts),
                "md": self.md_path.exists() and bool(self.posts),
            },
            "params": {
                key: value
                for key, value in self.params.items()
                if key not in ("confirm", "key", "preview_id", "only_urls_file")
            },
        }


class App:
    def __init__(
        self,
        *,
        engine: Path,
        history: Path,
        runs: Path,
        token: str,
        accounts: Path | None = None,
        verbose: bool = False,
    ):
        self.engine = Path(engine).expanduser().resolve()
        self.history = Path(history).expanduser()
        self.runs = Path(runs).expanduser()
        self.runs.mkdir(parents=True, exist_ok=True)
        self.accounts, _ = xrepost_accounts.resolve_paths(accounts, self.history)
        self.token = token
        self.verbose = verbose
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.lock = threading.Lock()

    def suggest_accounts(
        self,
        query: str = "",
        exclude: set[str] | list[str] | tuple[str, ...] = (),
        limit: int = 8,
    ) -> list[dict]:
        return xrepost_accounts.suggest(
            query,
            accounts=self.accounts,
            history=self.history,
            limit=max(1, min(int(limit), 20)),
            exclude=exclude,
        )

    def record_accounts(self, users: str) -> None:
        """Recuerda las cuentas usadas para el autocompletado (sin romper la búsqueda)."""
        try:
            xrepost_accounts.add_accounts(users, self.accounts)
        except OSError:
            pass

    def start_job(self, kind: str, params: dict) -> Job:
        with self.lock:
            if self.current is not None and self.current.status == "running":
                raise HttpError(409, "Ya hay una ejecución en curso. Espera o cancélala.")
            job = Job(kind, params, self.runs)
            self.jobs[job.id] = job
            self.current = job
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return job

    def cancel(self) -> bool:
        with self.lock:
            job = self.current
        if not job or job.status != "running":
            return False
        job.cancelled = True
        self._terminate(job)
        return True

    def shutdown(self) -> None:
        """Al cerrar el panel, no dejar el motor (ni Chrome) trabajando solo."""
        with self.lock:
            job = self.current
        if not job or job.status != "running":
            return
        job.cancelled = True
        self._terminate(job)
        proc = job.proc
        if proc is None:
            return
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    def _command(self, job: Job) -> list[str]:
        if job.kind == "undo":
            if job.params.get("mode") == "url":
                return [str(self.engine), "--undo", job.params["url"]]
            n = int(job.params.get("n") or 0)
            return [str(self.engine), "--undo-last"] + ([str(n)] if n else [])

        p = job.params
        return [
            str(self.engine),
            "--csv",
            str(job.csv_path),
            "--md",
            str(job.md_path),
            p["users"],
            p["from"],
            p["to"],
        ]

    def _environment(self, job: Job) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "X_HISTORY": str(self.history),
                "X_ACCOUNTS": str(self.accounts),
                "X_MODE": "repost",
                "X_FORCE": "0",
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
                "NO_COLOR": "1",
                "TERM": "dumb",
            }
        )
        if job.kind == "undo":
            env["X_DELAY"] = "20"
            return env

        p = job.params
        env.update(
            {
                "X_USERS": p["users"],
                "X_FROM": p["from"],
                "X_TO": p["to"],
                "X_SKIP_RETWEETS": "0" if p["scope"] == "all" else "1",
                "X_APPLY": "1" if job.kind == "apply" else "0",
                "X_LIMIT": str(p["limit"]),
                "X_DELAY": str(p["delay"]),
                "X_MATCH": p["match"],
                "X_EXCLUDE": p["exclude"],
                "X_CSV": str(job.csv_path),
                "X_MD": str(job.md_path),
            }
        )
        if job.kind == "apply" and p.get("only_urls_file"):
            # Aplicar exactamente lo que el usuario revisó, sin volver a buscar.
            env["X_ONLY_URLS_FILE"] = str(p["only_urls_file"])
        return env

    def _run_job(self, job: Job) -> None:
        job.dir.mkdir(parents=True, exist_ok=True)
        try:
            if not self.engine.exists():
                raise FileNotFoundError(f"No existe el motor: {self.engine}")
            cmd = self._command(job)
            env = self._environment(job)
            job.add("$ " + " ".join(shlex.quote(part) for part in cmd))
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            job.proc = proc
            if job.cancelled:
                # Se pulsó Cancelar antes de que el proceso llegara a arrancar.
                self._terminate(job)
            threading.Thread(target=self._watchdog, args=(job,), daemon=True).start()

            assert proc.stdout is not None
            for raw in proc.stdout:
                line = ANSI_RE.sub("", raw.rstrip("\n")).replace("\r", "")
                job.add(line)
            rc = proc.wait()
            job.returncode = rc

            if job.kind in ("preview", "apply"):
                self._load_posts(job)

            if job.cancelled:
                job.status = "cancelled"
            elif rc == 0:
                job.status = "done"
            else:
                job.status = "error"
                job.error = f"El motor terminó con código {rc}."
        except FileNotFoundError as exc:
            job.status = "error"
            job.error = str(exc)
            job.add(f"Error: {exc}")
        except Exception as exc:  # pragma: no cover - salvaguarda
            job.status = "error"
            job.error = f"Fallo inesperado: {exc}"
            job.add(job.error)
        finally:
            job.ended = time.time()
            if job.status == "running":
                job.status = "error"
                job.error = job.error or "La ejecución no terminó correctamente."
            if job.proc and job.proc.stdout:
                try:
                    job.proc.stdout.close()
                except Exception:
                    pass

    def _load_posts(self, job: Job) -> None:
        if not job.csv_path.exists():
            return
        try:
            with open(job.csv_path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                job.posts = [dict(row) for row in reader if row.get("url")]
        except Exception as exc:
            job.add(f"Aviso: no se pudo leer el CSV ({exc}).")

    def _watchdog(self, job: Job) -> None:
        while job.status == "running":
            if time.time() - job.started > MAX_JOB_SECONDS:
                job.add("Tiempo máximo alcanzado; cancelando la ejecución.")
                job.cancelled = True
                self._terminate(job)
                return
            time.sleep(1)

    @staticmethod
    def _terminate(job: Job) -> None:
        proc = job.proc
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
        threading.Thread(target=App._hard_kill, args=(proc,), daemon=True).start()

    @staticmethod
    def _hard_kill(proc: subprocess.Popen, wait: float = 3.0) -> None:
        time.sleep(wait)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "xrepost-web/1.0"
    application: App

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if self.application.verbose:
            super().log_message(fmt, *args)

    # --- utilidades HTTP ---------------------------------------------------

    def _host_ok(self) -> bool:
        host = urlparse("//" + (self.headers.get("Host") or "")).hostname
        return host in {"127.0.0.1", "localhost", "::1"}

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urlparse(origin).hostname
        return host in {"127.0.0.1", "localhost", "::1"}

    def _token_ok(self) -> bool:
        header = self.headers.get("X-Token", "")
        if header and hmac.compare_digest(header, self.application.token):
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("xrepost_token")
        return bool(morsel and hmac.compare_digest(morsel.value, self.application.token))

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        *,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str, *, download: str = "") -> None:
        try:
            body = path.read_bytes()
        except OSError:
            raise HttpError(404, "Archivo no encontrado.")
        extra = {"Content-Disposition": f'attachment; filename="{download}"'} if download else None
        self._send_bytes(body, content_type, extra=extra)

    def _error_page(self, status: int, title: str, detail: str) -> None:
        body = f"""<!doctype html><html lang="es"><meta charset="utf-8">
<title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;background:#0b1020;color:#e8edf7;
display:grid;place-items:center;min-height:100vh;margin:0;padding:24px}}
main{{max-width:560px;background:#111832;border:1px solid #24304f;border-radius:16px;padding:28px}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:#93a0bd;margin:0}}</style>
<main><h1>{title}</h1><p>{detail}</p></main></html>"""
        self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8", status)

    def _read_json(self) -> dict:
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            raise HttpError(415, "Se esperaba application/json.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "Longitud no válida.")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise HttpError(413, "Petición demasiado grande.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "JSON no válido.")
        if not isinstance(data, dict):
            raise HttpError(400, "El cuerpo debe ser un objeto JSON.")
        return data

    # --- rutas -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            if not self._host_ok():
                raise HttpError(403, "Host no permitido.")
            path = urlparse(self.path).path
            if path == "/":
                return self._index()
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self._security_headers()
                self.end_headers()
                return
            if path.startswith("/static/"):
                if not self._token_ok():
                    raise HttpError(401, "No autorizado.")
                return self._static(path[len("/static/"):])
            if path.startswith("/api/"):
                if not self._token_ok():
                    raise HttpError(401, "No autorizado.")
                return self._api_get(path)
            raise HttpError(404, "Ruta no encontrada.")
        except HttpError as exc:
            if exc.status == 401 and not self.path.startswith("/api/"):
                self._error_page(401, "Panel bloqueado", "Abre el enlace con token que se mostró en la terminal.")
            else:
                self._send_json({"error": exc.message}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - salvaguarda
            self._send_json({"error": f"Error interno: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self._host_ok():
                raise HttpError(403, "Host no permitido.")
            if not self._origin_ok():
                raise HttpError(403, "Origen no permitido.")
            if not self._token_ok():
                raise HttpError(401, "No autorizado.")
            path = urlparse(self.path).path
            data = self._read_json()
            if path == "/api/preview":
                return self._api_preview(data)
            if path == "/api/apply":
                return self._api_apply(data)
            if path == "/api/undo":
                return self._api_undo(data)
            if path == "/api/cancel":
                return self._api_cancel()
            raise HttpError(404, "Ruta no encontrada.")
        except HttpError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - salvaguarda
            self._send_json({"error": f"Error interno: {exc}"}, 500)

    def _index(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        token = (query.get("token") or [""])[0]
        if token:
            if not hmac.compare_digest(token, self.application.token):
                raise HttpError(401, "Enlace no válido.")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header(
                "Set-Cookie",
                f"xrepost_token={self.application.token}; Path=/; HttpOnly; SameSite=Strict",
            )
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self._security_headers()
            self.end_headers()
            return
        if not self._token_ok():
            raise HttpError(401, "No autorizado.")
        self._serve_static("index.html", "text/html; charset=utf-8")

    def _static(self, name: str) -> None:
        safe = Path(unquote(name))
        if safe.is_absolute() or ".." in safe.parts:
            raise HttpError(404, "Ruta no encontrada.")
        content_type = mimetypes.guess_type(str(safe))[0] or "application/octet-stream"
        self._serve_static(str(safe), content_type)

    def _serve_static(self, name: str, content_type: str) -> None:
        path = (STATIC_DIR / name).resolve()
        if not path.is_relative_to(STATIC_DIR.resolve()) or not path.is_file():
            raise HttpError(404, "Archivo no encontrado.")
        body = path.read_bytes()
        csp = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        self._send_bytes(
            body,
            content_type,
            extra={"Content-Security-Policy": csp} if content_type.startswith("text/html") else None,
        )

    def _api_get(self, path: str) -> None:
        if path == "/api/health":
            return self._send_json(
                {
                    "ok": True,
                    "engine": str(self.application.engine),
                    "engine_ok": self.application.engine.exists(),
                    "chrome": chrome_ok(),
                    "history": str(self.application.history),
                    "busy": bool(self.application.current and self.application.current.status == "running"),
                }
            )
        if path == "/api/history":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int((query.get("n") or ["30"])[0])
            except ValueError:
                limit = 30
            limit = max(1, min(limit, 200))
            return self._send_json({"entries": read_history(self.application.history, limit)})

        if path == "/api/accounts":
            query = parse_qs(urlparse(self.path).query)
            text = (query.get("q") or [""])[0].strip()
            try:
                limit = int((query.get("limit") or ["8"])[0])
            except ValueError:
                limit = 8
            exclude = (query.get("exclude") or [""])[0]
            items = self.application.suggest_accounts(
                text,
                exclude=xrepost_accounts.split_handles(exclude),
                limit=limit,
            )
            return self._send_json({"accounts": items})

        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)
        if match:
            job = self.application.jobs.get(match.group(1))
            if not job:
                raise HttpError(404, "Ejecución no encontrada.")
            return self._send_json(job.snapshot())

        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)/export/(csv|md)", path)
        if match:
            job = self.application.jobs.get(match.group(1))
            if not job:
                raise HttpError(404, "Ejecución no encontrada.")
            fmt = match.group(2)
            path_file = job.csv_path if fmt == "csv" else job.md_path
            if not path_file.exists():
                raise HttpError(404, "Esa ejecución no generó exportación.")
            ctype = "text/csv; charset=utf-8" if fmt == "csv" else "text/markdown; charset=utf-8"
            return self._send_file(
                path_file,
                ctype,
                download=f"xrepost-{job.id}.{fmt}",
            )

        raise HttpError(404, "Ruta no encontrada.")

    def _api_preview(self, data: dict) -> None:
        params = validate_repost_params(data, apply=False)
        params["key"] = params_key(params)
        # Las cuentas quedan recordadas en cuanto se usan en una búsqueda,
        # igual que en el CLI.
        self.application.record_accounts(params["users"])
        job = self.application.start_job("preview", params)
        self._send_json({"id": job.id}, 202)

    def _api_apply(self, data: dict) -> None:
        params = validate_repost_params(data, apply=True)
        preview_id = str(data.get("preview_id", ""))
        preview = self.application.jobs.get(preview_id)
        if not preview or preview.kind != "preview":
            raise HttpError(400, "Primero debes previsualizar el listado.")
        if preview.status != "done":
            raise HttpError(400, "La previsualización no ha terminado correctamente.")
        if preview.params.get("key") != params_key(params):
            raise HttpError(400, "Los datos cambiaron desde la previsualización. Vuelve a previsualizar.")
        if not preview.posts:
            raise HttpError(400, "La previsualización no tiene publicaciones para repostear.")

        preview.dir.mkdir(parents=True, exist_ok=True)
        selected = preview.dir / "selected.json"
        selected.write_text(
            json.dumps(preview.posts, ensure_ascii=False),
            encoding="utf-8",
        )
        params["only_urls_file"] = str(selected)
        job = self.application.start_job("apply", params)
        job.params["preview_id"] = preview_id
        self._send_json({"id": job.id}, 202)

    def _api_undo(self, data: dict) -> None:
        mode = str(data.get("mode", ""))
        if mode == "url":
            url = str(data.get("url", "")).strip()
            if not STATUS_RE.fullmatch(url):
                raise HttpError(400, "URL de post no válida.")
            params = {"mode": "url", "url": url}
        elif mode == "last":
            try:
                n = int(data.get("n") or 0)
            except (TypeError, ValueError):
                raise HttpError(400, "Número de reposts no válido.")
            if not 0 <= n <= 50:
                raise HttpError(400, "El número debe estar entre 0 y 50.")
            params = {"mode": "last", "n": n}
        else:
            raise HttpError(400, "Modo de deshacer no válido.")

        job = self.application.start_job("undo", params)
        self._send_json({"id": job.id}, 202)

    def _api_cancel(self) -> None:
        cancelled = self.application.cancel()
        self._send_json({"ok": True, "cancelled": cancelled})


def make_handler(app: App):
    class BoundHandler(Handler):
        application = app

    return BoundHandler


def create_server(app: App, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd


def open_panel(url: str) -> None:
    """Abre el panel en una instancia de Chrome aparte (no la de CDP)."""
    profile = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "xrepost-web" / "panel-profile"
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError:
        profile = Path.home() / ".xrepost-web-panel"

    for name in ("google-chrome", "chromium", "chromium-browser"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            subprocess.Popen(
                [
                    exe,
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-sync",
                    "--new-window",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError:
            continue

    try:
        webbrowser.open(url)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Panel web local para xrepost (escucha solo en 127.0.0.1)."
    )
    parser.add_argument("--port", type=int, default=8765, help="Puerto (0 = aleatorio; por defecto 8765).")
    parser.add_argument("--host", default="127.0.0.1", help="Interfaz de escucha (no la expongas a la red).")
    parser.add_argument("--token", default="", help="Token fijo (por defecto se genera uno aleatorio).")
    parser.add_argument(
        "--engine",
        default=os.environ.get("XREPOST_ENGINE", str(DEFAULT_ENGINE)),
        help="Ruta a xrepost.sh.",
    )
    parser.add_argument(
        "--history",
        default=str(default_history()),
        help="Ruta al historial JSONL.",
    )
    parser.add_argument(
        "--accounts",
        default=os.environ.get("X_ACCOUNTS", ""),
        help="Ruta de las cuentas recordadas (por defecto, junto al historial).",
    )
    parser.add_argument("--runs", default=str(DEFAULT_RUNS), help="Carpeta de ejecuciones y exportaciones.")
    parser.add_argument("--no-open", action="store_true", help="No abrir el navegador automáticamente.")
    parser.add_argument("--verbose", action="store_true", help="Registrar cada petición HTTP.")
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "Error: el panel solo puede escuchar en loopback "
            "(127.0.0.1, localhost o ::1); no se expone a la red.",
            file=sys.stderr,
        )
        return 2

    token = args.token or secrets.token_urlsafe(18)
    app = App(
        engine=Path(args.engine),
        history=Path(args.history),
        runs=Path(args.runs),
        accounts=Path(args.accounts) if args.accounts else None,
        token=token,
        verbose=args.verbose,
    )

    ports = [args.port] if args.port == 0 else ([args.port] if args.port != 8765 else [8765, 8766, 8767, 8768, 8769])
    httpd: ThreadingHTTPServer | None = None
    last_error: OSError | None = None
    for port in ports:
        try:
            httpd = create_server(app, port, args.host)
            break
        except OSError as exc:
            last_error = exc
    if httpd is None:
        print(f"No se pudo abrir el puerto: {last_error}", file=sys.stderr)
        return 1

    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={token}"
    print()
    print("  xrepost · panel local")
    print(f"  Panel:     {url}")
    print(f"  Motor:     {app.engine}")
    print(f"  Historial: {app.history}")
    print(f"  Cuentas:   {app.accounts}")
    print("  Solo escucha en 127.0.0.1. Ctrl-C para detener.")
    print()

    if not args.no_open:
        open_panel(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrando panel…")
    finally:
        app.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
