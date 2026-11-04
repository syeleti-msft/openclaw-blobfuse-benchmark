#!/usr/bin/env python3
"""Minimal reproduction of the repeated-chmod cost on local disk vs blobfuse FNS vs blobfuse HNS.

Scenario A ("noop"):   chmod(path, mode) N times with the mode the file already has.
Scenario B ("sqlite"): OpenClaw-style SQLite WAL write transaction followed by unconditional
                        chmod(state_dir, 0700) + chmod(db/-wal/-shm, 0600) after every commit
                        (what ensureOpenClawStatePermissions does), optionally with a
                        stat-before-chmod guard ("--guard") as proposed for OpenClaw.

Usage: FNS_ACCT=... HNS_ACCT=... hns_chmod_repro.py <local|fns|hns> [--bf BIN] [--n N] [--guard] [--keep]
Keys are read from $ACCOUNTS_FILE (default: <repo>/accounts.yaml).
Blobfuse mounts use the same file_cache/attr_cache/libfuse settings as the benchmark and
count FUSE chmod ops and SetAccessControl REST requests from the blobfuse debug log.
"""
import argparse, os, re, sqlite3, stat, statistics, subprocess, sys, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.environ.get('WORK', os.path.join(ROOT, 'work'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import azrest  # noqa: E402

SRC_CFG = os.environ.get('ACCOUNTS_FILE', os.path.join(ROOT, 'accounts.yaml'))
ACCTS = {'fns': (os.environ.get('FNS_ACCT', ''), 'block'), 'hns': (os.environ.get('HNS_ACCT', ''), 'adls')}


def bf_config(env, container, cache, logf):
    acct, kind = ACCTS[env]
    key = azrest.load_accounts(SRC_CFG)[acct]
    return f"""logging:
  type: base
  level: log_debug
  file-path: {logf}
  max-file-size-mb: 200
  file-count: 50
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
file_cache:
  path: {cache}
  timeout-sec: 120
  max-size-mb: 4096
attr_cache:
  timeout-sec: 30
azstorage:
  type: {kind}
  account-name: {acct}
  container: {container}
  mode: key
  account-key: {key}
"""


def count_log(logf):
    out = {s: {'fuse_chmod': 0, 'set_access_control': 0} for s in ('noop', 'sqlite')}
    out['rest_total'] = 0
    if not os.path.exists(logf):
        return out
    scen = lambda s: 'noop' if 'noop.txt' in s else 'sqlite'
    pending = False
    with open(logf, errors='replace') as f:
        for line in f:
            if 'Libfuse::libfuse_chmod :' in line:
                out[scen(line)]['fuse_chmod'] += 1
            if 'OUTGOING REQUEST' in line:
                pending = True
                out['rest_total'] += 1
                continue
            if pending:
                m = re.search(r'\b(GET|PUT|PATCH|HEAD|DELETE|POST)\s+(https://\S+)', line)
                if m:
                    pending = False
                    if 'action=setAccessControl' in m.group(2):
                        out[scen(m.group(2))]['set_access_control'] += 1
    return out


def harden(dirp, db, guard, lat):
    def ch(p, mode):
        if guard:
            try:
                if stat.S_IMODE(os.stat(p).st_mode) == mode:
                    return 0
            except FileNotFoundError:
                return 0
        t = time.perf_counter()
        os.chmod(p, mode)
        lat.append(time.perf_counter() - t)
        return 1
    n = ch(dirp, 0o700)
    for suf in ('', '-wal', '-shm', '-journal'):
        if os.path.exists(db + suf):
            n += ch(db + suf, 0o600)
    return n


def run(root, n, guard):
    out = {}
    # Scenario A: same-mode chmod loop on one file.
    f = os.path.join(root, 'noop.txt')
    with open(f, 'w') as fh:
        fh.write('x')
    os.chmod(f, 0o600)
    lat = []
    t0 = time.perf_counter()
    for _ in range(n):
        t = time.perf_counter()
        os.chmod(f, 0o600)
        lat.append(time.perf_counter() - t)
    out['noop'] = {'calls': n, 'wall_s': time.perf_counter() - t0,
                   'p50_ms': statistics.median(lat) * 1e3, 'p95_ms': sorted(lat)[int(0.95 * n) - 1] * 1e3}
    # Scenario B: SQLite WAL write txn + OpenClaw-style hardening after every commit.
    d = os.path.join(root, 'state')
    os.makedirs(d, mode=0o700, exist_ok=True)
    db = os.path.join(d, 'openclaw.sqlite')
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, ts INTEGER)')
    lat, chmods = [], 0
    t0 = time.perf_counter()
    for i in range(n):
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT OR REPLACE INTO kv VALUES(?,?,?)', (f'k{i % 50}', uuid.uuid4().hex, i))
        conn.execute('COMMIT')
        chmods += harden(d, db, guard, lat)
    wall = time.perf_counter() - t0
    conn.close()
    out['sqlite'] = {'txns': n, 'chmods': chmods, 'wall_s': wall, 'txn_per_s': n / wall,
                     'chmod_s': sum(lat), 'p50_ms': (statistics.median(lat) * 1e3) if lat else 0.0}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('env', choices=['local', 'fns', 'hns'])
    ap.add_argument('--bf', default=os.path.join(WORK, 'bin', 'blobfuse2-stock'))
    ap.add_argument('--n', type=int, default=200)
    ap.add_argument('--guard', action='store_true')
    ap.add_argument('--keep', action='store_true')
    a = ap.parse_args()
    tag = f"{a.env}-{'guard' if a.guard else 'stock'}-{os.path.basename(a.bf)}-{int(time.time())}"
    if a.env != 'local' and not ACCTS[a.env][0]:
        sys.exit(f'set {a.env.upper()}_ACCT to your storage account name')
    base = os.path.join(WORK, 'repro', tag)
    os.makedirs(base)
    if a.env == 'local':
        root = os.path.join(base, 'mnt')
        os.makedirs(root)
        res = run(root, a.n, a.guard)
        res['log'] = {}
    else:
        acct = ACCTS[a.env][0]
        container = ('ocrepro-' + uuid.uuid4().hex[:12])
        st, _, body = azrest.request(acct, azrest.load_accounts(SRC_CFG)[acct], 'PUT', 'blob', f'/{container}', {'restype': 'container'})
        assert st == 201, (st, body[:200])
        mnt, cache, logf = os.path.join(base, 'mnt'), os.path.join(base, 'cache'), os.path.join(base, 'blobfuse2.log')
        os.makedirs(mnt); os.makedirs(cache)
        cfg = os.path.join(base, 'bf.yaml')
        fd = os.open(cfg, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, bf_config(a.env, container, cache, logf).encode()); os.close(fd)
        subprocess.run([a.bf, 'mount', mnt, f'--config-file={cfg}'], check=True)
        try:
            for _ in range(30):
                if os.path.ismount(mnt):
                    break
                time.sleep(1)
            res = run(mnt, a.n, a.guard)
        finally:
            subprocess.run([a.bf, 'unmount', mnt], check=False)
            os.remove(cfg)
            if not a.keep:
                azrest.request(acct, azrest.load_accounts(SRC_CFG)[acct], 'DELETE', 'blob', f'/{container}', {'restype': 'container'})
        res['log'] = count_log(logf)
    res['tag'] = tag
    import json
    print(json.dumps(res, indent=1))
    with open(os.path.join(base, 'result.json'), 'w') as fh:
        json.dump(res, fh, indent=1)


if __name__ == '__main__':
    main()
