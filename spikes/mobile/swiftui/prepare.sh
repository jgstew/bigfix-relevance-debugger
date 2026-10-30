#!/usr/bin/env bash
# Fetch the official python.org iOS framework and stage the Python side of the
# SwiftUI app. Pure-Python wheels are platform independent, so installing
# them with the host's uv into app_packages/ is all iOS needs.
set -euo pipefail
cd "$(dirname "$0")"

PY_VERSION="${PY_VERSION:-3.15.2}"
TARBALL="python-${PY_VERSION}-iOS-XCframework.tar.gz"
URL="https://www.python.org/ftp/python/${PY_VERSION}/${TARBALL}"

if [ ! -d Python.xcframework ]; then
    mkdir -p .cache
    [ -f ".cache/${TARBALL}" ] || curl -fL -o ".cache/${TARBALL}" "$URL"
    # python.org publishes a .sha256 next to each artefact? It publishes
    # Sigstore bundles; verify with `python -m sigstore verify` in real builds.
    tar -xzf ".cache/${TARBALL}" Python.xcframework
fi

rm -rf app app_packages
mkdir -p app
cp -R ../core/src/bfrd_mobile_core app/
find app -name __pycache__ -prune -exec rm -rf {} +

# --no-deps + an explicit pure-Python closure: the client library's own deps
# (docker, asyncssh/cryptography, zstandard) are compiled and unused here.
uv pip install --quiet --target app_packages --no-deps --no-compile \
    --python-version "${PY_VERSION%.*}" \
    bigfix-relevance-analyzer==1.39.2 \
    bigfix-remote-session-relevance==1.3.0 \
    bigfix-remote-client-relevance==1.9.0 \
    anyio==4.15.1 beautifulsoup4==4.15.0 certifi==2026.10.19 \
    charset-normalizer==3.5.2 defusedxml==0.7.1 filelock==4.2.4 h11==0.16.0 \
    httpcore==1.0.9 httpx==0.28.1 idna==3.20 platformdirs==4.13.5 \
    requests==2.34.2 soupsieve==3.0.4 tomlkit==0.15.2 \
    typing-extensions==4.16.0 urllib3==2.8.0 \
    --only-binary :all: --python-platform aarch64-apple-darwin

# charset-normalizer has an optional compiled speedup: drop any host .so so
# only pure Python ships (it falls back automatically).
find app_packages -name "*.so" -print -delete

echo "Staged: $(ls app_packages | grep -c dist-info) packages, Python ${PY_VERSION}"
