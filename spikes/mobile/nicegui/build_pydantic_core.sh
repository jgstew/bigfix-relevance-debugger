#!/usr/bin/env bash
# Cross-compile pydantic-core (Rust/PyO3) for iOS against python.org's
# Python 3.15 iOS XCframework. Nobody publishes iOS wheels for it (not PyPI,
# not BeeWare's index), and FastAPI -> pydantic v2 can't import without it.
#
# Needs rustup (Homebrew's `rust` can't add iOS targets):
#   brew install rustup && rustup toolchain install stable --profile minimal
#   rustup target add aarch64-apple-ios aarch64-apple-ios-sim
#
# maturin refuses this build (it doesn't treat macOS -> iOS as cross), so
# cargo is driven directly; PyO3's build script does see it as cross and reads
# the target's _sysconfigdata from PYO3_CROSS_LIB_DIR.
#
#   PYDANTIC_CORE_VERSION=x.y.z ./build_pydantic_core.sh [sim|device]
#       -> .cache/pydantic_core-<kind>-<version>/{pydantic_core/, pydantic_core-<version>.dist-info/}
set -euo pipefail
cd "$(dirname "$0")"

KIND="${1:-sim}"
VERSION="${PYDANTIC_CORE_VERSION:-2.50.2}"
XCF="${XCFRAMEWORK:-$PWD/Python.xcframework}"
case "$KIND" in
    sim)    TARGET=aarch64-apple-ios-sim; SLICE=ios-arm64_x86_64-simulator; SOABI=cpython-315-iphonesimulator ;;
    device) TARGET=aarch64-apple-ios;     SLICE=ios-arm64;                  SOABI=cpython-315-iphoneos ;;
    *) echo "usage: $0 [sim|device]" >&2; exit 2 ;;
esac
LIB="$XCF/$SLICE/lib-arm64"   # holds libpython3.15.dylib -> ../Python.framework/Python and _sysconfigdata

export PATH="$(brew --prefix rustup)/bin:$PATH"
export RUSTUP_TOOLCHAIN=stable
export PYO3_CROSS_LIB_DIR="$LIB/python3.15"
export PYO3_CROSS_PYTHON_VERSION=3.15
export IPHONEOS_DEPLOYMENT_TARGET=13.0
# PyO3 links -lpython3.15; that symlink's install name is
# @rpath/Python.framework/Python, the same as CPython's own iOS modules.
rustflags_var="CARGO_TARGET_$(echo "$TARGET" | tr 'a-z-' 'A-Z_')_RUSTFLAGS"
export "$rustflags_var=-L $LIB"

mkdir -p .cache
SRC=".cache/pydantic_core-$VERSION"
if [ ! -d "$SRC" ]; then
    url=$(curl -fsSL "https://pypi.org/pypi/pydantic-core/$VERSION/json" \
        | python3 -c "import sys,json;print(next(f['url'] for f in json.load(sys.stdin)['urls'] if f['filename'].endswith('.tar.gz')))")
    curl -fsSL "$url" | tar -xz -C .cache
fi

(cd "$SRC" && cargo build --release --lib --target "$TARGET")

OUT=".cache/pydantic_core-$KIND-$VERSION"
rm -rf "$OUT" && mkdir -p "$OUT"
cp -R "$SRC/python/pydantic_core" "$OUT/"
cp "$SRC/target/$TARGET/release/lib_pydantic_core.dylib" "$OUT/pydantic_core/_pydantic_core.$SOABI.so"
install_name_tool -id "_pydantic_core.$SOABI.so" "$OUT/pydantic_core/_pydantic_core.$SOABI.so"
# Minimal metadata so importlib.metadata finds it like an installed wheel.
mkdir -p "$OUT/pydantic_core-$VERSION.dist-info"
printf 'Metadata-Version: 2.1\nName: pydantic_core\nVersion: %s\n' "$VERSION" \
    > "$OUT/pydantic_core-$VERSION.dist-info/METADATA"
echo "Built $OUT ($(vtool -show-build "$OUT/pydantic_core/_pydantic_core.$SOABI.so" | awk '/platform/{print $2}'))"
