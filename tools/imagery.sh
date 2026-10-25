# Sourced by build.sh and view.sh (after build.local.sh): settles IMAGERY and ORTHO_TEX.
#   IMAGERY unset: go2 if the Ortho4XP tile's textures are there, else open (with a note)
#   IMAGERY=go2:   needs ORTHO_TEX; stops if it's missing
#   IMAGERY=open:  OpenAerialMap tiles, ORTHO_TEX not needed
ortho_hint() {
    cat >&2 <<'EOF'
  Better: build the -02+036 tile with Ortho4XP (provider GO2) and set ORTHO_TEX in build.local.sh,
  e.g. ORTHO_TEX="/path/to/Ortho4XP/Tiles/zOrtho4XP_-02+036/textures". The models then take their
  roofs and colours from the very imagery X-Plane drapes under them, and nothing has to come from
  OpenAerialMap's slow, rate-limited tile server (a few thousand tiles on the first build).
EOF
}

if [ -n "${ORTHO_TEX:-}" ] && [ ! -d "$ORTHO_TEX" ]; then
    echo "ORTHO_TEX=$ORTHO_TEX is not a directory" >&2
    ORTHO_TEX=""
fi
case "${IMAGERY:-}" in
    "")
        if [ -n "${ORTHO_TEX:-}" ]; then
            IMAGERY=go2
        else
            echo "imagery: no Ortho4XP textures (ORTHO_TEX), using open imagery: Maxar via OpenAerialMap" >&2
            ortho_hint
            IMAGERY=open
        fi ;;
    go2)
        if [ -z "${ORTHO_TEX:-}" ]; then
            echo "IMAGERY=go2 needs ORTHO_TEX: the Ortho4XP tile's textures folder for -02+036" >&2
            ortho_hint
            echo "  (or set IMAGERY=open to use OpenAerialMap instead)" >&2
            exit 1
        fi ;;
    open) ;;
    *)
        echo "IMAGERY must be go2 or open, not '$IMAGERY'" >&2
        exit 1 ;;
esac
export IMAGERY
ORTHO_TEX="${ORTHO_TEX:-}"
