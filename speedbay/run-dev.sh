#!/bin/sh
# Start the Open SWE backend with a working local sandbox.
#
# The `local` sandbox backend runs commands on the host with the parent
# process's environment (LocalShellBackend(..., inherit_env=True)), so exporting
# these here is what makes git and gh authenticate inside agent runs — no
# upstream code changes required.
#
#   PATH              puts speedbay/bin first, so our `gh` shim wins over the
#                     real gh and can replace the hardcoded GH_TOKEN=dummy.
#   GIT_CONFIG_GLOBAL points git at speedbay/gitconfig, which registers the
#                     credential helper for github.com.
#   LOCAL_SANDBOX_ROOT_DIR keeps agent file writes out of the checkout.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
SCRATCH="${LOCAL_SANDBOX_ROOT_DIR:-/tmp/openswe-scratch}"

mkdir -p "$SCRATCH"

PATH="$HERE/bin:$PATH"
GIT_CONFIG_GLOBAL="$HERE/gitconfig"
LOCAL_SANDBOX_ROOT_DIR="$SCRATCH"
export PATH GIT_CONFIG_GLOBAL LOCAL_SANDBOX_ROOT_DIR

# core.hooksPath is injected here rather than written into gitconfig because it
# needs an absolute path that differs per checkout. GIT_CONFIG_COUNT layers extra
# config on top of the files git already reads.
GIT_CONFIG_COUNT=1
GIT_CONFIG_KEY_0=core.hooksPath
GIT_CONFIG_VALUE_0="$HERE/githooks"
export GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0

echo "sandbox root : $LOCAL_SANDBOX_ROOT_DIR"
echo "gh shim      : $(command -v gh)"
echo "git config   : $GIT_CONFIG_GLOBAL"
echo "git hooks    : $GIT_CONFIG_VALUE_0"

# --no-reload: langgraph dev's file watcher sees the runtime's own
# .langgraph_api/*.pckl persistence flushes (~every 10s), hot-reloads itself
# in a loop, and eventually dies mid-reload. This is an operational server,
# not active development — restart via speedbay/openswe after code changes.
exec "$ROOT/.venv/bin/langgraph" dev --no-browser --no-reload "$@"
