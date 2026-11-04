#!/usr/bin/env bash
# Minimal repro: on an HNS account, a BlobFuse file-cache upload of an existing file resets its POSIX
# mode to the account default (rw-r-----) unless azstorage.preserve-acl is enabled.
# usage: HNS_ACCT=<hns-account> hns_mode_reset_repro.sh [blobfuse2-binary]
# Keys are read from $ACCOUNTS_FILE (default: <repo>/accounts.yaml).
set -euo pipefail
T=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$T")
BF=${1:-${WORK:-$ROOT/work}/bin/blobfuse2-stock}
CFG=${ACCOUNTS_FILE:-$ROOT/accounts.yaml}
ACCT=${HNS_ACCT:?set HNS_ACCT to your HNS storage account}
AZREST=$ROOT/scripts/azrest.py
C=oc-modereset-$(date +%s)
W=$(mktemp -d /tmp/oc-modereset.XXXX)
MNT=$W/mnt
mkdir -p "$MNT"

storage_mode() { python3 "$AZREST" "$CFG" "$ACCT" getacl "$C" "$1" | sed -E "s/.*'x-ms-permissions': '([^']+)'.*/\1/"; }
count() { echo $(( $(grep -c "$1" "$2" || true) / 2 )); } # the SDK logs each request URL twice
cleanup() {
  "$BF" unmount "$MNT" >/dev/null 2>&1 || fusermount3 -u "$MNT" >/dev/null 2>&1 || true
  python3 "$AZREST" "$CFG" "$ACCT" rmcontainer "$C" >/dev/null || true
  rm -rf "$W"
}
trap cleanup EXIT

python3 "$AZREST" "$CFG" "$ACCT" mkcontainer "$C" >/dev/null
echo "binary: $(basename "$BF")  container: $ACCT/$C"
for variant in default preserve-acl; do
  python3 - "$T" "$C" "$W" "$variant" > "$W/$variant.yaml" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
import hns_chmod_repro as r
t, c, w, v = sys.argv[1:5]
cfg = r.bf_config('hns', c, f'{w}/cache-{v}', f'{w}/{v}.log')
print(cfg + ('  preserve-acl: true\n' if v == 'preserve-acl' else ''))
EOF
  mkdir -p "$W/cache-$variant"
  "$BF" mount "$MNT" --config-file="$W/$variant.yaml"
  for _ in $(seq 50); do mountpoint -q "$MNT" && break; sleep 0.2; done
  f=$variant.txt
  printf a > "$MNT/$f"
  chmod 600 "$MNT/$f"
  echo "[$variant] after chmod 600 : storage=$(storage_mode "$f") mount=$(stat -c %A "$MNT/$f")"
  before_get=$(count 'action=getAccessControl' "$W/$variant.log"); before_set=$(count 'action=setAccessControl' "$W/$variant.log")
  printf b >> "$MNT/$f"
  echo "[$variant] after append+close (upload): storage=$(storage_mode "$f") mount=$(stat -c %A "$MNT/$f")" \
       "| extra REST for the upload: GetAccessControl=$(( $(count 'action=getAccessControl' "$W/$variant.log") - before_get ))" \
       "SetAccessControl=$(( $(count 'action=setAccessControl' "$W/$variant.log") - before_set ))"
  "$BF" unmount "$MNT" >/dev/null
done
