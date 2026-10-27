#!/usr/bin/env bash
# Headless screenshot of the viewer (for checking a build without opening a browser).
#   tools/viewer/shot.sh out.png "lm=Jamia Mosque&az=160&el=20" [WxH]
# Needs chromium and an up-to-date tools/viewer/data (./view.sh or ./build.sh writes it).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
out="$(readlink -f "$1")"; hash="${2:-}"; size="${3:-1400,900}"
[ -f build.local.sh ] && . ./build.local.sh
. tools/venv.sh
PORT="${SHOT_PORT:-8766}"
python3 -I tools/viewer/serve.py "$PORT" >/dev/null 2>&1 &
srv=$!
trap 'kill $srv 2>/dev/null' EXIT
sleep 0.5
# hardware only: the Intel HD 520 iGPU via ANGLE/EGL (the Radeon stays with X-Plane), and no
# silent fallback to Chromium's CPU renderer
DRI_PRIME=0 chromium --headless=new --use-angle=gl-egl --ignore-gpu-blocklist --disable-software-rasterizer --hide-scrollbars \
    --window-size="$size" --virtual-time-budget=60000 --screenshot="$out" \
    "http://127.0.0.1:$PORT/tools/viewer/index.html#${hash// /%20}" >/dev/null 2>&1
echo "$out"
