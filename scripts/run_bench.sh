#!/usr/bin/env bash
# One PinchBench run with OpenClaw's state root (~/.openclaw) on local disk or on a BlobFuse2 mount
# (file cache). PinchBench's agent workspaces are placed inside the state root, so with FNS/HNS the
# whole agent workload runs on Blob storage through one mount.
#
# usage: run_bench.sh <local|fns|hns> <label> [blobfuse2-binary] [suite]
#   suite: automated-only (default, 25 tasks) or task_sanity (1 quick task)
#
# Environment:
#   FNS_ACCT / HNS_ACCT   storage account names (keys are read from $ACCOUNTS_FILE)
#   OPENCLAW_FIX=178107   apply the OpenClaw #178107 backport for this run (reverted afterwards)
#   TRACE=1               trace every chmod (bpftrace + Node.js hook; needs sudo). TRACE=0 to skip.
#   FC_TIMEOUT, FC_SIZE_MB, ATTR_TIMEOUT   BlobFuse2 file/attribute cache settings
#   KEEP_CONTAINER=1      keep the run's container (it contains a copy of the OpenClaw token)
#   MODEL, WORK, SKILL_DIR, ACCOUNTS_FILE   see scripts/common.sh
set -euo pipefail
trap 'echo "error: run_bench.sh failed at line $LINENO" >&2' ERR
source "$(dirname "$0")/common.sh"

ENV_KIND="${1:?usage: run_bench.sh <local|fns|hns> <label> [blobfuse2-binary] [suite]}"
LABEL="${2:?label required}"
BF_BIN="${3:-$WORK/bin/blobfuse2-stock}"
SUITE="${4:-automated-only}"
TRACE="${TRACE:-1}"
FC_TIMEOUT="${FC_TIMEOUT:-4294967295}"   # maximum: cached files are never evicted by age
FC_SIZE_MB="${FC_SIZE_MB:-4096}"
ATTR_TIMEOUT="${ATTR_TIMEOUT:-30}"
OPENCLAW_FIX="${OPENCLAW_FIX:-}"

RUN="$WORK/runs/${ENV_KIND}-${LABEL}"
CACHE="$WORK/cache/${ENV_KIND}-${LABEL}"
SECRETS="$WORK/secrets"
CONTAINER="$(echo "oc-${ENV_KIND}-${LABEL}" | tr '[:upper:]_.' '[:lower:]--' | cut -c1-63)"
DROPIN_DIR="$HOME/.config/systemd/user/openclaw-gateway.service.d"
DROPIN="$DROPIN_DIR/zz-openclaw-blobfuse-benchmark.conf"
BT_PID_FILE="$RUN/bpftrace.pid"
AZREST="$BENCH_ROOT/scripts/azrest.py"

case "$ENV_KIND" in
  fns) ACCT="${FNS_ACCT:?set FNS_ACCT to your FNS (flat namespace) storage account}"; AZTYPE=block ;;
  hns) ACCT="${HNS_ACCT:?set HNS_ACCT to your HNS (hierarchical namespace) storage account}"; AZTYPE=adls ;;
  local) ACCT=""; AZTYPE="" ;;
  *) die "environment must be local, fns or hns" ;;
esac
[[ -f "$GOLDEN" ]] || die "missing $GOLDEN; run scripts/login.sh first"
[[ -d "$SKILL_DIR" ]] || die "missing $SKILL_DIR; run scripts/install.sh first"
[[ "$ENV_KIND" == local || -x "$BF_BIN" ]] || die "missing BlobFuse2 binary $BF_BIN; run scripts/build_blobfuse2.sh"
[[ -e "$RUN" ]] && die "run directory exists: $RUN"
require_bench_owned_openclaw_home

mkdir -p "$RUN/js" "$SECRETS"
chmod 700 "$SECRETS"
log() { printf '[%s] %s\n' "$(date -u +%FT%T.%3NZ)" "$*" | tee -a "$RUN/run.log"; }

mount_bf() {
  rm -rf "$CACHE"; mkdir -p "$CACHE"
  "$BF_BIN" mount "$OPENCLAW_HOME" --config-file="$SECRETS/${ENV_KIND}-${LABEL}.yaml"
  for _ in $(seq 30); do mountpoint -q "$OPENCLAW_HOME" && return 0; sleep 1; done
  log "mount did not come up"; return 1
}
unmount_bf() {
  if mountpoint -q "$OPENCLAW_HOME"; then "$BF_BIN" unmount "$OPENCLAW_HOME" || fusermount3 -u "$OPENCLAW_HOME"; fi
  for _ in $(seq 30); do mountpoint -q "$OPENCLAW_HOME" || return 0; sleep 1; done
  return 1
}
cleanup() {
  set +e
  [[ -f "$BT_PID_FILE" ]] && { sudo kill -INT "$(cat "$BT_PID_FILE")" 2>/dev/null; sleep 3; rm -f "$BT_PID_FILE"; }
  rm -f "$DROPIN"; systemctl --user daemon-reload
  [[ -n "$OPENCLAW_FIX" ]] && "$BENCH_ROOT/patches/openclaw-$OPENCLAW_FIX.sh" revert >/dev/null
  rm -f "$SECRETS/${ENV_KIND}-${LABEL}.yaml"
  rm -rf "$CACHE"
}
trap cleanup EXIT

write_bf_config() {
  local key
  key="$(python3 -c "import sys; sys.path.insert(0, '$BENCH_ROOT/scripts'); import azrest; print(azrest.load_accounts('$ACCOUNTS_FILE')['$ACCT'])")" \
    || die "no key for $ACCT in $ACCOUNTS_FILE"
  ( umask 077; cat > "$SECRETS/${ENV_KIND}-${LABEL}.yaml" <<EOF
logging:
  type: base
  level: log_debug
  file-path: $RUN/blobfuse2.log
  max-file-size-mb: 200
  file-count: 200

components:
  - libfuse
  - file_cache
  - attr_cache
  - azstorage

libfuse:
  attribute-expiration-sec: 0
  entry-expiration-sec: 0
  negative-entry-expiration-sec: 0
  disable-writeback-cache: true
  fuse-trace: true

file_cache:
  path: $CACHE
  timeout-sec: $FC_TIMEOUT
  max-size-mb: $FC_SIZE_MB

attr_cache:
  timeout-sec: $ATTR_TIMEOUT

azstorage:
  type: $AZTYPE
  account-name: $ACCT
  container: $CONTAINER
  mode: key
  account-key: $key
EOF
  )
  sed -E 's/(account-key:).*/\1 <redacted>/' "$SECRETS/${ENV_KIND}-${LABEL}.yaml" > "$RUN/blobfuse.yaml"
}

if [[ -n "$OPENCLAW_FIX" ]]; then
  "$BENCH_ROOT/patches/openclaw-$OPENCLAW_FIX.sh" apply >> "$RUN/run.log"
fi
OC_VARIANT="release${OPENCLAW_FIX:++$OPENCLAW_FIX}"
log "env=$ENV_KIND label=$LABEL model=$MODEL suite=$SUITE openclaw=$(openclaw --version) variant=$OC_VARIANT" \
    "blobfuse2=${ACCT:+$("$BF_BIN" --version 2>/dev/null | head -1)} container=${ACCT:+$ACCT/$CONTAINER}"

# 1. Quiesce: gateway down, mount point free.
systemctl --user stop openclaw-gateway.service || true
unmount_bf || die "could not unmount $OPENCLAW_HOME"

# 2. Fresh state from the golden snapshot (identical for every environment).
rm -rf "$OPENCLAW_HOME"; mkdir -m 700 "$OPENCLAW_HOME"
if [[ "$ENV_KIND" == local ]]; then
  tar -xpf "$GOLDEN" -C "$OPENCLAW_HOME"
else
  python3 "$AZREST" "$ACCOUNTS_FILE" "$ACCT" mkcontainer "$CONTAINER" | tee -a "$RUN/run.log"
  write_bf_config
  mount_bf
  tar -xpf "$GOLDEN" -C "$OPENCLAW_HOME" 2> "$RUN/seed-tar.err" || log "seed tar reported errors (see seed-tar.err)"
  sync
  # Cold start: remount with an empty file cache so the run starts from storage.
  unmount_bf
  mv "$RUN/blobfuse2.log" "$RUN/blobfuse2.seed.log" 2>/dev/null || true
  mount_bf
fi
find "$OPENCLAW_HOME" -maxdepth 3 -printf '%M %s %p\n' > "$RUN/state-before.txt" 2>&1 || true

# 3. Gateway (with the Node.js chmod tracer preloaded when tracing).
mkdir -p "$DROPIN_DIR"
if [[ "$TRACE" == 1 ]]; then
  printf '[Service]\nEnvironment=NODE_OPTIONS=--require=%s\nEnvironment=CHMODTRACE_DIR=%s\n' \
    "$BENCH_ROOT/tracing/chmod-trace.cjs" "$RUN/js" > "$DROPIN"
  sudo bash -c "ulimit -n 1048576; echo \$\$ > '$BT_PID_FILE'; BPFTRACE_CACHE_USER_SYMBOLS=PER_PROGRAM BPFTRACE_PERF_RB_PAGES=1024 BPFTRACE_MAX_MAP_KEYS=65536 exec bpftrace '$BENCH_ROOT/tracing/chmod-trace.bt' > '$RUN/kernel.trace'" > "$RUN/bpftrace.stderr" 2>&1 &
  for _ in $(seq 60); do grep -q READY "$RUN/kernel.trace" 2>/dev/null && break; sleep 1; done
  grep -q READY "$RUN/kernel.trace" || die "bpftrace did not start (see $RUN/bpftrace.stderr); use TRACE=0 to run without tracing"
fi
systemctl --user daemon-reload
systemctl --user reset-failed openclaw-gateway.service 2>/dev/null || true
GW_T0=$(date +%s.%N)
systemctl --user start openclaw-gateway.service
code=000
for _ in $(seq 180); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18789/ || true)
  [[ "$code" != 000 ]] && break; sleep 1
done
GW_READY=$(date +%s.%N)
log "gateway ready after $(python3 -c "print(round($GW_READY-$GW_T0,2))")s (http=$code)"

# 4. PinchBench, with agent workspaces inside the state root.
[[ "$TRACE" == 1 ]] && export NODE_OPTIONS="--require=$BENCH_ROOT/tracing/chmod-trace.cjs" CHMODTRACE_DIR="$RUN/js"
export PINCHBENCH_RUN_ROOT="$OPENCLAW_HOME/pinchbench"
PB_PATCH=$(git -C "$SKILL_DIR" diff | sha256sum | cut -c1-12)
log "pinchbench $(git -C "$SKILL_DIR" rev-parse --short HEAD) patch=$PB_PATCH run_root=$PINCHBENCH_RUN_ROOT"
T0=$(date +%s.%N)
log "benchmark start"
set +e
"$SKILL_DIR/scripts/run.sh" --model "$MODEL" --suite "$SUITE" --no-upload --output-dir "$RUN/pinchbench-output" \
  > "$RUN/pinchbench.log" 2>&1
RC=$?
set -e
T1=$(date +%s.%N)
unset NODE_OPTIONS CHMODTRACE_DIR
log "benchmark end rc=$RC wall_s=$(python3 -c "print(round($T1-$T0,2))")"
python3 - "$RUN/meta.json" <<EOF
import json, sys
json.dump({"env": "$ENV_KIND", "label": "$LABEL", "suite": "$SUITE", "model": "$MODEL", "rc": $RC,
           "t0": $T0, "t1": $T1, "wall_s": round($T1 - $T0, 3), "gw_start": $GW_T0, "gw_ready": $GW_READY,
           "bf_bin": "$BF_BIN", "bf_sha256": "$( [[ -x "$BF_BIN" ]] && sha256sum "$BF_BIN" | cut -c1-12 )",
           "oc_variant": "$OC_VARIANT", "container": "${ACCT:+$ACCT/$CONTAINER}",
           "pinchbench_run_root": "$PINCHBENCH_RUN_ROOT", "pinchbench_patch": "$PB_PATCH",
           "file_cache_timeout": "$FC_TIMEOUT", "attr_cache_timeout": "$ATTR_TIMEOUT", "trace": $TRACE},
          open(sys.argv[1], "w"))
EOF

# 5. Stop tracing and the gateway, unmount, delete the container.
if [[ -f "$BT_PID_FILE" ]]; then sudo kill -INT "$(cat "$BT_PID_FILE")" 2>/dev/null || true; sleep 3; rm -f "$BT_PID_FILE"; fi
find "$OPENCLAW_HOME" -maxdepth 4 -printf '%M %s %p\n' > "$RUN/state-after.txt" 2>&1 || true
openclaw gateway stop >> "$RUN/run.log" 2>&1 || systemctl --user stop openclaw-gateway.service || true
if [[ "$ENV_KIND" != local ]]; then
  unmount_bf || log "unmount failed"
  if [[ "${KEEP_CONTAINER:-0}" != 1 ]]; then
    log "delete container $ACCT/$CONTAINER: $(python3 "$AZREST" "$ACCOUNTS_FILE" "$ACCT" rmcontainer "$CONTAINER" | cut -c1-3)"
  fi
fi
log "done (results in $RUN)"
