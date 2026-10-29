#!/usr/bin/env bash
# Arranca el panel web local de xrepost.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/app.py" "$@"
