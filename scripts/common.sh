# Shared settings for all scripts. Every value can be overridden from the environment.
# shellcheck shell=bash

BENCH_ROOT="${BENCH_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WORK="${WORK:-$BENCH_ROOT/work}"            # runs, caches, binaries, snapshots (git-ignored)
ACCOUNTS_FILE="${ACCOUNTS_FILE:-$BENCH_ROOT/accounts.yaml}"

# Versions used for the published results.
NODE_VERSION="${NODE_VERSION:-24.22.2}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.7.1-2}"
UV_VERSION="${UV_VERSION:-0.13.6}"
PINCHBENCH_REPO="${PINCHBENCH_REPO:-https://github.com/pinchbench/skill}"
PINCHBENCH_REF="${PINCHBENCH_REF:-819384a}"
BLOBFUSE_REPO="${BLOBFUSE_REPO:-https://github.com/Azure/azure-storage-fuse}"
BLOBFUSE_STOCK_REF="${BLOBFUSE_STOCK_REF:-dfbfaaece}"   # base commit of PR #2444
BLOBFUSE_PR="${BLOBFUSE_PR:-2444}"
MODEL="${MODEL:-github-copilot/gpt-5.4-nano}"

NODE_HOME="${NODE_HOME:-$HOME/.local/opt/node}"
SKILL_DIR="${SKILL_DIR:-$WORK/pinchbench}"
GOLDEN="${GOLDEN:-$WORK/golden/openclaw-home.tar}"
OPENCLAW_HOME="$HOME/.openclaw"               # OpenClaw's state root; also the BlobFuse2 mount point
BENCH_MARKER=".openclaw-blobfuse-benchmark"   # marks an OpenClaw state root created by this kit

export PATH="$NODE_HOME/bin:$HOME/.local/bin:$PATH"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

die() { echo "error: $*" >&2; exit 1; }

# Refuse to replace an OpenClaw state root this kit did not create.
require_bench_owned_openclaw_home() {
  if [[ -e "$OPENCLAW_HOME" ]] && ! mountpoint -q "$OPENCLAW_HOME" \
     && [[ -n "$(ls -A "$OPENCLAW_HOME" 2>/dev/null)" ]] && [[ ! -e "$OPENCLAW_HOME/$BENCH_MARKER" ]]; then
    die "$OPENCLAW_HOME exists and was not created by this kit. Back it up and remove it first:
       mv ~/.openclaw ~/.openclaw.backup"
  fi
}
