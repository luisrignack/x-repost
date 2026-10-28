#!/usr/bin/env bash
# xrepost — repostea posts de usuarios concretos en un rango de fechas.
#
# Uso:
#   ./xrepost.sh                          # interactivo (pregunta usuarios y fechas)
#   ./xrepost.sh <usuarios> <desde> [hasta]
#
# Opciones:
#   --csv RUTA          exportar el listado a CSV
#   --md RUTA           exportar el listado a Markdown
#   --force             ignorar el historial (repostea aunque ya conste)
#   --history [N]       ver los últimos N movimientos (20 por defecto)
#   --undo-last [N]     deshacer la última tanda (o los últimos N reposts)
#   --undo URL|ID       deshacer un repost concreto
#   -h, --help          esta ayuda
#
# Variables opcionales:
#   X_APPLY=1      repostear de verdad (por defecto solo lista)
#   X_LIMIT=5      máximo de reposts por ejecución
#   X_DELAY=30     segundos entre reposts
#   X_SKIP_REPLIES=0  incluir respuestas (por defecto se ignoran)
#   X_SKIP_RETWEETS=0 incluir retweets (por defecto se ignoran)
#   X_MATCH=a,b    solo posts que contengan alguna de esas palabras
#   X_EXCLUDE=c,d  descartar posts que contengan alguna
#   BU_CDP_URL=... endpoint de Chrome (default http://127.0.0.1:9222)
#
# Historial: ~/.local/state/xrepost/history.jsonl (cambiable con X_HISTORY)

set -euo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
export X_HISTORY="${X_HISTORY:-${XDG_STATE_HOME:-$HOME/.local/state}/xrepost/history.jsonl}"

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

MODE="repost"
UNDO_TARGET=""
UNDO_N=""
HISTORY_N=""
CSV=""
MD=""
FORCE=0
POS=()

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage; exit 0 ;;
    --csv)
      [ $# -ge 2 ] || { echo "Falta la ruta de --csv" >&2; exit 1; }
      CSV="$2"; shift 2 ;;
    --md)
      [ $# -ge 2 ] || { echo "Falta la ruta de --md" >&2; exit 1; }
      MD="$2"; shift 2 ;;
    --force)
      FORCE=1; shift ;;
    --history)
      MODE="history"; shift
      if [ $# -gt 0 ] && [[ "$1" =~ ^[0-9]+$ ]]; then HISTORY_N="$1"; shift; fi ;;
    --undo-last)
      MODE="undo_last"; shift
      if [ $# -gt 0 ] && [[ "$1" =~ ^[0-9]+$ ]]; then UNDO_N="$1"; shift; fi ;;
    --undo)
      [ $# -ge 2 ] || { echo "Falta la URL o el ID de --undo" >&2; exit 1; }
      MODE="undo"; UNDO_TARGET="$2"; shift 2 ;;
    --)
      shift; POS+=("$@"); break ;;
    -*)
      echo "Opción desconocida: $1 (usa --help)" >&2; exit 1 ;;
    *)
      POS+=("$1"); shift ;;
  esac
done

export X_MODE="$MODE" X_UNDO_TARGET="$UNDO_TARGET" X_UNDO_N="$UNDO_N"

if [ "$MODE" = "history" ]; then
  python3 - "$X_HISTORY" "${HISTORY_N:-20}" <<'PY'
import json, sys

LABELS = {
    "reposted": "reposteado", "already": "ya estaba reposteado",
    "no_button": "sin botón de repost", "no_menu": "menú no abierto",
    "unconfirmed": "no confirmado", "undone": "deshecho",
    "not_reposted": "no estaba reposteado", "undo_failed": "no se pudo deshacer",
}

path, n = sys.argv[1], max(0, int(sys.argv[2] or 20))
try:
    with open(path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
except FileNotFoundError:
    print(f"Sin historial todavía: {path}")
    raise SystemExit(0)
except OSError as e:
    print(f"No se pudo leer el historial ({path}): {e}")
    raise SystemExit(0)
sel = lines[-n:] if n else []
print(f"Historial: {path} ({len(lines)} eventos); mostrando {len(sel)}\n")
for line in sel:
    try:
        e = json.loads(line)
    except ValueError:
        continue
    handle = ("@" + e["handle"]) if e.get("handle") else ""
    result = e.get("result", "")
    print(f"{e.get('ts', '')[:19]}  {e.get('action', ''):6} "
          f"{LABELS.get(result, result):24} {handle:18} {e.get('url', '')}")
PY
  exit 0
fi

if [ "$MODE" = "repost" ]; then
  if [ ${#POS[@]} -ge 2 ]; then
    export X_USERS="${POS[0]}"
    export X_FROM="${POS[1]}"
    export X_TO="${POS[2]:-${POS[1]}}"
  elif [ ${#POS[@]} -eq 1 ]; then
    echo "Faltan la fecha (o usa --help)." >&2
    exit 1
  elif [ -n "${X_USERS:-}" ] && [ -n "${X_FROM:-}" ]; then
    export X_USERS X_FROM
    export X_TO="${X_TO:-$X_FROM}"
  else
    echo "=== xrepost ==="
    read -r -p "Usuarios (separados por coma, @ opcional): " X_USERS || X_USERS=""
    read -r -p "Fecha desde (YYYY-MM-DD): " X_FROM || X_FROM=""
    read -r -p "Fecha hasta (Enter = la misma): " X_TO || X_TO=""
    X_TO="${X_TO:-$X_FROM}"
    export X_USERS X_FROM X_TO
  fi

  if [ -z "${X_USERS:-}" ] || [ -z "${X_FROM:-}" ]; then
    echo "Faltan usuarios o fecha." >&2
    exit 1
  fi
fi

export X_APPLY="${X_APPLY:-0}"
export X_LIMIT="${X_LIMIT:-10}"
export X_DELAY="${X_DELAY:-20}"
export X_SKIP_REPLIES="${X_SKIP_REPLIES:-1}"
export X_SKIP_RETWEETS="${X_SKIP_RETWEETS:-1}"
export X_MATCH="${X_MATCH:-}"
export X_EXCLUDE="${X_EXCLUDE:-}"
export X_FORCE="$FORCE"
export X_CSV="$CSV"
export X_MD="$MD"
export BU_CDP_URL="${BU_CDP_URL:-http://127.0.0.1:9222}"

if [ "$MODE" = "repost" ] && [ "$X_APPLY" != "1" ]; then
  echo
  echo "AVISO: modo DRY-RUN. Para repostear de verdad: X_APPLY=1 $0 ..."
  echo
fi

exec browser-use < "$SELF_DIR/xrepost.py"