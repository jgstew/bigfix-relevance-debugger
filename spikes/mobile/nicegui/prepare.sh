#!/usr/bin/env bash
# Stage the Python side of the NiceGUI-on-iOS spike (simulator build).
#
#   1. python.org's Python 3.15 iOS XCframework
#   2. pydantic-core cross-compiled for iOS (build_pydantic_core.sh)
#   3. NiceGUI + the debugger's dependencies, pure-Python wheels for iOS,
#      --no-deps, minus the compiled ones that have no iOS wheel and that
#      NiceGUI/uvicorn don't need (see the README)
#   4. this repo's own wheel, unchanged, into app_packages/
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(cd ../../.. && pwd)"

PY_VERSION="${PY_VERSION:-3.15.2}"
TARBALL="python-${PY_VERSION}-iOS-XCframework.tar.gz"
PLATFORM="${IOS_PLATFORM:-arm64-apple-ios-simulator}"   # or arm64-apple-ios (then: ./build_pydantic_core.sh device)

mkdir -p .cache
if [ ! -d Python.xcframework ]; then
    [ -f ".cache/$TARBALL" ] || curl -fL -o ".cache/$TARBALL" "https://www.python.org/ftp/python/$PY_VERSION/$TARBALL"
    tar -xzf ".cache/$TARBALL" Python.xcframework
fi

# The desktop closure of NiceGUI + the debugger's libraries, resolved by uv
# (repo's exclude-newer policy applies), then filtered to what iOS can take.
cat > .cache/wanted.in <<'EOF'
nicegui==3.18.0
bigfix-relevance-analyzer==1.39.2
bigfix-remote-session-relevance==1.3.0
bigfix-remote-client-relevance==1.9.0
EOF
uv pip compile .cache/wanted.in --quiet --no-header --no-annotate \
    --python-version 3.15 --python-platform aarch64-apple-darwin > .cache/desktop.txt
# No iOS wheels, and not needed:
#   orjson          NiceGUI falls back to the stdlib json
#   uvloop, httptools, watchfiles, pyyaml   uvicorn[standard] extras; plain uvicorn uses asyncio + h11
#   pydantic-core   cross-compiled below (build_pydantic_core.sh)
#   asyncssh, cryptography, cffi, pycparser, zstandard   client library's ssh/container paths (lazy imports)
grep -v -E '^(orjson|uvloop|httptools|watchfiles|pyyaml|pydantic-core|asyncssh|cryptography|cffi|pycparser|zstandard)==' \
    .cache/desktop.txt > .cache/ios.txt

# pydantic pins pydantic-core exactly, so build the version the resolver chose.
PYDANTIC_CORE_VERSION=$(sed -n 's/^pydantic-core==//p' .cache/desktop.txt)
export PYDANTIC_CORE_VERSION
[ -d ".cache/pydantic_core-sim-$PYDANTIC_CORE_VERSION" ] || ./build_pydantic_core.sh sim

rm -rf app_packages && mkdir -p app_packages
uv pip install --quiet --target app_packages --no-deps --only-binary :all: --no-compile \
    --python-version 3.15 --python-platform "$PLATFORM" -r .cache/ios.txt
cp -R ".cache/pydantic_core-sim-$PYDANTIC_CORE_VERSION/." app_packages/

# This repo's wheel, exactly as `uvx bigfix-relevance-debugger` would install it.
rm -rf .cache/dist && uv build --quiet --wheel --out-dir .cache/dist "$REPO"
uv pip install --quiet --target app_packages --no-deps --no-compile .cache/dist/bigfix_relevance_debugger-*.whl

# Any host-built .so left by a wheel with an optional speedup would crash on iOS.
find app_packages -name "*.so" ! -name "*iphone*" -print -delete

echo "Staged $(ls app_packages | grep -c dist-info) packages for $PLATFORM"
