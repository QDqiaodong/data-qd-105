#!/usr/bin/env bash
# Build the offline release bundle for an air-gapped survey workstation.
#
# This runs on a CONNECTED preparation machine whose interpreter matches the
# target workstation's CPython minor version, OS and architecture. Nothing
# here touches an installed project environment or repository output
# directories: all artifacts are produced under the work directory, and the
# bundle itself is assembled by the stdlib-only offline_release packer from
# the prepared wheel staging directory.
#
# Usage:
#   scripts/build_offline_bundle.sh WORK_DIR [BUNDLE_DIR]
# Environment:
#   PYTHON       prep interpreter (default: python3)
#   PIP_VERSION  pip wheel to embed (default: 24.3.1)
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
WORK_DIR=${1:?"usage: build_offline_bundle.sh WORK_DIR [BUNDLE_DIR]"}
BUNDLE_DIR=${2:-"$WORK_DIR/release-bundle"}
PYTHON=${PYTHON:-python3}
PIP_VERSION=${PIP_VERSION:-24.3.1}

STAGING="$WORK_DIR/wheel-staging"
INPUTS="$WORK_DIR/inputs-stage"
VENV="$WORK_DIR/build-venv"

mkdir -p "$STAGING" "$INPUTS"

# 1. Build virtual environment. Prefer ensurepip; fall back to get-pip.py on
#    stripped interpreters (e.g. Debian python3 without python3-venv extras).
if [ ! -x "$VENV/bin/python" ]; then
    "$PYTHON" -I -m venv "$VENV" 2>/dev/null || "$PYTHON" -I -m venv --without-pip "$VENV"
fi
if ! "$VENV/bin/python" -c "import pip" >/dev/null 2>&1; then
    GET_PIP="$WORK_DIR/get-pip.py"
    if [ ! -f "$GET_PIP" ]; then
        curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$GET_PIP"
    fi
    "$VENV/bin/python" -I "$GET_PIP"
fi

# 2. Pin the build backend exactly as recorded in requirements-build.lock.
"$VENV/bin/python" -I -m pip install --no-cache-dir \
    -r "$PROJECT_DIR/requirements-build.lock"

# 3. Locked runtime wheels, binary wheels only (no source distributions are
#    allowed into a bundle), for this interpreter's platform.
"$VENV/bin/python" -I -m pip download --no-cache-dir --only-binary=:all: \
    -r "$PROJECT_DIR/requirements.lock" -d "$STAGING"

# 4. The embedded pip wheel lets the target install without local pip.
"$VENV/bin/python" -I -m pip download --no-cache-dir --only-binary=:all: \
    "pip==$PIP_VERSION" -d "$STAGING"

# 5. Project wheel, built without build isolation using the pinned backend.
"$VENV/bin/python" -I -m pip wheel --no-cache-dir --no-deps \
    --no-build-isolation --wheel-dir "$STAGING" "$PROJECT_DIR"

# 6. Synthetic CLI inputs (the only data the bundle needs).
cp "$PROJECT_DIR"/data/*.json "$PROJECT_DIR"/data/*.geojson "$INPUTS"/

# 7. Assemble the bundle. The packer is pure stdlib; invoke it in script
#    mode under -I so it runs with a bare interpreter (no pip, no cwd path
#    assumptions, no site-packages), proving target-side compatibility.
"$PYTHON" -I "$PROJECT_DIR/offline_release/__main__.py" pack \
    --wheel-dir "$STAGING" \
    --input-dir "$INPUTS" \
    --pyproject "$PROJECT_DIR/pyproject.toml" \
    --requirements-lock "$PROJECT_DIR/requirements.lock" \
    --output "$BUNDLE_DIR"

echo "bundle ready at: $BUNDLE_DIR"
