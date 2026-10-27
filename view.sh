#!/usr/bin/env bash
# Browser preview of the built pack (three.js), instead of loading X-Plane.
#   ./view.sh                   # opens at Jamia Mosque
#   ./view.sh "City Hall"       # opens at a landmark (name as in landmarks.txt)
# After another ./build.sh, press Reload (or R) in the page. Buildings removed in the page go
# to source/removed.txt; the page's Rebuild button runs ./build.sh.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ -f build.local.sh ] && . ./build.local.sh
. tools/imagery.sh
. tools/venv.sh
[ -f build/-02+036.txt ] || { echo "no build yet: run ./build.sh first"; exit 1; }
python3 -I tools/viewer/prep.py "$ORTHO_TEX"
PORT="${VIEW_PORT:-8765}"
url="http://127.0.0.1:$PORT/tools/viewer/index.html"
if [ $# -gt 0 ]; then
    url="$url#lm=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")"
fi
( sleep 1; xdg-open "$url" >/dev/null 2>&1 || true ) &
echo "viewer: $url   (Ctrl-C to stop)"
exec python3 -I tools/viewer/serve.py "$PORT"
