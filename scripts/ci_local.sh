#!/usr/bin/env bash
# Approximate CI (.github/workflows/test.yaml and pre-commit.yaml) on this
# machine, for when GitHub Actions is down or slow. Not a replacement for it.
#
#   scripts/ci_local.sh            # every job below
#   scripts/ci_local.sh test-3.11 smoke-macos   # just these
#
# Jobs:
#   test-3.11, test-3.13  test.yaml's `test` matrix, through act
#   pre-commit            pre-commit.yaml through act, on all files
#   build                 `uv build` of the wheel the smoke jobs test
#   smoke-ubuntu          the wheel smoke test in a python:3.13-slim container
#   smoke-macos           the wheel smoke test natively (macOS hosts only)
#
# How it differs from CI:
#   - Windows' smoke test can't run here (act and Docker run only Linux). It's
#     reported as skipped, never as passing.
#   - act runs the test jobs one at a time: in parallel they share act's
#     Python tool cache and corrupt each other's install.
#   - build and the smoke jobs run outside act, because act's artifact server
#     isn't reachable from its containers on Docker Desktop, so the
#     build -> smoke hand-off fails there.
#   - containers use the host's architecture (arm64 on Apple silicon); CI's
#     ubuntu-latest is x86_64.
#   - the smoke steps are copied from test.yaml's `smoke` job; keep them in
#     step with it.
#
# Needs: act, docker (running), uv. Logs go to a temp directory, printed at
# the end.
#
# shellcheck disable=SC2016,SC2329
# (SMOKE_SCRIPT is single-quoted on purpose: it expands where it runs; and the
# job functions are called through run().)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

ALL_JOBS=(test-3.11 test-3.13 pre-commit build smoke-ubuntu smoke-macos)
ACT_IMAGE="ubuntu-latest=catthehacker/ubuntu:act-latest"
SMOKE_IMAGE="python:3.13-slim"

usage() {
    # the header comment, up to the shellcheck directive
    awk 'NR > 1 && /^# shellcheck/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

case "${1:-}" in
    -h | --help)
        usage
        exit 0
        ;;
esac

JOBS=("$@")
if [ ${#JOBS[@]} -eq 0 ]; then
    JOBS=("${ALL_JOBS[@]}")
fi
for job in "${JOBS[@]}"; do
    case " ${ALL_JOBS[*]} " in
        *" $job "*) ;;
        *)
            echo "unknown job: $job (jobs: ${ALL_JOBS[*]})" >&2
            exit 2
            ;;
    esac
done

wants() {
    case " ${JOBS[*]} " in *" $1 "*) return 0 ;; esac
    return 1
}

missing=()
for tool in act docker uv; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "missing: ${missing[*]} (e.g. brew install ${missing[*]})" >&2
    exit 2
fi
if ! docker info >/dev/null 2>&1; then
    echo "docker isn't running" >&2
    exit 2
fi

# The uv version CI installs: the `# X.Y.Z` comment on astral-sh/uv-pre-commit's
# rev, as .github/actions/install-uv reads it.
UV_VERSION="$(awk '
    /^[[:space:]]*-[[:space:]]*repo:.*astral-sh\/uv-pre-commit[[:space:]]*$/ { found = 1; next }
    found && $1 == "rev:" {
        if (match($0, /#[ \t]*(frozen:[ \t]*)?[vV]?[0-9]+(\.[0-9]+)*/)) {
            ver = substr($0, RSTART, RLENGTH)
            sub(/^#[ \t]*(frozen:[ \t]*)?[vV]?/, "", ver)
        } else {
            ver = $2
            gsub(/["'\'']/, "", ver)
            sub(/^[vV]/, "", ver)
        }
        print ver
        exit
    }
' .pre-commit-config.yaml)"
if [ -z "$UV_VERSION" ]; then
    echo "could not read the uv version from .pre-commit-config.yaml" >&2
    exit 2
fi

LOGS="$(mktemp -d "${TMPDIR:-/tmp}/ci_local.XXXXXX")"
DIST="$LOGS/dist"
declare -a RESULTS=()

# run <job> <command...>: log the command's output and record PASS/FAIL.
run() {
    local job="$1"
    shift
    local log="$LOGS/$job.log"
    printf '%-14s running ... ' "$job"
    local start=$SECONDS
    if "$@" >"$log" 2>&1; then
        RESULTS+=("$job|PASS|$((SECONDS - start))s")
        echo "PASS ($((SECONDS - start))s)"
    else
        RESULTS+=("$job|FAIL|$((SECONDS - start))s, see $log")
        echo "FAIL ($((SECONDS - start))s): $log"
    fi
}

skip() {
    RESULTS+=("$1|SKIP|$2")
    printf '%-14s SKIP: %s\n' "$1" "$2"
}

act_job() {
    act workflow_dispatch -P "$ACT_IMAGE" "$@"
}

# The smoke test's two steps from test.yaml, against the wheel in $1. Runs
# under bash in the container and on the host alike.
SMOKE_SCRIPT='
set -euo pipefail
wheel="$(ls "$1"/*.whl)"
expected="$(uv run --isolated --no-project python -c "import pathlib, tomllib; print(tomllib.loads(pathlib.Path(\"pyproject.toml\").read_text())[\"project\"][\"version\"])")"
got="$(uvx --isolated --from "$wheel" bigfix-relevance-debugger --version | tr -d "\r")"
echo "$got"
if [ "$got" != "bigfix-relevance-debugger ${expected}" ]; then
    echo "expected bigfix-relevance-debugger ${expected}, got ${got}" >&2
    exit 1
fi
uvx --isolated --from "$wheel" bigfix-relevance-debugger --help
uv run --isolated --no-project --with "$wheel" python - <<"EOF"
import importlib
import platform
from importlib.metadata import version

import webview  # noqa: F401

from bigfix_relevance_debugger import app

backend = {
    "Windows": "webview.platforms.winforms",
    "Darwin": "webview.platforms.cocoa",
}.get(platform.system())
if backend:
    importlib.import_module(backend)
    problem = app.native_window_problem()
    assert problem is None, f"native window unavailable: {problem}"
print("pywebview", version("pywebview") + ", backend:", backend or "none (browser tab)")
EOF
'

smoke_ubuntu() {
    # Read-only repo for pyproject.toml; uv pinned like install-uv does.
    docker run --rm \
        -v "$ROOT":/repo:ro -v "$DIST":/dist:ro -w /repo \
        -e UV_PYTHON=3.13 -e UV_VERSION="$UV_VERSION" -e SMOKE_SCRIPT="$SMOKE_SCRIPT" \
        "$SMOKE_IMAGE" bash -c \
        'python -m pip install -q --root-user-action=ignore "uv==${UV_VERSION}" && bash -c "$SMOKE_SCRIPT" smoke /dist'
}

smoke_macos() {
    UV_PYTHON=3.13 bash -c "$SMOKE_SCRIPT" smoke "$DIST"
}

echo "uv $UV_VERSION (from .pre-commit-config.yaml), logs in $LOGS"

for python in 3.11 3.13; do
    if wants "test-$python"; then
        run "test-$python" act_job -W .github/workflows/test.yaml -j test --matrix "python:$python"
    fi
done

if wants pre-commit; then
    run pre-commit act_job -W .github/workflows/pre-commit.yaml
fi

# The smoke jobs need the wheel, so asking for one builds it.
if wants build || wants smoke-ubuntu || wants smoke-macos; then
    run build uv build --out-dir "$DIST"
fi
built=false
if compgen -G "$DIST/*.whl" >/dev/null; then
    built=true
fi

if wants smoke-ubuntu; then
    if $built; then
        run smoke-ubuntu smoke_ubuntu
    else
        skip smoke-ubuntu "no wheel (build failed)"
    fi
fi

if wants smoke-macos; then
    if [ "$(uname -s)" != Darwin ]; then
        skip smoke-macos "not a macOS host"
    elif $built; then
        run smoke-macos smoke_macos
    else
        skip smoke-macos "no wheel (build failed)"
    fi
fi

skip smoke-windows "can't run locally (act and Docker run only Linux)"

echo
echo "Summary (logs in $LOGS):"
failed=0
for row in "${RESULTS[@]}"; do
    IFS='|' read -r job status detail <<<"$row"
    printf '  %-14s %-4s  %s\n' "$job" "$status" "$detail"
    [ "$status" = FAIL ] && failed=1
done
exit "$failed"
