#!/usr/bin/env python3
"""Summarize benchmark runs: median (min-max) per configuration, and per-task median times.

usage: summarize.py [runs-dir]     (default: work/runs)
Run directories are named <config>-r<N>; runs of the same config are grouped.
Uses <run>/analysis/summary.json (from tracing/analyze.py) when present.
"""
import collections
import glob
import json
import os
import re
import statistics as st
import sys

RUNS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "work", "runs")


def results(run):
    for p in glob.glob(os.path.join(run, "pinchbench-output", "*.json")):
        d = json.load(open(p))
        if isinstance(d, dict) and "tasks" in d:
            return d["tasks"]
    return []


groups = collections.defaultdict(list)
for run in sorted(glob.glob(os.path.join(RUNS, "*-r[0-9]*"))):
    m = re.match(r"(.+)-r\d+$", os.path.basename(run))
    if m and os.path.exists(os.path.join(run, "meta.json")):
        groups[m.group(1)].append(run)
if not groups:
    sys.exit(f"no runs in {RUNS}")

order = ["local", "fns-stock", "fns-pr2444", "hns-stock", "hns-pr2444", "hns-oc178107"]
names = sorted(groups, key=lambda g: (order.index(g) if g in order else 99, g))


def fmt(vals, f="{:,.0f}"):
    vals = [v for v in vals if v is not None]
    if not vals:
        return "-"
    m = st.median(vals)
    return f.format(m) if len(vals) == 1 else f"{f.format(m)} ({f.format(min(vals))}-{f.format(max(vals))})"


rows = []
local = next((g for g in names if g.startswith("local")), None)
local_wall = st.median(json.load(open(os.path.join(r, "meta.json")))["wall_s"] for r in groups[local]) if local else None
for g in names:
    runs = groups[g]
    meta = [json.load(open(os.path.join(r, "meta.json"))) for r in runs]
    summ = [json.load(open(p)) if os.path.exists(p := os.path.join(r, "analysis", "summary.json")) else {} for r in runs]
    tasks = [results(r) for r in runs]
    wall = [m["wall_s"] for m in meta]
    score = [100 * sum(((t.get("grading") or {}).get("mean") or 0) for t in ts) / len(ts) for ts in tasks if ts]
    timeouts = [sum(bool(t.get("timed_out")) for t in ts) for ts in tasks if ts]
    rows.append([g, str(len(runs)), fmt(wall),
                 f"{(st.median(wall) / local_wall - 1) * 100:+.0f}%" if local_wall and g != local else "-",
                 fmt(score, "{:.1f}"), fmt(timeouts),
                 fmt([s.get("rest_total") for s in summ if s.get("rest_total")]),
                 fmt([s.get("rest_set_access_control") for s in summ if "rest_set_access_control" in s]),
                 fmt([s.get("gateway_loop_blocked_in_chmod_s") for s in summ if "gateway_loop_blocked_in_chmod_s" in s], "{:.0f}"),
                 fmt([s.get("kernel_chmod_state") for s in summ if "kernel_chmod_state" in s])])

hdr = ["config", "runs", "wall s", "vs local", "score %", "timeouts", "REST requests", "SetAccessControl",
       "gateway blocked in chmod (s)", "chmod on state DB"]
print("| " + " | ".join(hdr) + " |")
print("|" + "---|" * len(hdr))
for r in rows:
    print("| " + " | ".join(r) + " |")

per_task = collections.defaultdict(dict)
for g in names:
    acc = collections.defaultdict(list)
    for r in groups[g]:
        for t in results(r):
            acc[t["task_id"]].append(t.get("execution_time") or 0)
    for task, v in acc.items():
        per_task[task][g] = st.median(v)
print("\nPer-task median execution time (s)\n")
print("| task | " + " | ".join(names) + " |")
print("|" + "---|" * (len(names) + 1))
for task in sorted(per_task, key=lambda t: -max(per_task[t].values())):
    print(f"| {task.replace('task_', '')} | " + " | ".join(f"{per_task[task].get(g, 0):.0f}" for g in names) + " |")
