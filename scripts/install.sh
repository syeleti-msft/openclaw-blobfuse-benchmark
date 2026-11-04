#!/usr/bin/env bash
# Install everything the benchmark needs on Ubuntu 22.04/24.04 (x86_64):
# system packages, Node.js, OpenClaw (pinned), uv, and PinchBench (pinned + workspace patch).
# Safe to re-run.
set -euo pipefail
trap 'echo "error: install.sh failed at line $LINENO" >&2' ERR
source "$(dirname "$0")/common.sh"

echo "==> System packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  curl git jq tmux xz-utils python3 python3-venv sqlite3 strace \
  fuse3 libfuse3-dev gcc make bpftrace >/dev/null

echo "==> Node.js $NODE_VERSION"
if [[ "$(node -v 2>/dev/null)" != "v$NODE_VERSION" ]]; then
  mkdir -p "$(dirname "$NODE_HOME")"
  curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" \
    | tar -xJ -C "$(dirname "$NODE_HOME")"
  ln -sfn "node-v$NODE_VERSION-linux-x64" "$NODE_HOME"
fi
node -v

echo "==> OpenClaw $OPENCLAW_VERSION"
# Any openclaw command creates ~/.openclaw. If it does not exist yet, mark it as created by this kit
# so the other scripts may reset it; an existing ~/.openclaw is never touched.
[[ -e "$OPENCLAW_HOME" ]] || { mkdir -m 700 "$OPENCLAW_HOME"; touch "$OPENCLAW_HOME/$BENCH_MARKER"; }
if [[ "$(openclaw --version 2>/dev/null)" != *"$OPENCLAW_VERSION"* ]]; then
  # npm 11 skips install scripts unless allowed; OpenClaw's postinstall sets up bundled plugins.
  npm install -g "openclaw@$OPENCLAW_VERSION" --allow-scripts=openclaw >/dev/null
fi
openclaw --version

echo "==> uv $UV_VERSION"
if [[ "$(uv --version 2>/dev/null)" != *"$UV_VERSION"* ]]; then
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | sh >/dev/null
fi
uv --version

echo "==> PinchBench $PINCHBENCH_REF + workspace patch"
if [[ ! -d "$SKILL_DIR/.git" ]]; then
  git clone -q "$PINCHBENCH_REPO" "$SKILL_DIR"
fi
git -C "$SKILL_DIR" checkout -q "$PINCHBENCH_REF"
if git -C "$SKILL_DIR" apply --check "$BENCH_ROOT/patches/pinchbench-run-root.patch" 2>/dev/null; then
  git -C "$SKILL_DIR" apply "$BENCH_ROOT/patches/pinchbench-run-root.patch"
elif ! git -C "$SKILL_DIR" apply --reverse --check "$BENCH_ROOT/patches/pinchbench-run-root.patch" 2>/dev/null; then
  die "PinchBench working tree has unexpected changes in $SKILL_DIR"
fi
(cd "$SKILL_DIR" && uv sync -q)

# Keep the user's systemd instance (and the OpenClaw gateway) running without a login session.
sudo loginctl enable-linger "$(id -un)"

echo
echo "Done. Add Node.js to your PATH:  export PATH=$NODE_HOME/bin:\$HOME/.local/bin:\$PATH"
echo "Next: scripts/build_blobfuse2.sh, then scripts/login.sh"
