#!/usr/bin/env python3
"""Cuentas usadas antes en xrepost: almacenamiento, ranking y prompt.

El archivo por defecto es ``accounts.json`` junto al historial
(``~/.local/state/xrepost/accounts.json``). Se comparte entre el CLI y el panel
web, y se completa también con los ``search_user`` que ya existen en el
historial. ``X_ACCOUNTS`` permite usar otra ruta.

Uso como helper del CLI:

  python3 xrepost_accounts.py prompt   # entrada interactiva con sugerencias
  python3 xrepost_accounts.py add a,b  # recordar cuentas
  python3 xrepost_accounts.py list [q] # listar coincidencias
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
MAX_STORED = 200
DEFAULT_LIMIT = 8
VERSION = 1

PROMPT = "Usuarios (separados por coma, @ opcional): "
MAX_VISIBLE = 6


def default_history() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return Path(os.environ.get("X_HISTORY") or (base / "xrepost" / "history.jsonl")).expanduser()


def resolve_paths(
    accounts: str | Path | None = None,
    history: str | Path | None = None,
) -> tuple[Path, Path]:
    """Devuelve (cuentas.json, historial.jsonl) respetando X_ACCOUNTS/X_HISTORY."""
    hist = Path(history).expanduser() if history else default_history()
    acc_env = os.environ.get("X_ACCOUNTS")
    if accounts:
        acc = Path(accounts).expanduser()
    elif acc_env:
        acc = Path(acc_env).expanduser()
    else:
        acc = hist.parent / "accounts.json"
    return acc, hist


def normalize_handle(value: object) -> str:
    return str(value or "").strip().lstrip("@").strip()


def split_handles(values: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(values, str):
        raw = values.split(",")
    else:
        raw = list(values)
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        handle = normalize_handle(value)
        if not HANDLE_RE.fullmatch(handle):
            continue
        key = handle.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(handle)
    return out


def _parse_ts(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _read_file(path: Path) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return entries

    items = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return entries

    for item in items:
        if isinstance(item, str):
            handle, count, last = item, 1, ""
        elif isinstance(item, dict):
            handle = item.get("handle")
            try:
                count = max(0, int(item.get("count") or 1))
            except (TypeError, ValueError):
                count = 1
            last = str(item.get("last_used") or "")
        else:
            continue
        handle = normalize_handle(handle)
        if not HANDLE_RE.fullmatch(handle):
            continue
        key = handle.lower()
        current = entries.get(key)
        if current is None:
            entries[key] = {"handle": handle, "count": count, "last_used": last}
        else:
            current["count"] = max(current["count"], count)
            if _parse_ts(last) > _parse_ts(current["last_used"]):
                current["last_used"] = last
                current["handle"] = handle
    return entries


def _merge_history(entries: dict[str, dict], history: Path) -> None:
    counts: dict[str, int] = {}
    try:
        with open(history, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                handle = normalize_handle(item.get("search_user"))
                if not HANDLE_RE.fullmatch(handle):
                    continue
                key = handle.lower()
                counts[key] = counts.get(key, 0) + 1
                current = entries.get(key)
                ts = str(item.get("ts") or "")
                if current is None:
                    entries[key] = {"handle": handle, "count": 1, "last_used": ts}
                else:
                    current["count"] = max(current["count"], counts[key])
                    if _parse_ts(ts) > _parse_ts(current["last_used"]):
                        current["last_used"] = ts
                        current["handle"] = handle
    except FileNotFoundError:
        return
    except OSError:
        return


def load_accounts(
    accounts: str | Path | None = None,
    history: str | Path | None = None,
) -> dict[str, dict]:
    acc_path, hist_path = resolve_paths(accounts, history)
    entries = _read_file(acc_path)
    _merge_history(entries, hist_path)
    return entries


def _write_file(path: Path, entries: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        entries.values(),
        key=lambda e: (_parse_ts(e.get("last_used")), int(e.get("count") or 0)),
        reverse=True,
    )[:MAX_STORED]
    payload = {
        "version": VERSION,
        "accounts": [
            {
                "handle": item["handle"],
                "count": int(item.get("count") or 1),
                "last_used": item.get("last_used") or "",
            }
            for item in ordered
        ],
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def add_accounts(
    values: str | list[str] | tuple[str, ...],
    accounts: str | Path | None = None,
) -> bool:
    """Suma uno o varios handles al archivo. Devuelve False si no se pudo."""
    handles = split_handles(values)
    if not handles:
        return False
    acc_path, _ = resolve_paths(accounts, None)
    lock_path = acc_path.with_name(acc_path.name + ".lock")
    try:
        acc_path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        return False

    try:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass

        entries = _read_file(acc_path)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for handle in handles:
            key = handle.lower()
            current = entries.get(key)
            if current is None:
                entries[key] = {"handle": handle, "count": 1, "last_used": now}
            else:
                current["handle"] = handle
                current["count"] = int(current.get("count") or 1) + 1
                current["last_used"] = now
        _write_file(acc_path, entries)
        return True
    except OSError:
        return False
    finally:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        lock.close()


def rank_accounts(
    query: str,
    entries: dict[str, dict],
    limit: int = DEFAULT_LIMIT,
    exclude: set[str] | list[str] | tuple[str, ...] = (),
) -> list[dict]:
    q = normalize_handle(query).lower()
    blocked = {normalize_handle(item).lower() for item in exclude}
    ranked: list[tuple] = []

    for entry in entries.values():
        handle = str(entry.get("handle") or "")
        low = handle.lower()
        if not low or low in blocked:
            continue
        if q:
            if low == q:
                kind = 3
            elif low.startswith(q):
                kind = 2
            elif q in low:
                kind = 1
            else:
                continue
        else:
            kind = 0
        ranked.append(
            (
                kind,
                _parse_ts(entry.get("last_used")),
                int(entry.get("count") or 0),
                -len(handle),
                handle,
                entry,
            )
        )

    # Alfabético solo como último desempate; después, coincidencia y recencia.
    ranked.sort(key=lambda item: item[4].lower())
    ranked.sort(key=lambda item: item[:4], reverse=True)
    out = []
    for *_, handle, entry in ranked[: max(0, limit)]:
        out.append(
            {
                "handle": handle,
                "count": int(entry.get("count") or 0),
                "last_used": entry.get("last_used") or "",
            }
        )
    return out


def suggest(
    query: str = "",
    accounts: str | Path | None = None,
    history: str | Path | None = None,
    limit: int = DEFAULT_LIMIT,
    exclude: set[str] | list[str] | tuple[str, ...] = (),
) -> list[dict]:
    entries = load_accounts(accounts, history)
    return rank_accounts(query, entries, limit=limit, exclude=exclude)


# --- prompt interactivo -----------------------------------------------------


def _token_bounds(text: str) -> tuple[int, int, str]:
    """Token bajo el cursor (siempre al final): inicio, fin y consulta."""
    pos = len(text)
    start = text.rfind(",", 0, pos) + 1
    end = text.find(",", pos)
    if end == -1:
        end = len(text)
    raw = text[start:end]
    return start, end, raw.strip().lstrip("@")


def _used_handles(text: str) -> set[str]:
    used: set[str] = set()
    for part in text.split(","):
        handle = normalize_handle(part)
        if HANDLE_RE.fullmatch(handle):
            used.add(handle.lower())
    return used


def _replace_token(text: str, handle: str, add_comma: bool = False) -> str:
    start, end, _ = _token_bounds(text)
    raw = text[start:end]
    lead = raw[: len(raw) - len(raw.lstrip())]
    suffix = text[end:]
    replacement = lead + "@" + handle
    if add_comma and not suffix.startswith(","):
        replacement += ", "
    return text[:start] + replacement + suffix


def _read_escape(fd: int) -> bytes:
    import select

    seq = b""
    while len(seq) < 4 and select.select([fd], [], [], 0.02)[0]:
        chunk = os.read(fd, 1)
        if not chunk:
            break
        seq += chunk
        if seq in (b"[A", b"[B", b"[C", b"[D", b"OA", b"OB", b"[Z"):
            break
    return seq


def _draw(
    text: str,
    matches: list[dict],
    active: int,
    *,
    show: bool = True,
) -> None:
    line = "\r\033[K" + PROMPT + text
    out = [line, "\033[J"]
    if show and matches:
        out.append("\033[s")
        for index, item in enumerate(matches):
            marker = "❯" if index == active else " "
            uses = int(item.get("count") or 0)
            suffix = f"  · {uses} usos" if uses > 1 else ""
            out.append(f"\n  {marker} @{item['handle']}{suffix}")
        out.append("\033[u")
    sys.stderr.write("".join(out))
    sys.stderr.flush()


def _finish(text: str, *, cancelled: bool = False) -> None:
    sys.stderr.write("\r\033[J")
    if not cancelled:
        sys.stderr.write(PROMPT + text + "\n")
    else:
        sys.stderr.write("\n")
    sys.stderr.flush()


def prompt_accounts(accounts: str | Path | None = None, history: str | Path | None = None) -> int:
    interactive = sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive:
        sys.stderr.write(PROMPT)
        sys.stderr.flush()
        value = sys.stdin.readline()
        if not value:
            return 1
        text = value.strip()
        if not text:
            return 1
        print(text)
        return 0

    try:
        import termios
    except ImportError:
        sys.stderr.write(PROMPT)
        sys.stderr.flush()
        value = sys.stdin.readline().strip()
        if not value:
            return 1
        print(value)
        return 0

    entries = load_accounts(accounts, history)
    has_suggestions = bool(entries)
    if has_suggestions:
        sys.stderr.write("  ↑/↓ elegir · Tab completar · Enter aceptar · Esc cerrar\n")
        sys.stderr.flush()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    text = ""
    active = -1
    show = has_suggestions
    matches: list[dict] = []

    def refresh() -> None:
        nonlocal matches, active
        start, end, query = _token_bounds(text)
        used = _used_handles(text[:start] + "," + text[end:])
        matches = rank_accounts(query, entries, limit=MAX_VISIBLE, exclude=used)
        if active >= len(matches):
            active = len(matches) - 1

    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ICANON | termios.ECHO)
    termios.tcsetattr(fd, termios.TCSADRAIN, new)

    try:
        refresh()
        _draw(text, matches, active, show=show)
        while True:
            chunk = os.read(fd, 1)
            if not chunk:
                _finish(text, cancelled=True)
                return 1

            if chunk in (b"\r", b"\n"):
                if active >= 0 and matches:
                    text = _replace_token(text, matches[active]["handle"])
                text = text.strip().rstrip(",").strip()
                _finish(text)
                if not text:
                    return 1
                print(text)
                return 0

            if chunk == b"\x03":
                _finish(text, cancelled=True)
                return 1

            if chunk == b"\x04":
                if not text:
                    _finish(text, cancelled=True)
                    return 1
                continue

            if chunk == b"\t":
                if matches:
                    if active < 0:
                        active = 0
                    text = _replace_token(text, matches[active]["handle"], add_comma=True)
                    active = -1
                    show = True
                    refresh()
                    _draw(text, matches, active, show=show)
                continue

            if chunk == b"\x1b":
                seq = _read_escape(fd)
                if seq in (b"[A", b"OA"):
                    if matches:
                        active = (active - 1) % len(matches) if active > 0 else len(matches) - 1
                elif seq in (b"[B", b"OB"):
                    if matches:
                        active = (active + 1) % len(matches) if active >= 0 else 0
                else:
                    active = -1
                    show = True
                refresh()
                _draw(text, matches, active, show=show)
                continue

            if chunk in (b"\x7f", b"\x08"):
                text = text[:-1]
                active = -1
                show = True
                refresh()
                _draw(text, matches, active, show=show)
                continue

            if chunk == b"\x15":  # Ctrl-U
                text = ""
                active = -1
                show = True
                refresh()
                _draw(text, matches, active, show=show)
                continue

            if 32 <= chunk[0] <= 126:
                text += chunk.decode("ascii")
                active = -1
                show = True
                refresh()
                _draw(text, matches, active, show=show)
    except KeyboardInterrupt:
        _finish(text, cancelled=True)
        return 1
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cuentas recordadas por xrepost.")
    parser.add_argument("--accounts", default="", help="Ruta de accounts.json (o X_ACCOUNTS).")
    parser.add_argument("--history", default="", help="Ruta del historial JSONL (o X_HISTORY).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prompt = sub.add_parser("prompt", help="Pedir cuentas con sugerencias.")
    p_prompt.add_argument("--accounts", default=argparse.SUPPRESS)
    p_prompt.add_argument("--history", default=argparse.SUPPRESS)

    p_add = sub.add_parser("add", help="Recordar cuentas.")
    p_add.add_argument("values", nargs="+", help="Cuentas separadas por coma o espacios.")
    p_add.add_argument("--accounts", default=argparse.SUPPRESS)

    p_list = sub.add_parser("list", help="Listar cuentas que coinciden.")
    p_list.add_argument("query", nargs="?", default="")
    p_list.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p_list.add_argument("--json", action="store_true", help="Salida JSON.")
    p_list.add_argument("--accounts", default=argparse.SUPPRESS)
    p_list.add_argument("--history", default=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    accounts = args.accounts or None
    history = args.history or None

    if args.command == "prompt":
        return prompt_accounts(accounts, history)

    if args.command == "add":
        values: list[str] = []
        for item in args.values:
            values.extend(item.split(","))
        return 0 if add_accounts(values, accounts) else 1

    if args.command == "list":
        items = suggest(args.query, accounts, history, limit=max(1, min(args.limit, 50)))
        if args.json:
            print(json.dumps({"accounts": items}, ensure_ascii=False))
        else:
            for item in items:
                print(item["handle"])
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
