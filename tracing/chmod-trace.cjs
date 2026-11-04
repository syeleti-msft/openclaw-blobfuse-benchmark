'use strict';
// Node preload (NODE_OPTIONS=--require=...) that records every JS-level chmod/fchmod.
// Enabled only when CHMODTRACE_DIR is set. Output: <dir>/<pid>-<threadId>.jsonl
// CHMODTRACE_STAT=1 additionally stats the target before the call (current mode).
const traceDir = process.env.CHMODTRACE_DIR;
if (traceDir) {
  const fs = require('fs');
  const path = require('path');
  const { threadId } = require('worker_threads');
  const { performance } = require('perf_hooks');
  const doStat = process.env.CHMODTRACE_STAT === '1';

  const o = {
    openSync: fs.openSync, writeSync: fs.writeSync, mkdirSync: fs.mkdirSync,
    statSync: fs.statSync, fstatSync: fs.fstatSync, readlinkSync: fs.readlinkSync,
    chmod: fs.chmod, chmodSync: fs.chmodSync, fchmod: fs.fchmod, fchmodSync: fs.fchmodSync,
    lchmod: fs.lchmod, lchmodSync: fs.lchmodSync,
    pchmod: fs.promises.chmod, plchmod: fs.promises.lchmod,
  };

  let outFd = -1;
  let seq = 0;
  const nowIso = () => {
    const t = performance.timeOrigin + performance.now();
    return { ts: t, iso: new Date(t).toISOString() };
  };
  function emit(obj) {
    try {
      if (outFd < 0) {
        o.mkdirSync(traceDir, { recursive: true });
        outFd = o.openSync(path.join(traceDir, `${process.pid}-${threadId}.jsonl`), 'a');
      }
      o.writeSync(outFd, JSON.stringify(obj) + '\n');
    } catch { /* never break the host process */ }
  }

  const argv = process.argv.slice(1).map((a) => (a.length > 120 ? a.slice(0, 117) + '...' : a));
  const sidIdx = process.argv.indexOf('--session-id');
  const sessionId = sidIdx > 0 ? process.argv[sidIdx + 1] : undefined;
  const firstIdx = process.argv.findIndex((a, i) => i >= 2 && !a.startsWith('-'));
  const first = firstIdx > 0 ? process.argv[firstIdx] : '?';
  const second = firstIdx > 0 ? process.argv[firstIdx + 1] : undefined;
  const sub = second && /^[a-z][a-z-]{0,20}$/.test(second) ? ` ${second}` : '';
  const role = /dist\/index\.js$/.test(process.argv[1] || '') && first === 'gateway'
    ? 'gateway'
    : `cli:${first}${sub}`;
  emit({ ev: 'proc', ...nowIso(), pid: process.pid, ppid: process.ppid, tid: threadId, role, sessionId, argv });
  process.on('exit', (code) => emit({ ev: 'exit', ...nowIso(), pid: process.pid, tid: threadId, code }));

  function stackNow() {
    const lim = Error.stackTraceLimit;
    Error.stackTraceLimit = 40;
    const s = new Error().stack || '';
    Error.stackTraceLimit = lim;
    return s.split('\n').slice(1).map((l) => l.trim().replace(/^at /, ''))
      .filter((l) => !l.includes('chmod-trace.cjs'));
  }
  function curMode(target, isFd) {
    if (!doStat) return undefined;
    try {
      const st = isFd ? o.fstatSync(target) : o.statSync(target);
      return (st.mode & 0o7777).toString(8);
    } catch (e) { return `ERR:${e.code}`; }
  }
  function fdPath(fd) {
    try { return o.readlinkSync(`/proc/self/fd/${fd}`); } catch { return `fd:${fd}`; }
  }
  function normMode(mode) {
    const m = typeof mode === 'string' ? parseInt(mode, 8) : mode;
    return Number.isFinite(m) ? (m & 0o7777).toString(8) : String(mode);
  }
  function record(fn, target, mode, isFd) {
    const p = isFd ? fdPath(target) : path.resolve(String(target));
    return {
      ev: 'chmod', seq: seq++, ...nowIso(), pid: process.pid, tid: threadId, role, sessionId, fn,
      path: p, mode: normMode(mode), cur: curMode(target, isFd), stack: stackNow(),
    };
  }
  function finish(rec, t0, err) {
    rec.dur_us = Math.round((performance.now() - t0) * 1000);
    if (err) rec.err = err.code || String(err);
    emit(rec);
  }

  const wrapSync = (fn, orig, isFd) => function (target, mode) {
    const rec = record(fn, target, mode, isFd);
    const t0 = performance.now();
    try {
      const r = orig.apply(this, arguments);
      finish(rec, t0);
      return r;
    } catch (e) { finish(rec, t0, e); throw e; }
  };
  const wrapCb = (fn, orig, isFd) => function (target, mode, cb) {
    const rec = record(fn, target, mode, isFd);
    const t0 = performance.now();
    if (typeof cb !== 'function') return orig.apply(this, arguments);
    return orig.call(this, target, mode, function (err) { finish(rec, t0, err); return cb.apply(this, arguments); });
  };
  const wrapPromise = (fn, orig) => function (target, mode) {
    const rec = record(fn, target, mode, false);
    const t0 = performance.now();
    return orig.apply(this, arguments).then(
      (r) => { finish(rec, t0); return r; },
      (e) => { finish(rec, t0, e); throw e; });
  };

  fs.chmodSync = wrapSync('chmodSync', o.chmodSync, false);
  fs.fchmodSync = wrapSync('fchmodSync', o.fchmodSync, true);
  fs.chmod = wrapCb('chmod', o.chmod, false);
  fs.fchmod = wrapCb('fchmod', o.fchmod, true);
  if (o.lchmodSync) fs.lchmodSync = wrapSync('lchmodSync', o.lchmodSync, false);
  if (o.lchmod) fs.lchmod = wrapCb('lchmod', o.lchmod, false);
  fs.promises.chmod = wrapPromise('promises.chmod', o.pchmod);
  if (o.plchmod) fs.promises.lchmod = wrapPromise('promises.lchmod', o.plchmod);

  // FileHandle.prototype.chmod is not reachable synchronously; patch it via a probe handle.
  o.pOpen = fs.promises.open;
  o.pOpen('/dev/null', 'r').then((fh) => {
    const proto = Object.getPrototypeOf(fh);
    const origFh = proto.chmod;
    proto.chmod = function (mode) {
      const rec = record('FileHandle.chmod', this.fd, mode, true);
      const t0 = performance.now();
      return origFh.apply(this, arguments).then(
        (r) => { finish(rec, t0); return r; },
        (e) => { finish(rec, t0, e); throw e; });
    };
    return fh.close();
  }).catch(() => {});

  require('module').syncBuiltinESMExports();
}
