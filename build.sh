#!/usr/bin/env bash
# Rebuild the HKNA_VFR_Landmarks scenery pack in place (objects/, Earth nav data/).
#   ./build.sh
# Local paths go in build.local.sh (not in git), e.g.
#   ORTHO_TEX="/path/to/Ortho4XP/Tiles/zOrtho4XP_-02+036/textures"
#   XP_CUSTOM_SCENERY="/path/to/X-Plane 11/Custom Scenery"     # optional (simHeaven checks)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

[ -f build.local.sh ] && . ./build.local.sh
# imagery the models take colours/roofs from: go2 (Ortho4XP's textures, the sim's own ground)
# or open (Maxar 30 cm via OpenAerialMap, CC BY-NC), falling back to open without ORTHO_TEX;
# see README "Imagery"
. tools/imagery.sh
. tools/venv.sh
XPTOOLS="${XPTOOLS:-$HOME/.cache/xptools}"
# installed scenery, read at build time so simHeaven's duplicate turbines can be excluded and
# its hand-made models protected (nothing from simHeaven is stored in this repo)
export XP_CUSTOM_SCENERY="${XP_CUSTOM_SCENERY:-}"

if [ ! -x "$XPTOOLS/tools/DSFTool" ] || [ ! -x "$XPTOOLS/tools/DDSTool" ]; then
    echo "fetching X-Plane scenery tools into $XPTOOLS"
    mkdir -p "$XPTOOLS"
    curl -sSL -o "$XPTOOLS/xptools.zip" https://files.x-plane.com/public/xptools/xptools_lin_24-5.zip
    unzip -oq "$XPTOOLS/xptools.zip" -d "$XPTOOLS"
    chmod +x "$XPTOOLS"/tools/*
fi

# cached: only queries that are new/changed hit Overpass
python3 -I -u source/osm_fetch.py source/osm

rm -rf build
DSFTOOL="$XPTOOLS/tools/DSFTool" \
    python3 -I -u source/build.py source/osm build "$ORTHO_TEX" "$XPTOOLS/tools/DDSTool"
for txt in build/-??+???.txt; do
    tile=$(basename "$txt" .txt)
    "$XPTOOLS/tools/DSFTool" --text2dsf "$txt" "build/Earth nav data/-10+030/$tile.dsf" >/dev/null
    echo "compiled $tile.dsf"
done

rm -rf objects "Earth nav data"
mv build/objects build/"Earth nav data" .
mv build/landmarks.txt .

# landmark coordinates table into README.md
python3 -I - build/landmarks.md README.md <<'PY'
import re, sys
table = open(sys.argv[1]).read()
readme = open(sys.argv[2]).read()
readme = re.sub(r"(<!-- landmarks:begin -->\n).*?(<!-- landmarks:end -->)",
                lambda m: m.group(1) + table.lstrip("\n") + "\n" + m.group(2), readme, flags=re.S)
open(sys.argv[2], "w").write(readme)
PY
echo "done: $(ls objects/*.obj | wc -l) objects; restart X-Plane to load"

# browser viewer data (./view.sh), so a running viewer only needs Reload
python3 -I -u tools/viewer/prep.py "$ORTHO_TEX"
