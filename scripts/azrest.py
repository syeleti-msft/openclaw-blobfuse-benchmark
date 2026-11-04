#!/usr/bin/env python3
"""Minimal SharedKey-signed Azure Storage REST helper (keys read from a blobfuse yaml, never printed)."""
import base64, datetime, hashlib, hmac, sys, urllib.parse, urllib.request, re, json

def load_accounts(cfg_path):
    accts = {}
    name = None
    for line in open(cfg_path):
        s = line.strip().lstrip('#').strip()
        m = re.match(r'account-name:\s*(\S+)', s)
        if m:
            name = m.group(1)
        m = re.match(r'account-key:\s*(\S+)', s)
        if m and name:
            accts.setdefault(name, m.group(1))
    return accts

def request(acct, key, method, host_kind, path, query=None, headers=None, body=b''):
    query = query or {}
    headers = dict(headers or {})
    headers['x-ms-date'] = datetime.datetime.now(datetime.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    headers['x-ms-version'] = '2023-11-03'
    clen = str(len(body)) if body else ''
    canon_headers = ''.join(f'{k.lower()}:{v}\n' for k, v in sorted(headers.items()) if k.lower().startswith('x-ms-'))
    canon_res = f'/{acct}{path}'
    for k in sorted(query):
        canon_res += f'\n{k.lower()}:{query[k]}'
    sts = '\n'.join([method, '', '', clen, '', headers.get('Content-Type', ''), '', '', '', '', '', '']) + '\n' + canon_headers + canon_res
    sig = base64.b64encode(hmac.new(base64.b64decode(key), sts.encode(), hashlib.sha256).digest()).decode()
    headers['Authorization'] = f'SharedKey {acct}:{sig}'
    url = f'https://{acct}.{host_kind}.core.windows.net{urllib.parse.quote(path)}'
    if query:
        url += '?' + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, data=body if body else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

if __name__ == '__main__':
    cfg, acct, op = sys.argv[1], sys.argv[2], sys.argv[3]
    key = load_accounts(cfg)[acct]
    if op == 'info':
        st, h, _ = request(acct, key, 'GET', 'blob', '/', {'restype': 'account', 'comp': 'properties'})
        print(st, {k: v for k, v in h.items() if k.lower() in ('x-ms-sku-name', 'x-ms-account-kind', 'x-ms-is-hns-enabled')})
    elif op == 'containers':
        st, h, b = request(acct, key, 'GET', 'blob', '/', {'comp': 'list'})
        print(st, re.findall(r'<Name>([^<]+)</Name>', b.decode()))
    elif op == 'ls':
        cont, prefix = sys.argv[4], (sys.argv[5] if len(sys.argv) > 5 else '')
        q = {'restype': 'container', 'comp': 'list', 'maxresults': '50', 'delimiter': '/'}
        if prefix:
            q['prefix'] = prefix
        st, h, b = request(acct, key, 'GET', 'blob', f'/{cont}', q)
        txt = b.decode()
        print(st, 'blobs:', re.findall(r'<Blob><Name>([^<]+)</Name>', txt)[:50], 'prefixes:', re.findall(r'<BlobPrefix><Name>([^<]+)</Name>', txt)[:50])
    elif op == 'mkcontainer':
        st, h, b = request(acct, key, 'PUT', 'blob', f'/{sys.argv[4]}', {'restype': 'container'})
        print(st, b[:200])
    elif op == 'rmcontainer':
        st, h, b = request(acct, key, 'DELETE', 'blob', f'/{sys.argv[4]}', {'restype': 'container'})
        print(st, b[:200])
    elif op == 'getacl':
        st, h, b = request(acct, key, 'HEAD', 'dfs', f'/{sys.argv[4]}/{sys.argv[5]}', {'action': 'getAccessControl'})
        print(st, {k: v for k, v in h.items() if k.lower() in ('x-ms-permissions', 'x-ms-owner', 'x-ms-group')})
    elif op == 'setperm':
        st, h, b = request(acct, key, 'PATCH', 'dfs', f'/{sys.argv[4]}/{sys.argv[5]}', {'action': 'setAccessControl'}, {'x-ms-permissions': sys.argv[6]})
        print(st, b[:200])
