#!/usr/bin/env bash
# Backport openclaw/openclaw#178107 into the installed OpenClaw 2026.7.1-2 release, in place.
# The change: applyPrivateModeSync() skips chmod when the path already has the requested mode
# (see openclaw-178107-backport.diff). Patch the installed package rather than a copy: OpenClaw
# started from another install directory adds bundled plugin skills to the agent prompt, which
# changes agent behaviour and invalidates comparisons.
# usage: openclaw-178107.sh apply|revert|status
set -euo pipefail
NODE_HOME="${NODE_HOME:-$HOME/.local/opt/node}"
PKG="${OPENCLAW_PKG:-$NODE_HOME/lib/node_modules/openclaw}"
MARK="$PKG/.openclaw-178107-backport"

case "${1:-status}" in
  apply)
    [[ -f "$MARK" ]] && { echo "already applied"; exit 0; }
    mapfile -t files < <(grep -rl "function applyPrivateModeSync(target, mode) {" "$PKG/dist")
    [[ ${#files[@]} -eq 1 ]] || { echo "expected 1 file with applyPrivateModeSync, found ${#files[@]}" >&2; exit 1; }
    f="${files[0]}"
    grep -qE '^import \{[^}]*\bstatSync\b[^}]*\} from "node:fs"' "$f" || { echo "statSync is not imported in $f" >&2; exit 1; }
    cp -p "$f" "$f.orig-178107"
    python3 - "$f" <<'EOF'
import sys
p = sys.argv[1]
s = open(p).read()
old = "function applyPrivateModeSync(target, mode) {\n\ttry {\n"
new = ("function applyPrivateModeSync(target, mode) {\n"
       "\tif ((statSync(target).mode & 4095) === mode) return { applied: true };\n"
       "\ttry {\n")
if s.count(old) != 1:
    sys.exit("unexpected applyPrivateModeSync shape; is this OpenClaw 2026.7.1-2?")
open(p, "w").write(s.replace(old, new))
EOF
    printf '%s\n' "$f" > "$MARK"
    echo "applied to $f"
    ;;
  revert)
    [[ -f "$MARK" ]] || { echo "not applied"; exit 0; }
    f="$(cat "$MARK")"
    mv "$f.orig-178107" "$f"
    rm -f "$MARK"
    echo "reverted $f"
    ;;
  status)
    if [[ -f "$MARK" ]]; then echo "applied"; else echo "not applied"; fi
    ;;
  *) echo "usage: $0 apply|revert|status" >&2; exit 1 ;;
esac
