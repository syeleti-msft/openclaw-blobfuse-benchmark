#!/usr/bin/env python3
"""Analyze one benchmark run directory produced by run_bench.sh.

Inputs : meta.json, pinchbench.log, pinchbench-output/*.json, js/*.jsonl (Node chmod hook),
         kernel.trace (bpftrace chmod_common), blobfuse2.log* (FNS/HNS runs).
Outputs: <run>/analysis/summary.json, report.md, events.csv (one row per kernel chmod event).
"""
import bisect
import collections
import csv
import datetime as dt
import glob
import json
import os
import re
import sys
import urllib.parse

HOME = os.path.expanduser('~')
# Name of the directory holding runs/ and cache/ (the harness's own files are not part of the benchmark).
WORK_TAG = 'work'
FORKS = []
NOISE_COMMS = {'clamscan', 'azsecd', 'freshclam'}


def ts_of(s):
    s = s.rstrip('Z')
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()


def oct_mode(v):
    return f'{v:o}' if isinstance(v, int) else v


# ----------------------------------------------------------------------------- inputs

def load_meta(run):
    with open(os.path.join(run, 'meta.json')) as f:
        return json.load(f)


def load_tasks(run, meta):
    starts = []
    with open(os.path.join(run, 'pinchbench.log'), errors='replace') as f:
        for line in f:
            m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) - INFO - .*starting task: (\S+)', line)
            if m:
                t = dt.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=dt.timezone.utc).timestamp() + int(m.group(2)) / 1000
                starts.append((t, m.group(3)))
    results = {}
    files = sorted(glob.glob(os.path.join(run, 'pinchbench-output', '*.json')))
    doc = None
    for p in files:
        try:
            with open(p) as f:
                d = json.load(f)
            if isinstance(d, dict) and 'tasks' in d:
                doc = d
        except (OSError, json.JSONDecodeError):
            pass
    if doc:
        for t in doc['tasks']:
            g = t.get('grading') or {}
            score = g.get('score', g.get('mean'))
            mx = g.get('max_score', 1.0)
            if isinstance(g.get('runs'), list) and g['runs'] and score is None:
                score = g['runs'][0].get('score')
            results[t['task_id']] = {
                'status': t.get('status'), 'timed_out': t.get('timed_out'),
                'execution_time': t.get('execution_time'), 'score': score, 'max_score': mx,
                'tokens': (t.get('usage') or {}).get('total_tokens'),
            }
    return starts, results, doc


class TaskClock:
    def __init__(self, starts, t0, t1):
        self.starts = starts
        self.times = [s[0] for s in starts]
        self.t0, self.t1 = t0, t1

    def task(self, t):
        if t < self.t0:
            return '(pre-benchmark)'
        if t > self.t1:
            return '(post-benchmark)'
        i = bisect.bisect_right(self.times, t) - 1
        return self.starts[i][1] if i >= 0 else '(benchmark setup)'


def load_js(run):
    procs, events = {}, []
    for p in glob.glob(os.path.join(run, 'js', '*.jsonl')):
        with open(p, errors='replace') as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d['ev'] == 'proc':
                    procs[d['pid']] = d
                elif d['ev'] == 'chmod':
                    d['t'] = d['ts'] / 1000.0
                    events.append(d)
    events.sort(key=lambda e: e['t'])
    return procs, events


def load_kernel(run):
    path = os.path.join(run, 'kernel.trace')
    evs, stacks, paths, cgs = {}, {}, {}, {}
    if not os.path.exists(path):
        return []
    cur_st = None
    with open(path, errors='replace') as f:
        for line in f:
            if line.startswith('\t'):
                if cur_st is not None:
                    stacks[cur_st].append(line.strip())
                continue
            cur_st = None
            if line.startswith('EV|'):
                p = line.rstrip('\n').split('|')
                evs[p[1]] = {
                    'key': p[1], 't': ts_of(p[2]), 'iso': p[2], 'pid': int(p[3]), 'tid': int(p[4]), 'ppid': int(p[5]),
                    'proc': p[6], 'thread': p[7], 'fs': p[8], 'old': int(p[9], 8), 'req': int(p[10], 8),
                    'ret': int(p[11]), 'lat_us': int(p[12]),
                }
            elif line.startswith('PA|'):
                p = line.rstrip('\n').split('|', 2)
                paths[p[1]] = p[2]
            elif line.startswith('FK|'):
                p = line.rstrip('\n').split('|')
                FORKS.append((int(p[1]), int(p[2])))
            elif line.startswith('CG|'):
                p = line.rstrip('\n').split('|', 2)
                cgs[p[1]] = p[2]
            elif line.startswith('ST|'):
                p = line.rstrip('\n').split('|', 2)
                cur_st = p[1]
                stacks[cur_st] = []
    out = []
    for k, e in evs.items():
        e['raw_path'] = paths.get(k, '')
        e['cgroup'] = cgs.get(k, '')
        e['stack'] = stacks.get(k, [])
        e['path'] = kernel_path(e['raw_path'], e['fs'])
        out.append(e)
    out.sort(key=lambda e: e['t'])
    return out


def kernel_path(raw, fs):
    comps = [c for c in raw.split('/') if c]
    if fs.startswith('fuse'):
        return '~/.openclaw/' + '/'.join(comps)
    s = '/'.join(comps)
    i = s.find('.openclaw')
    if i >= 0:
        return '~/' + s[i:]
    return '/' + s


def js_path(p):
    return '~' + p[len(HOME):] if p.startswith(HOME) else p


WS_RE = re.compile(r'^~/\.openclaw/pinchbench/\d+/agent_workspace(?:/([^/]+))?(/.*)?$')


def area(p):
    p = js_path(p)
    if p.startswith('~/.openclaw/pinchbench/') or p == '~/.openclaw/pinchbench':
        return 'workspace'
    if p.startswith('~/.openclaw'):
        return 'state'
    return 'other'


def storage_area(rel):
    """Area of a container- or mount-relative path."""
    rel = rel.lstrip('/')
    return 'workspace' if rel.startswith('pinchbench') else ('state' if rel else 'root')


def path_class(p):
    p = js_path(p)
    m = WS_RE.match(p)
    if m:
        return '<workspace>' + (f'/{m.group(1)}' if m.group(1) else '') + ('/...' if m.group(2) else '')
    p = re.sub(r'/agents/[^/]+/', '/agents/<agent>/', p)
    p = re.sub(r'/agents/[^/]+$', '/agents/<agent>', p)
    p = re.sub(r'\.openclaw-[a-z]+-\.\d+\.[0-9a-f-]+\.tmp$', '<sibling-temp>.tmp', p)
    p = re.sub(r'/[^/]*\.tmp(-[^/]*)?$', '/<temp>.tmp', p)
    p = re.sub(r'/sessions/[^/]+\.trajectory\.jsonl$', '/sessions/<session>.trajectory.jsonl', p)
    p = re.sub(r'/sessions/[^/]+\.trajectory-path\.json$', '/sessions/<session>.trajectory-path.json', p)
    p = re.sub(r'/sessions/[^/]+\.jsonl$', '/sessions/<session>.jsonl', p)
    p = re.sub(r'openclaw-\d{4}-\d{2}-\d{2}\.log$', 'openclaw-<date>.log', p)
    p = re.sub(r'^/tmp/pinchbench/\d+/[^/]+(/.*)?$', '/tmp/pinchbench/<run>/<workspace>/...', p)
    p = re.sub(r'^/tmp/[^/]*pinchbench[^/]*(/.*)?$', '/tmp/<pinchbench-tmp>/...', p)
    return p


def on_state(p):
    return area(p) == 'state'


# ----------------------------------------------------------------------------- JS stacks

FN_RE = re.compile(r'^(?:async )?([^\s(]+)\s*\(')


def frames(stack):
    out = []
    for fr in stack:
        if 'node:' in fr and 'openclaw' not in fr:
            continue
        m = FN_RE.match(fr)
        if m:
            out.append(m.group(1))
        else:
            mm = re.search(r'/dist/([^/:]+?)(?:-[A-Za-z0-9_-]{8})?\.js:(\d+)', fr) or re.search(r'([^/]+\.(?:m?js|cjs)):(\d+)', fr)
            out.append(f'<anon {mm.group(1)}:{mm.group(2)}>' if mm else '<anon>')
    return out


MECHANISMS = [
    ('ensureOpenClawStatePermissions', 'state-db hardening (ensureOpenClawStatePermissions)'),
    ('ensureOpenClawAgentDatabasePermissions', 'agent-db hardening (ensureOpenClawAgentDatabasePermissions)'),
    ('hardenWorkboardDatabaseFiles', 'workboard-db hardening'),
    ('appendRegularFileSync', 'append-file fchmod (appendRegularFileSync)'),
    ('appendRegularFile', 'append-file chmod (appendRegularFile)'),
    ('replaceFileAtomicUnserialized', 'atomic replace (replaceFileAtomic: dir + file)'),
    ('replaceFileAtomicSync', 'atomic replace sync (replaceFileAtomicSync)'),
    ('writeSiblingTempFile', 'sibling temp write (writeSiblingTempFile)'),
    ('sibling-temp', 'sibling temp write (writeSiblingTempFile)'),
    ('writeTrajectoryPointerBestEffort', 'trajectory pointer (writeTrajectoryPointerBestEffort)'),
    ('enforcePrivateFileIdentityAndMode', 'secret file write (writeSecretFileAtomic)'),
    ('ensureModelsFileModeForModelsJson', 'models.json mode (ensureModelsFileModeForModelsJson)'),
    ('trySetSecureMode', 'JSON store secure mode (trySetSecureMode)'),
    ('hardenBackupPermissions', 'backup hardening (hardenBackupPermissions)'),
    ('chmodConfigBestEffort', 'config chmod (chmodConfigBestEffort)'),
    ('enforcePrivatePathMode', 'private path mode (enforcePrivatePathMode)'),
    ('repairSessionFileIfNeeded', 'session repair (repairSessionFileIfNeeded)'),
]


def mechanism(fr):
    joined = ' '.join(fr)
    for key, name in MECHANISMS:
        if key in joined:
            return name
    first = next((f for f in fr if not f.startswith('<anon')), fr[0] if fr else '?')
    return f'other ({first})'


def signature(fr, n=6):
    return ' <- '.join(fr[:n])


def trigger(fr):
    """Caller that triggered state/agent DB hardening (open vs post-write) and the writer above it."""
    for i, f in enumerate(fr):
        if f in ('ensureOpenClawStatePermissions', 'ensureOpenClawAgentDatabasePermissions'):
            rest = [x for x in fr[i + 1:] if not x.startswith('<anon')]
            if not rest:
                return '?'
            phase = rest[0]
            writer = next((x for x in rest[1:] if x not in ('runOpenClawStateWriteTransaction', 'runOpenClawAgentWriteTransaction', 'openOpenClawStateDatabase', 'openOpenClawAgentDatabase', 'runSqliteImmediateTransactionSync')), '?')
            return f'{phase} <- {writer}'
    return ''


# ----------------------------------------------------------------------------- kernel origin

def kernel_origin(e):
    st = ' '.join(e['stack'])
    if 'robust_open' in st or 'unixOpen' in st:
        return 'sqlite robust_open (native fchmod)'
    if 'uv__fs_copyfile' in st or 'uv_fs_copyfile' in st:
        return 'libuv copyfile (native fchmod)'
    if 'node::fs::' in st or 'uv__fs_work' in st or 'uv_fs_chmod' in st or 'uv_fs_fchmod' in st:
        return 'node fs API (JS)'
    comm = e['proc']
    if comm.startswith('python'):
        return f'python ({comm})'
    return f'native ({comm})'


def proc_role(e, js_procs):
    jp = js_procs.get(e['pid'])
    if jp:
        return jp['role'] if not jp['role'].startswith('cli:agent ') else 'cli:agent'
    if 'openclaw-gateway.service' in e['cgroup']:
        return f'gateway child ({e["proc"]})'
    return e['proc']


S_bf_cache = []


def bench_scope(meta, js_procs):
    """Classify a kernel event as app (OpenClaw), pinchbench, blobfuse-cache, or None (unrelated)."""
    node_pids = set(js_procs)
    pb_pids = {p['ppid'] for p in js_procs.values() if p['role'].startswith('cli:')} - node_pids
    children = collections.defaultdict(set)
    for parent, child in FORKS:
        children[parent].add(child)
    desc = set()
    stack = list(pb_pids)
    while stack:
        p = stack.pop()
        for c in children.get(p, ()):
            if c not in desc:
                desc.add(c)
                stack.append(c)
    cache_tag = f"{meta['env']}-{meta['label']}"

    def scope(e):
        if e['pid'] in node_pids or 'openclaw-gateway.service' in e['cgroup']:
            return 'app'
        if e['proc'].startswith('blobfuse2'):
            return 'blobfuse-cache' if cache_tag in e['raw_path'] else None
        if e['pid'] in pb_pids or e['pid'] in desc:
            return 'pinchbench'
        if not FORKS and e['proc'] in ('git', 'bash', 'sh', 'uv') and WORK_TAG not in e['raw_path']:
            return 'pinchbench'
        return None
    return scope


def match_js(kev, jsev):
    by_pid = collections.defaultdict(list)
    for j in jsev:
        by_pid[j['pid']].append(j)
    used = set()
    for e in kev:
        cands = by_pid.get(e['pid'])
        if not cands:
            continue
        base = os.path.basename(e['path'])
        best, bestd = None, None
        lo = bisect.bisect_left([c['t'] for c in cands], e['t'] - 0.25)
        for j in cands[lo:]:
            if j['t'] > e['t'] + 0.02:
                break
            if id(j) in used or os.path.basename(j['path']) != base:
                continue
            end = j['t'] + j.get('dur_us', 0) / 1e6
            d = abs(e['t'] - end)
            if bestd is None or d < bestd:
                best, bestd = j, d
        if best is not None:
            used.add(id(best))
            e['js'] = best
    return used


# ----------------------------------------------------------------------------- blobfuse log

REQ_RE = re.compile(r'^\s+(GET|PUT|PATCH|HEAD|DELETE|POST)\s+https://([^.]+)\.(blob|dfs)\.core\.windows\.net/([^/?\s]+)/?([^?\s]*)\??(\S*)')
LINE_TS = re.compile(r'^[A-Za-z]{3} ([A-Za-z]{3}) +(\d+) (\d{2}:\d{2}:\d{2}\.\d{3}) UTC (\d{4}) : ')
FUSE_RE = re.compile(r'Libfuse::libfuse_([a-z_0-9]+) :')
MONTHS = {m: i for i, m in enumerate(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)}


def rest_op(method, kind, path, query):
    q = dict(kv.split('=', 1) if '=' in kv else (kv, '') for kv in query.split('&') if kv)
    if kind == 'dfs':
        if q.get('action') == 'setAccessControl':
            return 'dfs SetAccessControl'
        if q.get('action') == 'getAccessControl':
            return 'dfs GetAccessControl'
        if q.get('resource') == 'filesystem':
            return 'dfs ListPaths'
        if method == 'PUT' and q.get('resource') == 'directory':
            return 'dfs CreateDirectory'
        if method == 'PUT' and q.get('resource') == 'file':
            return 'dfs CreateFile'
        if method == 'PUT':
            return 'dfs RenamePath'
        if method == 'HEAD':
            return 'dfs GetProperties'
        if method == 'DELETE':
            return 'dfs DeletePath'
        return f'dfs {method}{" " + q["action"] if "action" in q else ""}'
    if q.get('comp') == 'list':
        return 'blob ListBlobs'
    if method == 'PUT':
        return {'block': 'blob PutBlock', 'blocklist': 'blob PutBlockList', 'metadata': 'blob SetMetadata'}.get(q.get('comp'), 'blob PutBlob' if path else 'blob PUT container')
    if method == 'GET' and q.get('comp') == 'blocklist':
        return 'blob GetBlockList'
    return {'HEAD': 'blob GetProperties', 'GET': 'blob GetBlob', 'DELETE': 'blob DeleteBlob'}.get(method, f'blob {method}')


def parse_blobfuse(run, clock):
    logs = sorted(glob.glob(os.path.join(run, 'blobfuse2.log*')), key=lambda p: (p.count('.'), p))
    logs = [p for p in logs if 'seed' not in p]
    if not logs:
        return None
    rest = collections.Counter()
    rest_status = collections.Counter()
    rest_by_task = collections.defaultdict(collections.Counter)
    sac_by_path = collections.Counter()
    sac_status_by_path = collections.Counter()
    rest_lat = collections.defaultdict(float)
    fuse = collections.Counter()
    fuse_by_task = collections.defaultdict(collections.Counter)
    chmod_skips = collections.Counter()
    rest_area = collections.Counter()
    rest_area_ms = collections.defaultdict(float)
    rest_area_op = collections.Counter()
    fuse_area = collections.Counter()
    fuse_area_op = collections.Counter()
    pending = None
    last_t = None
    for p in logs:
        with open(p, errors='replace') as f:
            for line in f:
                m = LINE_TS.match(line)
                if m:
                    mon, day, hms, yr = m.groups()
                    last_t = dt.datetime.strptime(f'{yr}-{MONTHS[mon]:02d}-{int(day):02d} {hms}', '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=dt.timezone.utc).timestamp()
                    if pending is not None:
                        pending = None
                    if 'SDK(Response) : ==> REQUEST/RESPONSE' in line:
                        lm = re.search(r'Try=\d+/([0-9.]+)(ms|µs|s)', line)
                        lat = float(lm.group(1)) * {'ms': 1, 'µs': 1e-3, 's': 1e3}[lm.group(2)] if lm else 0.0
                        pending = {'t': last_t, 'lat_ms': lat}
                        continue
                    fm = FUSE_RE.search(line)
                    if fm and 'LOG_TRACE' in line:
                        op = fm.group(1)
                        fuse[op] += 1
                        fp = line[fm.end():].strip().split(',')[0].strip()
                        fa = storage_area(fp)
                        fuse_area[fa] += 1
                        fuse_area_op[(fa, op)] += 1
                        if clock:
                            fuse_by_task[clock.task(last_t)][op] += 1
                    if 'AttrCache::Chmod :' in line and 'LOG_DEBUG' in line:
                        if 'already has mode' in line:
                            chmod_skips['redundant mode'] += 1
                        elif 'cached as absent' in line:
                            chmod_skips['cached absent (ENOENT)'] += 1
                    continue
                if pending is None:
                    continue
                rm = REQ_RE.match(line)
                if rm and 'op' not in pending:
                    method, _acct, kind, _cont, path, query = rm.groups()
                    rel = path
                    if not rel:
                        qm = re.search(r'(?:^|&)(?:prefix|directory)=([^&]*)', query)
                        rel = urllib.parse.unquote(qm.group(1)) if qm else ''
                    pending.update(op=rest_op(method, kind, path, query), path=path, area=storage_area(rel))
                    continue
                sm = re.match(r'^\s+RESPONSE Status: (\d{3})', line)
                if sm and 'op' in pending:
                    st = int(sm.group(1))
                    op = pending['op']
                    if op == 'blob PutBlob' and st == 202:
                        op = 'blob CopyBlob'
                    rest[op] += 1
                    rest_status[(op, st)] += 1
                    rest_lat[op] += pending['lat_ms']
                    rest_area[pending['area']] += 1
                    rest_area_ms[pending['area']] += pending['lat_ms']
                    rest_area_op[(pending['area'], op)] += 1
                    task = clock.task(pending['t']) if clock else ''
                    rest_by_task[task][op] += 1
                    if op == 'dfs SetAccessControl':
                        pc = path_class('~/.openclaw/' + pending['path'])
                        sac_by_path[pc] += 1
                        sac_status_by_path[(pc, st)] += 1
                    pending = None
    return {
        'rest': rest, 'rest_status': rest_status, 'rest_by_task': rest_by_task, 'rest_lat_ms': rest_lat,
        'sac_by_path': sac_by_path, 'sac_status_by_path': sac_status_by_path,
        'fuse': fuse, 'fuse_by_task': fuse_by_task, 'chmod_skips': chmod_skips,
        'rest_area': rest_area, 'rest_area_ms': rest_area_ms, 'rest_area_op': rest_area_op,
        'fuse_area': fuse_area, 'fuse_area_op': fuse_area_op,
    }


# ----------------------------------------------------------------------------- report

def md_table(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join('---' for _ in headers) + '|']
    for r in rows:
        out.append('| ' + ' | '.join(str(x) for x in r) + ' |')
    return '\n'.join(out)


def main(run):
    run = os.path.abspath(run)
    meta = load_meta(run)
    starts, results, doc = load_tasks(run, meta)
    clock = TaskClock(starts, meta['t0'], meta['t1'])
    js_procs, jsev = load_js(run)
    kev_all = load_kernel(run)
    scope = bench_scope(meta, js_procs)
    kev = []
    for e in kev_all:
        if not (meta['gw_start'] - 1 <= e['t'] <= meta['t1'] + 1):
            continue
        e['scope'] = scope(e)
        if e['scope'] == 'blobfuse-cache':
            S_bf_cache.append(e)
        elif e['scope']:
            kev.append(e)
    match_js(kev, jsev)
    jsev_bench = [j for j in jsev if meta['gw_start'] - 1 <= j['t'] <= meta['t1'] + 1]
    bf = parse_blobfuse(run, clock)

    for e in kev:
        if 'js' in e and e['js']['path'].startswith('/'):
            e['path'] = js_path(e['js']['path'])
        e['task'] = clock.task(e['t'])
        e['noop'] = (e['old'] & 0o7777) == (e['req'] & 0o7777)
        e['role'] = proc_role(e, js_procs)
        e['origin'] = kernel_origin(e)
        fr = frames(e['js']['stack']) if 'js' in e else []
        e['mechanism'] = mechanism(fr) if fr else e['origin']
        e['signature'] = signature(fr) if fr else ' <- '.join(s.split(' ', 1)[-1].split('+')[0] for s in e['stack'][:5])
        e['trigger'] = trigger(fr)
        e['class'] = path_class(e['path'])
        e['on_state'] = on_state(e['path'])
    for j in jsev_bench:
        j['task'] = clock.task(j['t'])
        j['class'] = path_class(j['path'])
        fr = frames(j['stack'])
        j['mechanism'] = mechanism(fr)
        j['signature'] = signature(fr)
        j['trigger'] = trigger(fr)

    adir = os.path.join(run, 'analysis')
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, 'events.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['iso_ts', 'task', 'pid', 'tid', 'process', 'role', 'thread', 'fs', 'path', 'path_class', 'requested_mode', 'current_mode', 'noop', 'lat_us', 'ret', 'origin', 'mechanism', 'trigger', 'js_fn', 'stack_signature'])
        for e in kev:
            w.writerow([e['iso'], e['task'], e['pid'], e['tid'], e['proc'], e['role'], e['thread'], e['fs'], e['path'], e['class'],
                        oct_mode(e['req'] & 0o7777), oct_mode(e['old'] & 0o7777), int(e['noop']), e['lat_us'], e['ret'], e['origin'],
                        e['mechanism'], e['trigger'], e['js']['fn'] if 'js' in e else '', e['signature']])

    tot = len(kev)
    st = [e for e in kev if e['on_state']]
    S = {
        'run': os.path.basename(run), 'env': meta['env'], 'label': meta['label'], 'model': meta.get('model'),
        'bf_bin': os.path.basename(meta.get('bf_bin', '')), 'oc_variant': meta.get('oc_variant'),
        'wall_s': meta['wall_s'], 'rc': meta['rc'],
        'kernel_chmod_total': tot, 'kernel_chmod_state': len(st),
        'kernel_chmod_noop': sum(e['noop'] for e in kev), 'kernel_chmod_state_noop': sum(e['noop'] for e in st),
        'kernel_chmod_lat_s': sum(e['lat_us'] for e in kev) / 1e6, 'kernel_chmod_state_lat_s': sum(e['lat_us'] for e in st) / 1e6,
        'kernel_chmod_err': sum(e['ret'] != 0 for e in kev),
        'js_chmod_total': len(jsev_bench), 'js_chmod_state': sum(on_state(j['path']) for j in jsev_bench),
        'js_chmod_workspace': sum(area(j['path']) == 'workspace' for j in jsev_bench),
        'kernel_chmod_workspace': sum(area(e['path']) == 'workspace' for e in kev),
        'js_matched': sum('js' in e for e in kev),
        'kernel_chmod_by_scope': dict(collections.Counter(e['scope'] for e in kev)),
        'js_chmod_s': round(sum(j.get('dur_us', 0) for j in jsev_bench) / 1e6, 3),
        'gateway_loop_blocked_in_chmod_s': round(sum(j.get('dur_us', 0) for j in jsev_bench if j['role'] == 'gateway' and j['fn'] in ('chmodSync', 'fchmodSync') and j['tid'] == 0) / 1e6, 3),
        'gateway_post_write_hardening_calls': sum(1 for j in jsev_bench if j['role'] == 'gateway' and j['path'].endswith('/.openclaw/state') and 'runOpenClawStateWriteTransaction' in ' '.join(j['stack'])),
        'blobfuse_cache_chmod': len(S_bf_cache),
    }
    if results:
        sc = [r['score'] for r in results.values() if isinstance(r.get('score'), (int, float))]
        S['tasks'] = len(results)
        S['score_sum'] = round(sum(sc), 3)
        S['score_pct'] = round(100 * sum(sc) / max(1, len(results)), 2)
        S['tasks_timed_out'] = sum(bool(r.get('timed_out')) for r in results.values())
        S['tasks_full_score'] = sum(1 for r in results.values() if (r.get('score') or 0) >= 0.999)
        S['task_exec_s'] = round(sum((r.get('execution_time') or 0) for r in results.values()), 2)
    if bf:
        S['rest_total'] = sum(bf['rest'].values())
        S['rest_set_access_control'] = bf['rest'].get('dfs SetAccessControl', 0)
        S['rest_set_access_control_404'] = sum(v for (op, s), v in bf['rest_status'].items() if op == 'dfs SetAccessControl' and s == 404)
        S['rest_get_access_control'] = bf['rest'].get('dfs GetAccessControl', 0)
        S['rest_set_access_control_ms'] = round(bf['rest_lat_ms'].get('dfs SetAccessControl', 0.0), 1)
        S['fuse_ops_total'] = sum(bf['fuse'].values())  # traced FUSE ops (getattr/read/write are not traced)
        S['fuse_chmod'] = bf['fuse'].get('chmod', 0)
        S['attr_cache_chmod_answered'] = dict(bf['chmod_skips'])
        S['rest_by_op'] = dict(bf['rest'].most_common())
        S['rest_s_by_op'] = {op: round(ms / 1000, 1) for op, ms in sorted(bf['rest_lat_ms'].items(), key=lambda kv: -kv[1])}
        S['fuse_by_op'] = dict(bf['fuse'].most_common())
        S['rest_by_area'] = dict(bf['rest_area'])
        S['rest_s_by_area'] = {k: round(v / 1000, 1) for k, v in bf['rest_area_ms'].items()}
        S['fuse_by_area'] = dict(bf['fuse_area'])
        S['rest_by_area_op'] = {f'{a}|{op}': n for (a, op), n in bf['rest_area_op'].most_common()}

    # ---- report
    L = [f"# Run {S['run']}", '', '```', json.dumps({k: v for k, v in S.items() if not isinstance(v, dict)}, indent=1), '```', '']

    rows = []
    task_order = [s[1] for s in starts]
    by_task = collections.defaultdict(list)
    for e in kev:
        by_task[e['task']].append(e)
    js_by_task = collections.Counter(j['task'] for j in jsev_bench)
    for t in ['(benchmark setup)'] + task_order + ['(post-benchmark)']:
        es = by_task.get(t, [])
        r = results.get(t, {})
        sst = [e for e in es if e['on_state']]
        row = [t, '' if r.get('score') is None else f"{r['score']:.2f}", '' if r.get('execution_time') is None else f"{r['execution_time']:.1f}",
               js_by_task.get(t, 0), len(es), len(sst), f"{100 * sum(e['noop'] for e in sst) / len(sst):.0f}%" if sst else '-',
               f"{sum(e['lat_us'] for e in sst) / 1e3:.0f}"]
        if bf:
            row += [bf['rest_by_task'].get(t, {}).get('dfs SetAccessControl', 0), sum(bf['rest_by_task'].get(t, {}).values()), sum(bf['fuse_by_task'].get(t, {}).values())]
        rows.append(row)
    sst_all = [e for e in kev if e['on_state']]
    total = ['**total**', S.get('score_sum', ''), S.get('task_exec_s', ''), len(jsev_bench), len(kev), len(sst_all),
             f"{100 * sum(e['noop'] for e in sst_all) / len(sst_all):.0f}%" if sst_all else '-', f"{sum(e['lat_us'] for e in sst_all) / 1e3:.0f}"]
    if bf:
        total += [S['rest_set_access_control'], S['rest_total'], S['fuse_ops_total']]
    rows.append(total)
    hdr = ['task', 'score', 'exec s', 'JS chmod', 'kernel chmod', 'on ~/.openclaw', 'no-op %', 'chmod ms (state)']
    if bf:
        hdr += ['SetAccessControl', 'REST total', 'FUSE ops']
    L += ['## Per task', '', md_table(hdr, rows), '']

    top_classes = [c for c, _ in collections.Counter(e['class'] for e in kev).most_common(7)]
    rows = []
    for t in ['(benchmark setup)'] + task_order:
        cnt = collections.Counter(e['class'] for e in by_task.get(t, []))
        rows.append([t] + [cnt.get(c, 0) for c in top_classes] + [sum(cnt.values()) - sum(cnt.get(c, 0) for c in top_classes)])
    L += ['## Per task x path (kernel events)', '', md_table(['task'] + [c.replace('~/.openclaw/', '') for c in top_classes] + ['other'], rows), '']

    pc = collections.defaultdict(lambda: [0, 0, 0, collections.Counter()])
    for e in kev:
        k = (e['class'], oct_mode(e['req'] & 0o7777))
        pc[k][0] += 1
        pc[k][1] += e['noop']
        pc[k][2] += e['lat_us']
        pc[k][3][e['mechanism']] += 1
    rows = [[k[0], k[1], v[0], f"{100 * v[1] / v[0]:.0f}%", f"{v[2] / 1e3:.0f}", f"{v[2] / v[0]:.0f}", v[3].most_common(1)[0][0]]
            for k, v in sorted(pc.items(), key=lambda kv: -kv[1][0])[:40]]
    L += ['## Per path class and requested mode (kernel events)', '', md_table(['path', 'mode', 'calls', 'no-op %', 'total ms', 'avg us', 'main mechanism'], rows), '']

    mc = collections.defaultdict(lambda: [0, 0, 0])
    for e in kev:
        mc[e['mechanism']][0] += 1
        mc[e['mechanism']][1] += e['on_state']
        mc[e['mechanism']][2] += e['noop']
    rows = [[k, v[0], v[1], f"{100 * v[2] / v[0]:.0f}%"] for k, v in sorted(mc.items(), key=lambda kv: -kv[1][0])]
    L += ['## Per mechanism (kernel events joined to JS stacks)', '', md_table(['mechanism', 'calls', 'on ~/.openclaw', 'no-op %'], rows), '']

    tc = collections.Counter(e['trigger'] for e in kev if e['trigger'])
    L += ['## State/agent DB hardening triggers (phase <- writer)', '', md_table(['trigger', 'calls'], tc.most_common(25)), '']

    sc = collections.Counter((j['mechanism'], j['signature']) for j in jsev_bench)
    L += ['## Top JS call stacks', '', md_table(['calls', 'mechanism', 'stack (innermost first)'], [[v, k[0], k[1]] for k, v in sc.most_common(25)]), '']

    rc = collections.defaultdict(lambda: [0, 0])
    for e in kev:
        rc[e['role']][0] += 1
        rc[e['role']][1] += e['on_state']
    L += ['## Per process', '', md_table(['process', 'calls', 'on ~/.openclaw'], sorted(([k, v[0], v[1]] for k, v in rc.items()), key=lambda r: -r[1])[:25]), '']

    oc = collections.Counter(e['origin'] for e in kev)
    L += ['## Kernel-observed origin', '', md_table(['origin', 'calls'], oc.most_common()), '']

    if bf:
        L += ['## Blobfuse REST requests', '', md_table(['operation', 'status', 'count'], [[op, s, v] for (op, s), v in sorted(bf['rest_status'].items(), key=lambda kv: -kv[1])]), '']
        L += ['## SetAccessControl by path', '', md_table(['path', 'status', 'count'], [[p, s, v] for (p, s), v in sorted(bf['sac_status_by_path'].items(), key=lambda kv: -kv[1])[:30]]), '']
        L += ['## FUSE operations', '', md_table(['op', 'count'], bf['fuse'].most_common()), '']
        L += ['## REST requests by area', '', md_table(['area', 'requests', 'REST s'], [[a, n, f"{bf['rest_area_ms'][a] / 1000:.1f}"] for a, n in bf['rest_area'].most_common()]), '']
        L += ['## REST requests by area and operation', '', md_table(['area', 'operation', 'count'], [[a, op, n] for (a, op), n in bf['rest_area_op'].most_common(30)]), '']
        L += ['## FUSE operations by area', '', md_table(['area', 'op', 'count'], [[a, op, n] for (a, op), n in bf['fuse_area_op'].most_common(30)]), '']
        if bf['chmod_skips']:
            L += ['## chmod answered by attr_cache', '', md_table(['reason', 'count'], bf['chmod_skips'].most_common()), '']

    with open(os.path.join(adir, 'report.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
    with open(os.path.join(adir, 'summary.json'), 'w') as f:
        json.dump(S, f, indent=1, default=str)
    print(json.dumps({k: v for k, v in S.items() if not isinstance(v, dict)}, indent=1))


if __name__ == '__main__':
    main(sys.argv[1])
