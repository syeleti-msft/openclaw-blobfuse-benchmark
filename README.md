# OpenClaw on Azure Blob Storage (BlobFuse2): benchmark kit

Scripts, patches and tracing tools used to measure how the [OpenClaw](https://github.com/openclaw/openclaw)
AI agent performs when its data lives on Azure Blob Storage mounted with
[BlobFuse2](https://github.com/Azure/azure-storage-fuse), using the
[PinchBench](https://github.com/pinchbench/skill) agent benchmark (25 realistic tasks).

The investigation found that OpenClaw re-applies the same file permissions to its SQLite state
database after every write: about 40,000 redundant `chmod` calls per benchmark run. Blob storage
without hierarchical namespace (FNS) ignores them. With hierarchical namespace (HNS) every call becomes a synchronous
`SetAccessControl` request, which made HNS 44 % slower than local disk. Two fixes remove the problem:

| Fix | Where | Status |
|---|---|---|
| Skip `chmod` that cannot change anything, using the attribute cache | BlobFuse2 [Azure/azure-storage-fuse#2444](https://github.com/Azure/azure-storage-fuse/pull/2444) | Open |
| Skip `chmod` when the mode already matches (`applyPrivateModeSync`) | OpenClaw [openclaw/openclaw#178107](https://github.com/openclaw/openclaw/pull/178107) | Merged to `main` 2026-10-20, not yet released |
| Related: agent database compare-before-chmod | OpenClaw [openclaw/openclaw#146492](https://github.com/openclaw/openclaw/pull/146492) | Released (2026.9.x) |
| Related: no agent database permission recheck on every write | OpenClaw [openclaw/openclaw#169310](https://github.com/openclaw/openclaw/pull/169310) | Merged to `main`, not yet released |

## Results (Standard_D4s_v6, Premium block blob storage in the same region)

Median of 3 runs of all 25 tasks, agent workspaces and OpenClaw state on one BlobFuse2 mount
(file cache), OpenClaw 2026.7.1-2, model `github-copilot/gpt-5.4-nano`:

| Configuration | Wall time | vs local | Score | Timeouts | REST requests | `SetAccessControl` |
|---|---:|---:|---:|---:|---:|---:|
| Local disk | 1,333 s | — | 83.9 % | 1 | — | — |
| Premium FNS + BlobFuse2 #2444 | 1,394 s | +5 % | 77.9 % | 2 | 10,782 | 0 |
| Premium HNS, stock BlobFuse2 | 1,920 s | +44 % | 80.7 % | 3 | 53,220 | 42,652 |
| Premium HNS + BlobFuse2 #2444 | 1,452 s | +9 % | 82.0 % | 2 | 12,080 | 2,221 |
| Premium HNS + OpenClaw #178107 backport | 1,548 s | +16 % | 84.7 % | 2 | 13,151 | 2,324 |

Scores vary 74–92 % between identical runs because of the model; storage affects the score only
through tasks that hit their time limit.

## Patches in this repository

> **The published results depend on two patches to third-party projects. Both are in [`patches/`](patches/).**

| Patch | Applies to | Why |
|---|---|---|
| [`pinchbench-run-root.patch`](patches/pinchbench-run-root.patch) | PinchBench [`819384a`](https://github.com/pinchbench/skill/commit/819384a) | PinchBench hardcodes `/tmp/pinchbench` for agent workspaces. The patch reads `PINCHBENCH_RUN_ROOT` instead, so workspaces can be placed on the BlobFuse2 mount. Without the variable, behaviour is unchanged. |
| [`openclaw-178107.sh`](patches/openclaw-178107.sh) ([diff](patches/openclaw-178107-backport.diff)) | OpenClaw 2026.7.1-2 (npm) | Backports the one-line fix of [openclaw/openclaw#178107](https://github.com/openclaw/openclaw/pull/178107), which is not in any release yet. Applied in place for one run and reverted afterwards. |

The BlobFuse2 change is not a patch here: `scripts/build_blobfuse2.sh` builds it straight from
[PR #2444](https://github.com/Azure/azure-storage-fuse/pull/2444).

## How the setup works

```
PinchBench ──► openclaw agent (CLI) ──► OpenClaw gateway (systemd user service) ◄──► GitHub Copilot (model)
                                              │
                         ~/.openclaw  = one BlobFuse2 mount (file cache + attribute cache)
                         ├── state/openclaw.sqlite (+ -wal, -shm)    OpenClaw state database
                         ├── agents/<name>/agent/openclaw-agent.sqlite, agents/<name>/sessions/
                         ├── openclaw.json, credentials/, logs/ ...
                         └── pinchbench/<run>/agent_workspace/       files the agent works on
```

* **OpenClaw state root (`~/.openclaw`)**: SQLite databases, transcripts and configuration. OpenClaw
  writes to it for nearly every agent event.
* **Agent workspace**: the files the agent reads, edits and runs. Normally a user's project folder;
  here PinchBench's per-run workspace, moved inside `~/.openclaw` so the whole workload is on Blob.
* For each task PinchBench resets the workspace, sends the prompt through `openclaw agent`, waits
  (time limit 120–300 s), then grades a copy of the workspace (on local disk) with the task's checks.
* SQLite memory-maps its `-shm` file, so BlobFuse2 must use the file cache without direct I/O.

Each run starts from the same saved OpenClaw state ("golden" snapshot), a new container and an empty
file cache.

## Run it yourself

**You need:** an Ubuntu 22.04/24.04 x86_64 VM with `sudo`; a GitHub account with GitHub Copilot
(the model must be available to OpenClaw through Copilot); for Blob runs, a storage account without
hierarchical namespace (FNS) and/or one with it (HNS), ideally Premium block blob in the VM's region.

> Each run **replaces `~/.openclaw`**. The scripts refuse to touch an existing `~/.openclaw` that this
> kit did not create; move yours away first (`mv ~/.openclaw ~/.openclaw.backup`). Use a dedicated VM.

```bash
git clone https://github.com/syeleti-msft/openclaw-blobfuse-benchmark
cd openclaw-blobfuse-benchmark

scripts/install.sh            # packages, Node.js, OpenClaw 2026.7.1-2, uv, PinchBench + patch
export PATH=$HOME/.local/opt/node/bin:$HOME/.local/bin:$PATH
scripts/build_blobfuse2.sh    # work/bin/blobfuse2-stock and work/bin/blobfuse2-pr2444 (installs Go if needed)
scripts/login.sh              # GitHub Copilot device login, model, gateway, golden snapshot

cp accounts.example.yaml accounts.yaml && chmod 600 accounts.yaml   # add your account names and keys
export FNS_ACCT=<fns-account> HNS_ACCT=<hns-account>
```

Check the setup with a one-task run, then run the full matrix (about 2 hours per round):

```bash
scripts/run_bench.sh hns smoke work/bin/blobfuse2-pr2444 task_sanity
tmux new -s bench 'scripts/run_matrix.sh 3; bash'      # 5 configurations x 3 rounds
scripts/summarize.py                                    # medians per configuration and per task
```

### Scripts

| Script | Purpose |
|---|---|
| `scripts/install.sh` | Install prerequisites and the pinned versions (re-runnable). |
| `scripts/build_blobfuse2.sh` | Build stock BlobFuse2 (base of #2444) and BlobFuse2 with #2444. |
| `scripts/login.sh` | Sign in to Copilot, set the model, install the gateway, save the golden state. |
| `scripts/run_bench.sh <local\|fns\|hns> <label> [blobfuse2] [suite]` | One run. Results in `work/runs/<env>-<label>/`. |
| `scripts/run_matrix.sh [rounds] [suite]` | All configurations, round-robin, with validation and analysis. |
| `scripts/summarize.py [runs-dir]` | Median (min–max) tables per configuration and per task. |
| `tracing/analyze.py <run-dir>` | Per-run analysis: `chmod` calls by source, path and task; FUSE operations and REST requests from the BlobFuse2 log. Writes `analysis/summary.json` and `report.md`. |
| `tracing/chmod-trace.bt`, `tracing/chmod-trace.cjs` | Kernel (bpftrace) and Node.js tracers for every `chmod`. |
| `tracing/acl_race_analysis.py <run-dir>...` | HNS only: which `SetAccessControl` calls restored a mode reset by an upload, which were redundant. |
| `repro/hns_chmod_repro.py <local\|fns\|hns>` | Minimal reproduction of the HNS `chmod` cost (SQLite writes + OpenClaw-style re-hardening). |
| `repro/hns_mode_reset_repro.sh` | Minimal reproduction: a BlobFuse2 upload resets an HNS file's mode unless `preserve-acl` is set. |

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `FNS_ACCT`, `HNS_ACCT` | (required for Blob runs) | Storage account names; keys come from `accounts.yaml` |
| `MODEL` | `github-copilot/gpt-5.4-nano` | Model used by OpenClaw and PinchBench |
| `CONFIGS` | `local fns-pr2444 hns-stock hns-pr2444 hns-oc178107` | Matrix configurations (`fns-stock` is also available) |
| `TRACE` | `1` | Trace every `chmod` (needs `sudo` for bpftrace); `0` for plain timing runs |
| `FC_TIMEOUT`, `FC_SIZE_MB`, `ATTR_TIMEOUT` | `4294967295`, `4096`, `30` | BlobFuse2 file cache timeout (s) and size, attribute cache timeout (s) |
| `OPENCLAW_FIX` | (empty) | `178107` applies the OpenClaw backport for a single `run_bench.sh` run |
| `KEEP_CONTAINER` | `0` | `1` keeps each run's container (it contains a copy of the OpenClaw token) |

### Notes

* `gpt-5.4-mini` is not available to OpenClaw through Copilot at the time of writing
  ("not available for integrator vscode-chat"); `gpt-5.4-nano` is.
* Do not run OpenClaw from a copied install directory: OpenClaw then links bundled plugin skills into
  its state and adds them to the agent prompt (about 2,300 extra tokens per call), which changes
  results. `openclaw-178107.sh` patches the installed package in place for this reason.
* The golden snapshot (`work/golden/`) and every run's container hold your Copilot token. Containers
  are deleted after each run unless `KEEP_CONTAINER=1`.
* OpenClaw releases after 2026.10 store transcripts in a way PinchBench `819384a` cannot read, so
  these scripts pin both versions.

The OpenClaw helper workflow (install, run, stop) is based on the `test/openclaw` scripts in the
[`sourav/openclaw`](https://github.com/Azure/azure-storage-fuse/tree/sourav/openclaw/test/openclaw)
branch of Azure/azure-storage-fuse.

## License

MIT. PinchBench and OpenClaw are MIT-licensed projects of their respective authors.
