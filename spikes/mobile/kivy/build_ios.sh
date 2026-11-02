#!/usr/bin/env bash
# Build the Kivy PoC for the iOS simulator with kivy-ios.
#
# kivy-ios compiles its own CPython (3.11.6 in kivy-ios 2025.5.17), SDL2 and
# Kivy from source: it does NOT use python.org's official iOS framework, and
# Python 3.15 is not available there. First build: ~30-60 min.
#
# Needs: Xcode, Homebrew autoconf + automake + pkg-config, uv.
# KIVY_IOS_DIR holds the toolchain's (large) build tree; default ./.kivy-ios.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$PWD"
KIVY_IOS_DIR="${KIVY_IOS_DIR:-$HERE/.kivy-ios}"
mkdir -p "$KIVY_IOS_DIR"
cd "$KIVY_IOS_DIR"

if [ ! -x venv/bin/toolchain ]; then
    uv venv -q -p 3.11 venv
    VIRTUAL_ENV=venv uv pip install -q pip kivy-ios==2025.5.17 "cython<3.1"
fi
export PATH="$KIVY_IOS_DIR/venv/bin:$PATH"

toolchain build python3 kivy openssl libffi

# Pure-Python deps only, --no-deps: the same closure as the other PoCs, plus
# pygments for CodeInput.
toolchain pip install --no-deps \
    bigfix-relevance-analyzer==1.39.2 \
    bigfix-remote-session-relevance==1.3.0 \
    bigfix-remote-client-relevance==1.9.0 \
    anyio==4.15.1 beautifulsoup4==4.15.0 certifi==2026.10.19 \
    charset-normalizer==3.5.2 defusedxml==0.7.1 filelock==4.2.4 h11==0.16.0 \
    httpcore==1.0.9 httpx==0.28.1 idna==3.20 platformdirs==4.13.5 \
    requests==2.34.2 soupsieve==3.0.4 tomlkit==0.15.2 \
    typing-extensions==4.16.0 urllib3==2.8.0 pygments

# The app dir kivy-ios bundles: main.py plus the shared core.
APP="$KIVY_IOS_DIR/app"
rm -rf "$APP" && mkdir -p "$APP"
cp "$HERE/main.py" "$APP/"
cp -R "$HERE/../core/src/bfrd_mobile_core" "$APP/"

[ -d bigfixrelevance-ios ] || toolchain create BigFixRelevance "$APP"
toolchain update bigfixrelevance-ios/bigfixrelevance.xcodeproj

xcodebuild -project bigfixrelevance-ios/bigfixrelevance.xcodeproj \
    -scheme bigfixrelevance -configuration Debug \
    -destination "platform=iOS Simulator,name=${SIM_DEVICE:-iPhone 16 Pro}" \
    -derivedDataPath build-xcode build
echo "Built: $(find build-xcode/Build/Products -name '*.app' -maxdepth 2 | head -1)"
