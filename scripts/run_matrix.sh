#!/usr/bin/env bash
# Run the benchmark matrix: ROUNDS rounds of the configurations below, interleaved round-robin so
# that time-of-day effects (model latency, storage load) affect every configuration equally.
# Each run is analyzed when it finishes. Stops early if a run looks invalid or disk space is low.
#
# usage: FNS_ACCT=<fns-account> HNS_ACCT=<hns-account> run_matrix.sh [rounds] [suite]
#   CONFIGS="local fns-pr2444 hns-stock hns-pr2444 hns-oc178107"  (default: all five)
set -uo pipefail
source "$(dirname "$0")/common.sh"

ROUNDS="${1:-3}"
SUITE="${2:-automated-only}"
CONFIGS="${CONFIGS:-local fns-pr2444 hns-stock hns-pr2444 hns-oc178107}"
STOCK="$WORK/bin/blobfuse2-stock"
PR="$WORK/bin/blobfuse2-pr$BLOBFUSE_PR"
LOG="$WORK/runs/matrix.log"
mkdir -p "$WORK/runs"
say() { printf '[%s] %s\n' "$(date -u +%FT%T)" "$*" | tee -a "$LOG"; }

# config name -> "<env> <blobfuse2 binary> <OpenClaw fix>"
cfg() {
  case "$1" in
    local)       echo "local $STOCK -" ;;
    fns-stock)   echo "fns $STOCK -" ;;
    fns-pr2444)  echo "fns $PR -" ;;
    hns-stock)   echo "hns $STOCK -" ;;
    hns-pr2444)  echo "hns $PR -" ;;
    hns-oc178107) echo "hns $STOCK 178107" ;;
    *) return 1 ;;
  esac
}

check_run() {
  python3 - "$1" "$2" <<'EOF'
import glob, json, sys
run, suite = sys.argv[1], sys.argv[2]
meta = json.load(open(f"{run}/meta.json"))
doc = None
for p in glob.glob(f"{run}/pinchbench-output/*.json"):
    d = json.load(open(p))
    if isinstance(d, dict) and "tasks" in d:
        doc = d
if meta.get("rc") != 0 or doc is None:
    print(f"INVALID rc={meta.get('rc')} results={'yes' if doc else 'no'}"); sys.exit(1)
scores = [((t.get("grading") or {}).get("mean") or 0) for t in doc["tasks"]]
positive = sum(1 for s in scores if s > 0)
print(f"tasks={len(scores)} scored>0={positive} score%={100 * sum(scores) / max(1, len(scores)):.1f} wall={meta['wall_s']:.0f}s")
# A model/auth failure scores (almost) every task 0.
sys.exit(0 if positive >= max(1, len(scores) * 0.4) else 1)
EOF
}

for c in $CONFIGS; do cfg "$c" >/dev/null || { echo "unknown config $c" >&2; exit 1; }; done
say "matrix start rounds=$ROUNDS suite=$SUITE configs=[$CONFIGS] model=$MODEL fns=${FNS_ACCT:-} hns=${HNS_ACCT:-}"
for r in $(seq "$ROUNDS"); do
  for c in $CONFIGS; do
    read -r env bin fix <<< "$(cfg "$c")"
    label="${c#*-}-r$r"; [[ "$c" == local ]] && label="r$r"
    free_gb=$(df -BG --output=avail "$WORK" | tail -1 | tr -dc 0-9)
    (( free_gb < 20 )) && { say "STOP: only ${free_gb} GB free"; exit 2; }
    say "run $env-$label ($(basename "$bin"), openclaw fix=$fix)"
    OPENCLAW_FIX="${fix/-/}" "$BENCH_ROOT/scripts/run_bench.sh" "$env" "$label" "$bin" "$SUITE" \
      > "$WORK/runs/.$env-$label.out" 2>&1
    say "  run_bench rc=$?"
    if ! res=$(check_run "$WORK/runs/$env-$label" "$SUITE"); then say "STOP: $env-$label invalid: $res"; exit 1; fi
    say "  $res"
    python3 "$BENCH_ROOT/tracing/analyze.py" "$WORK/runs/$env-$label" > "$WORK/runs/$env-$label/analysis.stdout" 2>&1 \
      || say "  analyze failed (see analysis.stdout)"
  done
done
say "matrix done"
python3 "$BENCH_ROOT/scripts/summarize.py" "$WORK/runs" | tee -a "$LOG"
