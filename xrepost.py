"""
xrepost — descubre posts de usuarios concretos en un rango de fechas y,
opcionalmente, los repostea desde la sesión de Chrome controlada por browser-use.

Variables de entorno (las pone xrepost.sh):
  X_USERS   lista separada por comas: "usuario1,usuario2"  (@ opcional)
  X_FROM    fecha inicial YYYY-MM-DD
  X_TO      fecha final   YYYY-MM-DD (incluida)
  X_APPLY   0 = solo listar (dry-run) | 1 = repostear
  X_LIMIT   máximo de reposts por ejecución (default 10)
  X_DELAY   segundos entre reposts (default 20)
  X_SKIP_REPLIES 1 = ignorar respuestas (default 1)
  X_SKIP_RETWEETS 1 = ignorar retweets (default 1)
  X_MATCH   palabras que debe contener el post, separadas por comas (opcional)
  X_EXCLUDE palabras que descartan el post, separadas por comas (opcional)
  X_FORCE   "1" = ignorar el historial y volver a procesar lo ya reposteado
  X_HISTORY ruta del historial JSONL
  X_CSV     exportar el listado a CSV
  X_MD      exportar el listado a Markdown
  X_MODE    repost (default) | undo | undo_last
"""

import csv
import json
import os
import re
import sys
import time
import datetime as dt
from urllib.parse import quote

USERS = [u.strip().lstrip("@") for u in os.environ.get("X_USERS", "").split(",") if u.strip()]
D_FROM = os.environ.get("X_FROM", "")
D_TO = os.environ.get("X_TO", "") or D_FROM
APPLY = os.environ.get("X_APPLY", "0") == "1"
LIMIT = int(os.environ.get("X_LIMIT", "10"))
DELAY = float(os.environ.get("X_DELAY", "20"))
SKIP_REPLIES = os.environ.get("X_SKIP_REPLIES", "1") == "1"
SKIP_RETWEETS = os.environ.get("X_SKIP_RETWEETS", "1") == "1"
MODE = os.environ.get("X_MODE", "repost")
FORCE = os.environ.get("X_FORCE", "0") == "1"
MATCH = [k.strip().lower() for k in os.environ.get("X_MATCH", "").split(",") if k.strip()]
EXCLUDE = [k.strip().lower() for k in os.environ.get("X_EXCLUDE", "").split(",") if k.strip()]
CSV_PATH = os.environ.get("X_CSV", "")
MD_PATH = os.environ.get("X_MD", "")
RUN_ID = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
HISTORY = os.environ.get("X_HISTORY") or os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "xrepost",
    "history.jsonl",
)

SKIPPED_RT = 0
SKIPPED_KW = 0
SEEN = set()  # URLs ya aceptadas; global para no duplicar entre usuarios

# Códigos internos (historial) → texto que ve el usuario.
LABELS = {
    "reposted": "reposteado",
    "already": "ya estaba reposteado",
    "no_button": "no encontré el botón de repost",
    "no_menu": "el menú no se abrió",
    "unconfirmed": "no se pudo confirmar",
    "undone": "deshecho",
    "not_reposted": "no estaba reposteado",
    "undo_failed": "no se pudo deshacer",
}


def label(res):
    return LABELS.get(res, res)

# Textos de "respuesta" en varios idiomas: solo como respaldo si el DOM no basta.
REPLY_HINTS = (
    "replying to", "in reply to", "respondiendo a", "en respuesta a",
    "réponse à", "antwort auf", "risposta a", "respondendo a", "em resposta a",
    "in risposta a", "yanıt", "в ответ", "返信", "回复", "回覆",
)

COLLECT_JS = (
    "Array.from(document.querySelectorAll('article[data-testid=\"tweet\"]'))"
    ".map(function(a){"
    "var t=a.querySelector('time');"
    "var l=a.querySelector('a[href*=\"/status/\"]');"
    "var n=a.querySelector('[data-testid=\"User-Name\"]');"
    "var x=a.querySelector('[data-testid=\"tweetText\"]');"
    "var r=a.querySelector('[data-testid=\"socialContext\"]');"
    "var lines=n?n.innerText.split('\\n'):[];"
    "var handle='';"
    "for(var i=0;i<lines.length;i++){"
    "var s=lines[i].trim();"
    "if(s.charAt(0)==='@'){handle=s.slice(1).replace(/[^A-Za-z0-9_]/g,'');break;}}"
    "var reply=false;"
    "if(n){"
    "var links=a.querySelectorAll('a[href]');"
    "for(var i=0;i<links.length;i++){"
    "var lk=links[i];"
    "var h=lk.getAttribute('href')||'';"
    "var txt=(lk.innerText||'').trim();"
    "if(!/^\\/[A-Za-z0-9_]+$/.test(h)||txt.charAt(0)!=='@')continue;"
    "if(n.contains(lk))continue;"
    "if(x&&x.contains(lk))continue;"
    "if(!x||(lk.compareDocumentPosition(x)&Node.DOCUMENT_POSITION_FOLLOWING)){reply=true;break;}}}"
    "return {"
    "time: t?t.getAttribute('datetime'):'',"
    "url: l?('https://x.com'+l.getAttribute('href')):'',"
    "user: n?n.innerText.split('\\n')[0]:'',"
    "handle: handle,"
    "text: x?x.innerText.slice(0,500):'',"
    "ctx: r?r.innerText:'',"
    "reply: reply"
    "};})"
)

N_ARTICLES = "document.querySelectorAll('article[data-testid=\"tweet\"]').length"
N_RETWEET = "document.querySelectorAll('[data-testid=\"retweet\"]').length"
N_UNRETWEET = "document.querySelectorAll('[data-testid=\"unretweet\"]').length"
HAS_CONFIRM = "!!document.querySelector('[data-testid=\"retweetConfirm\"]')"
HAS_UNDO = "!!document.querySelector('[data-testid=\"unretweetConfirm\"]')"
FIRST_RETWEET = "document.querySelector('[data-testid=\"retweet\"]')"
FIRST_UNRETWEET = "document.querySelector('[data-testid=\"unretweet\"]')"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def next_day(d):
    return (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()


def handle_from_url(url):
    m = re.search(r"x\.com/([^/?#]+)/status/", url or "")
    return m.group(1) if m else ""


def tweet_id(url_or_id):
    s = (url_or_id or "").strip()
    m = re.search(r"/status/(\d+)", s)
    return m.group(1) if m else s


def text_ok(text):
    low = (text or "").lower()
    if MATCH and not any(k in low for k in MATCH):
        return False
    if any(k in low for k in EXCLUDE):
        return False
    return True


# --- historial --------------------------------------------------------------

def load_history():
    entries = []
    try:
        with open(HISTORY, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("id"):
                    entries.append(e)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"  (aviso: no se pudo leer el historial: {e})")
    return entries


def save_event(action, result, post=None, entry=None):
    base = entry or post or {}
    rec = {
        "ts": now(),
        "run": RUN_ID,
        "action": action,
        "result": result,
        "id": base.get("id") or tweet_id(base.get("url", "")),
        "url": base.get("url", ""),
        "search_user": base.get("search_user", ""),
        "author": base.get("author") or base.get("user", ""),
        "handle": base.get("handle", ""),
        "posted_at": base.get("posted_at") or base.get("time", ""),
    }
    try:
        d = os.path.dirname(HISTORY)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"  (aviso: no se pudo guardar el historial: {e})")


def history_state(entries):
    """Devuelve (reposteados, nuestros).

    reposteados: IDs cuyo último estado conocido es "reposteado".
    nuestros:    id -> evento del último repost hecho por la herramienta
                 que sigue vigente (para poder deshacerlo).
    """
    reposted = set()
    ours = {}
    for e in entries:
        action, res = e.get("action"), e.get("result")
        if action == "repost":
            if res == "reposted":
                reposted.add(e["id"])
                ours.pop(e["id"], None)  # que la última acción quede al final
                ours[e["id"]] = e
            elif res == "already":
                reposted.add(e["id"])
                # si ya era nuestro, sigue siéndolo; si no, no lo reclamamos
            # un fallo no cambia lo que sabíamos del estado
        elif action == "undo" and res in ("undone", "not_reposted"):
            reposted.discard(e["id"])
            ours.pop(e["id"], None)
    return reposted, ours


def reposted_ids(entries):
    """IDs cuyo último estado conocido indica que están reposteados."""
    return history_state(entries)[0]


def undo_candidates(entries, target="", n=0):
    """Reposts hechos por la herramienta que siguen vigentes."""
    cand = list(history_state(entries)[1].values())
    if target:
        tid = tweet_id(target)
        return [e for e in cand if e["id"] == tid or (e.get("url") or "").rstrip("/") == target.rstrip("/")]
    if n:
        return cand[-n:]
    if not cand:
        return []
    last_run = cand[-1].get("run")
    return [e for e in cand if e.get("run") == last_run]


# --- navegador --------------------------------------------------------------

def wait_articles(timeout=12):
    for _ in range(timeout):
        if js(N_ARTICLES) > 0:
            return True
        time.sleep(1)
    return False


def collect(user, d_from, d_to):
    global SKIPPED_RT, SKIPPED_KW
    q = f"from:{user} since:{d_from} until:{next_day(d_to)}"
    url = "https://x.com/search?q=" + quote(q) + "&f=live"
    print(f"\n[{user}] buscando {d_from} .. {d_to}")
    goto_url(url)
    wait_for_load()
    if not wait_articles():
        print("  sin resultados (o no cargó)")
        return []

    seen, out = set(), []
    for _ in range(8):
        for r in (js(COLLECT_JS) or []):
            url = r.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            day = (r.get("time") or "")[:10]
            if not day or not (d_from <= day <= d_to):
                continue
            author = (r.get("handle") or handle_from_url(url)).lstrip("@").lower()
            if SKIP_RETWEETS and author and author != user.lower():
                SKIPPED_RT += 1
                continue
            ctx = (r.get("ctx") or "").lower()
            if SKIP_REPLIES and (r.get("reply") or any(k in ctx for k in REPLY_HINTS)):
                continue
            if not text_ok(r.get("text")):
                SKIPPED_KW += 1
                continue
            if url in SEEN:
                continue
            SEEN.add(url)
            r["search_user"] = user
            out.append(r)
        if len(out) >= 40:
            break
        js("window.scrollBy(0, 1600)")
        time.sleep(1.8)
    return out


def repost(url):
    goto_url(url)
    wait_for_load()
    time.sleep(1.5)
    if js(N_UNRETWEET) > 0:
        return "already"
    if js(N_RETWEET) == 0:
        return "no_button"
    js(FIRST_RETWEET + ".click()")
    time.sleep(1.3)
    if not js(HAS_CONFIRM):
        return "no_menu"
    js("document.querySelector('[data-testid=\"retweetConfirm\"]').click()")
    time.sleep(2.5)
    return "reposted" if js(N_UNRETWEET) > 0 else "unconfirmed"


def undo_one(url):
    goto_url(url)
    wait_for_load()
    time.sleep(1.5)
    if js(N_UNRETWEET) == 0:
        return "not_reposted"
    js(FIRST_UNRETWEET + ".click()")
    time.sleep(1.2)
    if js(HAS_UNDO):
        js("document.querySelector('[data-testid=\"unretweetConfirm\"]').click()")
        time.sleep(2.0)
    return "undone" if js(N_UNRETWEET) == 0 else "undo_failed"


# --- exportación ------------------------------------------------------------

def build_rows(posts, statuses, default_status):
    rows = []
    for p in posts:
        rows.append({
            "fecha": (p.get("time") or "")[:16].replace("T", " "),
            "autor": p.get("user", ""),
            "usuario": ("@" + p["handle"]) if p.get("handle") else "",
            "texto": (p.get("text") or "").replace("\n", " ").strip(),
            "url": p.get("url", ""),
            "estado": label(statuses.get(p.get("url", ""), default_status)),
        })
    return rows


def write_csv(rows, path):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fecha", "autor", "usuario", "texto", "url", "estado"])
        w.writeheader()
        w.writerows(rows)


def write_md(rows, path):
    def esc(s):
        return str(s).replace("|", "\\|").replace("\n", " ")

    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    lines = ["| Fecha | Autor | Texto | Enlace | Estado |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {esc(r['fecha'])} | {esc(r['autor'])} {esc(r['usuario'])} | "
            f"{esc(r['texto'])} | [post]({r['url']}) | {esc(r['estado'])} |"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def do_export(posts, statuses=None, default_status="solo listado"):
    if not (CSV_PATH or MD_PATH):
        return
    rows = build_rows(posts, statuses or {}, default_status)
    if CSV_PATH:
        write_csv(rows, CSV_PATH)
        print(f"  CSV guardado en {CSV_PATH}")
    if MD_PATH:
        write_md(rows, MD_PATH)
        print(f"  Markdown guardado en {MD_PATH}")


# --- deshacer ---------------------------------------------------------------

def run_undo():
    entries = load_history()
    target = os.environ.get("X_UNDO_TARGET", "").strip()
    try:
        n = int(os.environ.get("X_UNDO_N") or 0)
    except ValueError:
        n = 0

    cand = undo_candidates(entries, target=target, n=n)
    if not cand:
        if target:
            print(f"No encuentro ese repost en el historial: {target}")
        elif n:
            print("No hay reposts que deshacer en el historial.")
        else:
            print("No hay reposts de la última ejecución que deshacer.")
        return 0

    print(f"Deshaciendo {len(cand)} repost(s)...\n")
    done = 0
    for e in cand:
        url = e.get("url") or f"https://x.com/i/status/{e['id']}"
        res = undo_one(url)
        print(f"  {label(res)}: {url}")
        save_event("undo", res, entry=e)
        if res == "undone":
            done += 1
            time.sleep(DELAY)
    print(f"\nTotal deshecho: {done}")
    return 0


# --- principal --------------------------------------------------------------

def main():
    if MODE in ("undo", "undo_last"):
        return run_undo()

    if not USERS or not D_FROM:
        print("faltan X_USERS o X_FROM")
        return 2

    print(f"modo: {'APLICAR reposts' if APPLY else 'DRY-RUN (solo listar)'} | límite={LIMIT} | delay={DELAY}s")
    if MATCH or EXCLUDE:
        print(f"filtros de texto: incluir={MATCH or '—'} | excluir={EXCLUDE or '—'}")

    found = []
    for u in USERS:
        try:
            found.extend(collect(u, D_FROM, D_TO))
        except Exception as e:
            print(f"  error con {u}: {e}")

    if SKIPPED_RT:
        print(f"\n({SKIPPED_RT} retweets ignorados: el autor original no es el buscado)")
    if SKIPPED_KW:
        print(f"({SKIPPED_KW} descartados por X_MATCH/X_EXCLUDE)")

    skipped_hist = 0
    already = reposted_ids(load_history())
    if already and not FORCE:
        before = len(found)
        found = [p for p in found if tweet_id(p.get("url", "")) not in already]
        skipped_hist = before - len(found)
        if skipped_hist:
            print(f"({skipped_hist} ya reposteados en ejecuciones anteriores; usa --force para incluirlos)")

    if not found:
        print("\nSin posts en ese rango.")
        do_export([])
        return 0

    print(f"\n{len(found)} posts encontrados:\n")
    for i, p in enumerate(found, 1):
        print(f"{i:3}. {p['time'][:16]} | {p['user']}")
        print(f"     {p['text'][:150].replace(chr(10), ' ')}")
        print(f"     {p['url']}")

    statuses = {}
    do_export(found, default_status="solo listado" if not APPLY else "pendiente")

    if not APPLY:
        print("\nDRY-RUN: no se ha reposteado nada. Usa X_APPLY=1 para ejecutar.")
        return 0

    print(f"\nReposteando (máx {LIMIT})...\n")
    done = 0
    for p in found:
        if done >= LIMIT:
            print(f"límite de {LIMIT} alcanzado")
            break
        res = repost(p["url"])
        statuses[p["url"]] = res
        print(f"  {label(res)}: {p['url']}")
        save_event("repost", res, post=p)
        if res == "reposted":
            done += 1
            time.sleep(DELAY)
    do_export(found, statuses, default_status="no procesado")
    print(f"\nTotal reposteado: {done}")
    return 0


sys.exit(main())