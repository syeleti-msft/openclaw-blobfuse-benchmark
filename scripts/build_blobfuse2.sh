#!/usr/bin/env bash
# Build the two BlobFuse2 binaries compared in the benchmark:
#   work/bin/blobfuse2-stock    Azure/azure-storage-fuse at $BLOBFUSE_STOCK_REF (base of PR #2444)
#   work/bin/blobfuse2-pr2444   the same plus PR #2444 (skip redundant chmod via the attribute cache)
# Installs Go (version from go.mod) under ~/.local/opt/go if no suitable Go is on PATH.
set -euo pipefail
trap 'echo "error: build_blobfuse2.sh failed at line $LINENO" >&2' ERR
source "$(dirname "$0")/common.sh"

SRC="$WORK/src/azure-storage-fuse"
mkdir -p "$WORK/bin" "$WORK/src"
if [[ ! -d "$SRC/.git" ]]; then
  git clone -q "$BLOBFUSE_REPO" "$SRC"
fi
git -C "$SRC" fetch -q origin "pull/$BLOBFUSE_PR/head:pr-$BLOBFUSE_PR" --force
git -C "$SRC" fetch -q origin main

need_go="$(git -C "$SRC" show "pr-$BLOBFUSE_PR:go.mod" | awk '/^go /{print $2}')"
have_go="$(command -v go >/dev/null && go env GOVERSION | sed 's/^go//' || true)"
if [[ -z "$have_go" ]] || [[ "$(printf '%s\n%s\n' "$need_go" "$have_go" | sort -V | head -1)" != "$need_go" ]]; then
  echo "==> Installing Go $need_go"
  mkdir -p "$HOME/.local/opt"
  rm -rf "$HOME/.local/opt/go"
  curl -fsSL "https://go.dev/dl/go$need_go.linux-amd64.tar.gz" | tar -xz -C "$HOME/.local/opt"
  export PATH="$HOME/.local/opt/go/bin:$PATH"
fi
go version

build() {
  local ref="$1" out="$2"
  echo "==> Building $out from $ref"
  git -C "$SRC" checkout -q --detach "$ref"
  (cd "$SRC" && ./build.sh >/dev/null)
  cp "$SRC/blobfuse2" "$WORK/bin/$out"
  echo "    $("$WORK/bin/$out" --version 2>/dev/null | head -1)  sha256 $(sha256sum "$WORK/bin/$out" | cut -c1-12)"
}
build "$BLOBFUSE_STOCK_REF" blobfuse2-stock
build "pr-$BLOBFUSE_PR" "blobfuse2-pr$BLOBFUSE_PR"
