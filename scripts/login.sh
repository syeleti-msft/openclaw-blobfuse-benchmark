#!/usr/bin/env bash
# Sign OpenClaw in to GitHub Copilot, select the model, install the gateway service, and save the
# "golden" OpenClaw state that every benchmark run starts from (work/golden/openclaw-home.tar).
# Interactive: prints a GitHub device code to enter at https://github.com/login/device.
set -euo pipefail
trap 'echo "error: login.sh failed at line $LINENO" >&2' ERR
source "$(dirname "$0")/common.sh"

require_bench_owned_openclaw_home
mountpoint -q "$OPENCLAW_HOME" && die "$OPENCLAW_HOME is a mount point; unmount it first"
systemctl --user stop openclaw-gateway.service 2>/dev/null || true
rm -rf "$OPENCLAW_HOME"
mkdir -m 700 "$OPENCLAW_HOME"
touch "$OPENCLAW_HOME/$BENCH_MARKER"

echo "==> GitHub Copilot sign-in"
openclaw models auth login-github-copilot
openclaw models set "$MODEL"

echo "==> Gateway service"
openclaw gateway install --force >/dev/null
systemctl --user daemon-reload
systemctl --user restart openclaw-gateway.service
for _ in $(seq 60); do
  [[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18789/ || true)" != 000 ]] && break
  sleep 1
done

echo "==> Smoke test ($MODEL)"
reply="$(timeout 300 openclaw agent --agent main --message "Reply with exactly: pong" 2>&1 | tail -1)"
echo "    reply: $reply"
systemctl --user stop openclaw-gateway.service
[[ "$reply" == *pong* ]] || die "the model did not answer; check 'openclaw models status' (is $MODEL available to your Copilot plan?)"

mkdir -p -m 700 "$(dirname "$GOLDEN")"
tar -cpf "$GOLDEN" -C "$OPENCLAW_HOME" .
chmod 600 "$GOLDEN"
echo "Saved $GOLDEN (contains your Copilot token; keep it private)."
