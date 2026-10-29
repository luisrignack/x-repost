#!/usr/bin/env bash
# xrepost — repostea posts de usuarios concretos en un rango de fechas.
#
# Uso:
#   ./xrepost.sh                          # interactivo (pregunta usuarios, fechas y alcance)
#   ./xrepost.sh <usuarios> <desde> [hasta]
#
# Fechas (modo interactivo): campo AAAA-MM-DD con avance automático (los guiones
# se insertan solos y al escribir el octavo dígito se acepta). Enter acepta el
# valor entre corchetes; se valida el calendario y que desde <= hasta.
#
# Retweets: en modo interactivo se pregunta si se incluyen los retweets que las
# cuentas hayan hecho de otras cuentas. Por defecto solo se consideran las
# publicaciones originales (X_SKIP_RETWEETS=1).
#
# Cuentas: el modo interactivo sugiere las cuentas usadas antes mientras se
# escribe (↑/↓ elige, Tab completa, Enter acepta). Se guardan junto al historial.
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
#   X_ACCOUNTS=... archivo de cuentas recordadas (por defecto, junto al historial)
#   BU_CDP_URL=... endpoint de Chrome (default http://127.0.0.1:9222)
#
# Historial: ~/.local/state/xrepost/history.jsonl (cambiable con X_HISTORY)
# Cuentas:   ~/.local/state/xrepost/accounts.json  (cambiable con X_ACCOUNTS)

set -euo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
export X_HISTORY="${X_HISTORY:-${XDG_STATE_HOME:-$HOME/.local/state}/xrepost/history.jsonl}"

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

# --- entrada interactiva ----------------------------------------------------

# Valida una fecha AAAA-MM-DD real (incluye meses y días del calendario).
valid_date() {
  local d="$1" y m day
  [[ "$d" =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})$ ]] || return 1
  y="${BASH_REMATCH[1]}"
  m="${BASH_REMATCH[2]}"
  day="${BASH_REMATCH[3]}"
  (( 10#$y >= 1900 && 10#$y <= 2199 )) || return 1
  (( 10#$m >= 1 && 10#$m <= 12 )) || return 1
  (( 10#$day >= 1 && 10#$day <= 31 )) || return 1
  [ "$(date -d "$d" +%F 2>/dev/null)" = "$d" ]
}

# Máscara AAAA-MM-DD para dibujar el campo mientras se escribe.
format_mask() {
  local d="$1" y="${1:0:4}" m="${1:4:2}" day="${1:6:2}"
  while [ ${#y} -lt 4 ]; do y+="_"; done
  while [ ${#m} -lt 2 ]; do m+="_"; done
  while [ ${#day} -lt 2 ]; do day+="_"; done
  printf '%s-%s-%s' "$y" "$m" "$day"
}

# Campo de fecha con avance automático. $1 = etiqueta, $2 = valor por defecto.
# Los guiones se insertan solos; al completar 8 dígitos se valida y se acepta.
# Enter acepta el valor por defecto (si lo hay) o confirma una fecha completa.
# Deja el resultado en READ_DATE; devuelve 1 si no hay entrada (EOF).
read_date() {
  local label="$1" def="${2:-}" digits="" ch msg="" tty=0 drain=0 skip_def=0
  if [ -t 0 ] && [ -t 1 ]; then tty=1; fi

  if [ "$tty" = 0 ]; then
    printf '  %s' "$label"
    if [ -n "$def" ]; then printf ' [%s]' "$def"; fi
    printf ': '
  fi

  while :; do
    if [ "$tty" = 1 ]; then
      printf '\r\033[K  %s' "$label"
      if [ -n "$def" ]; then printf ' [%s]' "$def"; fi
      printf ': %s' "$(format_mask "$digits")"
      if [ -n "$msg" ]; then printf '  (%s)' "$msg"; fi
    elif [ -n "$msg" ]; then
      printf '\n  (%s)\n  %s' "$msg" "$label"
      if [ -n "$def" ]; then printf ' [%s]' "$def"; fi
      printf ': '
    fi
    msg=""

    if ! IFS= read -rsn1 ch; then
      printf '\n'
      return 1
    fi

    case "$ch" in
      "")
        if [ "$skip_def" = "1" ]; then
          skip_def=0
          msg="escribe la fecha"
          continue
        fi
        if [ -z "$digits" ] && [ -n "$def" ]; then
          digits="${def//-/}"
          break
        fi
        if [ ${#digits} -eq 8 ] && valid_date "$(format_mask "$digits")"; then
          break
        fi
        msg="fecha incompleta: faltan dígitos"
        ;;
      [0-9])
        skip_def=0
        digits+="$ch"
        ;;
      -|/|' '|:|.)
        ;;
      $'\x7f'|$'\b')
        digits="${digits%?}"
        ;;
      *)
        msg="solo se admiten números"
        ;;
    esac

    if [ ${#digits} -eq 8 ]; then
      if valid_date "$(format_mask "$digits")"; then
        drain=1
        break
      fi
      digits=""
      skip_def=1
      msg="fecha no válida; vuelve a escribirla"
    fi
  done

  READ_DATE="$(format_mask "$digits")"
  if [ "$tty" = 1 ]; then
    printf '\r\033[K  %s' "$label"
    if [ -n "$def" ]; then printf ' [%s]' "$def"; fi
    printf ': %s\n' "$READ_DATE"
  else
    printf '%s\n' "$READ_DATE"
  fi
  # Si el usuario pulsó Enter después del octavo dígito, descartarlo para que
  # no lo consuma el siguiente campo. Tras un Enter explícito no hay nada que
  # descartar: ese salto de línea ya lo consumió la lectura.
  if [ "$drain" = "1" ]; then
    IFS= read -rsn1 -t 0.15 _ 2>/dev/null || true
  fi
}

# Pregunta el alcance: solo publicaciones originales o también retweets.
ask_scope() {
  local ans
  cat <<'TXT'

Alcance de las publicaciones
  Indique qué publicaciones de las cuentas seleccionadas desea considerar:

    1) Solo publicaciones originales (recomendado)
       Se excluyen los retweets que esas cuentas hayan hecho de otras cuentas.

    2) Publicaciones originales y retweets de otras cuentas
       Se incluyen también esos retweets; al repostear, se repostea la
       publicación original, no el retweet.

TXT
  while :; do
    printf '  Seleccione una opción [1/2] (Enter = 1): '
    read -r ans || ans=""
    case "${ans:-1}" in
      1) X_SKIP_RETWEETS=1; break ;;
      2) X_SKIP_RETWEETS=0; break ;;
      *) echo "  Opción no válida: escriba 1 o 2." ;;
    esac
  done
  if [ "$X_SKIP_RETWEETS" = "1" ]; then
    echo "  → Alcance: solo publicaciones originales (retweets omitidos)."
  else
    echo "  → Alcance: publicaciones originales y retweets de otras cuentas."
  fi
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
    if [ -t 0 ] && [ -t 1 ]; then
      if ! X_USERS="$(python3 "$SELF_DIR/xrepost_accounts.py" prompt)"; then
        echo "Entrada cancelada." >&2
        exit 1
      fi
    else
      read -r -p "Usuarios (separados por coma, @ opcional): " X_USERS || X_USERS=""
    fi
    if [ -z "${X_USERS// /}" ]; then
      echo "Debes indicar al menos un usuario." >&2
      exit 1
    fi

    _today="$(date +%F)"
    echo
    echo "Fechas (AAAA-MM-DD): escribe los 8 dígitos seguidos; los guiones se añaden solos."
    echo "Enter acepta el valor entre corchetes."
    while :; do
      if ! read_date "Fecha desde" "$_today"; then
        echo "Entrada cancelada." >&2
        exit 1
      fi
      X_FROM="$READ_DATE"

      if ! read_date "Fecha hasta (Enter = la misma)" "$X_FROM"; then
        echo "Entrada cancelada." >&2
        exit 1
      fi
      X_TO="$READ_DATE"

      if [[ "$X_FROM" > "$X_TO" ]]; then
        echo "  La fecha inicial ($X_FROM) es posterior a la final ($X_TO). Inténtalo de nuevo."
        continue
      fi
      break
    done

    if [ -n "${X_SKIP_RETWEETS:-}" ]; then
      if [ "$X_SKIP_RETWEETS" = "0" ]; then
        echo
        echo "  Alcance (X_SKIP_RETWEETS=0): se incluyen retweets de otras cuentas."
      else
        echo
        echo "  Alcance (X_SKIP_RETWEETS=1): solo publicaciones originales."
      fi
    else
      ask_scope
    fi

    export X_USERS X_FROM X_TO
  fi

  if [ -z "${X_USERS:-}" ] || [ -z "${X_FROM:-}" ]; then
    echo "Faltan usuarios o fecha." >&2
    exit 1
  fi

  for _date in "$X_FROM" "$X_TO"; do
    if ! valid_date "$_date"; then
      echo "Fecha no válida: '$_date'. Formato esperado: AAAA-MM-DD." >&2
      exit 1
    fi
  done
  if [[ "$X_FROM" > "$X_TO" ]]; then
    echo "La fecha inicial ($X_FROM) es posterior a la final ($X_TO)." >&2
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

# Recordar las cuentas para las próximas sugerencias (CLI y panel web).
if [ "$MODE" = "repost" ] && [ -n "${X_USERS:-}" ]; then
  python3 "$SELF_DIR/xrepost_accounts.py" add "$X_USERS" >/dev/null 2>&1 || true
fi

if [ "$MODE" = "repost" ] && [ "$X_APPLY" != "1" ]; then
  echo
  echo "AVISO: modo DRY-RUN. Para repostear de verdad: X_APPLY=1 $0 ..."
  echo
fi

exec browser-use < "$SELF_DIR/xrepost.py"