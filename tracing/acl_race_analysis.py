#!/usr/bin/env python3
"""Interval-accurate classification of HNS permission traffic on the OpenClaw state quartet.

Every Blob upload (PutBlob / PutBlockList) of an existing HNS file resets its POSIX mode to the
account default (rw-r-----) unless azstorage.preserve-acl is set. Using SDK request/response
intervals from a log_debug BlobFuse log, this classifies each SetAccessControl as:
  first      - first permission write on the path in this mount
  restore    - the last storage change before it was an upload reset (a real change)
  racing     - overlapped an in-flight upload of the same path (outcome depends on server order)
  redundant  - storage already had the mode the last SetAccessControl applied
and each #2444 attr-cache skip as:
  correct           - storage still had the applied mode
  during_upload     - an upload of the path was in flight (stock would have raced it)
  stale             - the last completed storage change was an upload reset (a wrong skip)
It also reports how long each file spent with the reset (group-readable) mode.

usage: acl_race_analysis.py RUN_DIR [RUN_DIR...]
"""
import bisect
import datetime as dt
import glob
import json
import multiprocessing
import os
import re
import sys
import urllib.parse

QUARTET = ("state", "state/openclaw.sqlite", "state/openclaw.sqlite-wal", "state/openclaw.sqlite-shm")
LINE_TS = re.compile(r'^[A-Za-z]{3} ([A-Za-z]{3}) +(\d+) (\d{2}:\d{2}:\d{2}\.\d{3}) UTC (\d{4}) : ')
REQ_RE = re.compile(r'^\s+(GET|PUT|PATCH|HEAD|DELETE|POST)\s+https://[^.]+\.(blob|dfs)\.core\.windows\.net/[^/?\s]+/?([^?\s]*)\??(\S*)')
LAT_RE = re.compile(r'Try=\d+/([0-9.]+)(ms|µs|s)')
SKIP_RE = re.compile(r'AttrCache::Chmod : (\S+) already has mode \d+ applied, skipping storage call')
MONTHS = {m: i for i, m in enumerate(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)}
SCALE = {'ms': 1e-3, 'µs': 1e-6, 's': 1.0}


def log_files(run):
    files = [f for f in glob.glob(os.path.join(run, "blobfuse2.log*")) if re.search(r"blobfuse2\.log(\.\d+)?$", f)]
    return sorted(files, key=lambda f: -int(f.rsplit(".", 1)[1]) if f[-1].isdigit() else 0)


def parse(run):
    uploads = {p: [] for p in QUARTET}   # (start, end)
    setacls = {p: [] for p in QUARTET}   # (start, end)
    skips = {p: [] for p in QUARTET}     # t
    pending, t_last = None, None
    for f in log_files(run):
        with open(f, errors="replace") as fh:
            for line in fh:
                m = LINE_TS.match(line)
                if m:
                    mon, day, hms, yr = m.groups()
                    t_last = dt.datetime.strptime(f"{yr}-{MONTHS[mon]:02d}-{int(day):02d} {hms}",
                                                  "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc).timestamp()
                    pending = None
                    if "SDK(Response) : ==> REQUEST/RESPONSE" in line:
                        lm = LAT_RE.search(line)
                        pending = {"end": t_last, "lat": float(lm.group(1)) * SCALE[lm.group(2)] if lm else 0.0}
                    elif "skipping storage call" in line:
                        sm = SKIP_RE.search(line)
                        if sm and sm.group(1) in skips:
                            skips[sm.group(1)].append(t_last)
                    continue
                if pending is None:
                    continue
                if "op" not in pending:
                    rm = REQ_RE.match(line)
                    if rm:
                        method, kind, path, query = rm.groups()
                        path = urllib.parse.unquote(path)
                        q = dict(kv.split("=", 1) if "=" in kv else (kv, "") for kv in query.split("&") if kv)
                        op = None
                        if kind == "dfs" and method == "PATCH" and q.get("action") == "setAccessControl":
                            op = "setacl"
                        elif kind == "blob" and method == "PUT" and (q.get("comp") in (None, "blocklist")):
                            op = "upload"
                        pending.update(op=op, path=path)
                    continue
                st = re.match(r"^\s+RESPONSE Status: (\d{3})", line)
                if st:
                    ok = st.group(1) in ("200", "201")
                    if ok and pending["op"] and pending["path"] in QUARTET:
                        iv = (pending["end"] - pending["lat"], pending["end"])
                        (uploads if pending["op"] == "upload" else setacls)[pending["path"]].append(iv)
                    pending = None
    return uploads, setacls, skips, t_last


def classify(run):
    uploads, setacls, skips, t_end = parse(run)
    res = {}
    for p in QUARTET:
        ups = sorted(uploads[p])
        sas = sorted(setacls[p])
        up_ends = [e for _, e in ups]
        sa_ends = [e for _, e in sas]

        def overlaps_upload(a, b):
            # any upload interval [s, e] with s <= b and e >= a
            i = bisect.bisect_left(up_ends, a)
            return any(ups[j][0] <= b for j in range(i, min(i + 50, len(ups))))

        def last_change_before(t):
            # A skip can only follow the attr-cache Put that happens after a SetAccessControl response, so a
            # SetAccessControl completing in the same millisecond counts as earlier (log timestamps are ms).
            iu = bisect.bisect_left(up_ends, t) - 1
            ia = bisect.bisect_right(sa_ends, t) - 1
            tu = up_ends[iu] if iu >= 0 else None
            ta = sa_ends[ia] if ia >= 0 else None
            if tu is None and ta is None:
                return None
            if ta is None or (tu is not None and tu > ta):
                return "upload"
            return "setacl"

        c = {"uploads": len(ups), "setacl": len(sas), "first": 0, "restore": 0, "racing": 0, "redundant": 0,
             "skips": len(skips[p]), "skip_correct": 0, "skip_during_upload": 0, "skip_stale": 0}
        for k, (a, b) in enumerate(sas):
            if overlaps_upload(a, b):
                c["racing"] += 1
            elif k == 0:
                c["first"] += 1
            elif last_change_before(a) == "upload":
                c["restore"] += 1
            else:
                c["redundant"] += 1
        for t in skips[p]:
            if overlaps_upload(t, t):
                c["skip_during_upload"] += 1
            elif last_change_before(t) == "upload":
                c["skip_stale"] += 1
            else:
                c["skip_correct"] += 1
        # Time spent with the upload-reset mode: from an upload completion until the next SetAccessControl
        # completion (or the end of the log).
        exposed, reset_since = 0.0, None
        for t, kind in sorted([(e, "u") for e in up_ends] + [(e, "a") for e in sa_ends]):
            if kind == "u" and reset_since is None:
                reset_since = t
            elif kind == "a" and reset_since is not None:
                exposed += t - reset_since
                reset_since = None
        if reset_since is not None:
            exposed += t_end - reset_since
        c["reset_mode_s"] = round(exposed, 1)
        c["final"] = last_change_before(t_end + 1)
        res[p] = c
    return os.path.basename(os.path.normpath(run)), res


def main():
    with multiprocessing.Pool(min(8, len(sys.argv) - 1)) as pool:
        results = dict(pool.map(classify, sys.argv[1:]))
    print("| Run | Path | Uploads | SAC | first | restore | racing | redundant | skips ok | skips during upload | stale skips | s at reset mode | final |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for run, res in results.items():
        for p, c in res.items():
            print(f"| {run} | {p.replace('state/', '')} | {c['uploads']} | {c['setacl']} | {c['first']} | {c['restore']} | "
                  f"{c['racing']} | {c['redundant']} | {c['skip_correct']} | {c['skip_during_upload']} | {c['skip_stale']} | "
                  f"{c['reset_mode_s']} | {c['final']} |")
    json.dump(results, open("/tmp/acl_race_analysis.json", "w"), indent=1)


if __name__ == "__main__":
    main()
