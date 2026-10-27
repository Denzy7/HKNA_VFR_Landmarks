# Sourced by build.sh, view.sh and shot.sh: puts the repo's Python venv (.venv/) first on PATH,
# creating it and installing requirements.txt when it's missing or requirements.txt has changed.
# VENV=/path/to/venv in build.local.sh uses another one.
VENV="${VENV:-.venv}"
if [ ! -x "$VENV/bin/python3" ]; then
    echo "creating Python venv in $VENV"
    python3 -m venv "$VENV"
fi
if ! cmp -s requirements.txt "$VENV/requirements.txt"; then
    echo "installing requirements.txt into $VENV"
    "$VENV/bin/python3" -m pip install -q --disable-pip-version-check -r requirements.txt
    cp requirements.txt "$VENV/requirements.txt"
fi
export VIRTUAL_ENV="$(readlink -f "$VENV")"
export PATH="$VIRTUAL_ENV/bin:$PATH"
