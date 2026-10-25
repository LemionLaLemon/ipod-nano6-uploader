#!/bin/bash
# One-time install for the Nano6G uploader.
# Run as: sudo bash install.sh   (from anywhere; paths follow this script)
# Only use after verifying a raw dd backup of the iPod.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$SCRIPT_DIR/nano6g-libs"
DST="/usr/lib/libgpod"
STORE="/usr/local/share/ipod-nano6g/nano6g-libs"
HOOK="/etc/pacman.d/hooks/libhashab-nano6g.hook"
FIXER="/usr/local/sbin/ipod-nano6g-fix.sh"

if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi
if [ ! -d "$DST" ]; then echo "libgpod not found at $DST; install libgpod first"; exit 1; fi
for f in libhashab64_original.so libhashab32.so libhashab32_wrapper; do
  [ -f "$STAGE/$f" ] || { echo "missing $STAGE/$f; run this from the cloned repo"; exit 1; }
done
[ -f "$SCRIPT_DIR/reapply.sh" ] || { echo "missing $SCRIPT_DIR/reapply.sh"; exit 1; }

echo "== backup stock =="
mkdir -p "$DST/stock-backup-20260930"
cp -a "$DST/libhashab.so" "$DST/stock-backup-20260930/" 2>/dev/null || true
cp -a "$DST/libhashab32.so" "$DST/stock-backup-20260930/" 2>/dev/null || true
cp -a "$DST/libhashab32_wrapper" "$DST/stock-backup-20260930/" 2>/dev/null || true
md5sum "$DST"/libhashab* 2>/dev/null || true

echo "== install nano6g libs =="
mkdir -p "$STORE"
cp -f "$STAGE/libhashab64_original.so" "$STORE/"
cp -f "$STAGE/libhashab32.so" "$STORE/"
cp -f "$STAGE/libhashab32_wrapper" "$STORE/"
cp -f "$SCRIPT_DIR/reapply.sh" "$FIXER"
chmod 755 "$FIXER"
bash "$FIXER"

echo "== pacman hook =="
mkdir -p /etc/pacman.d/hooks
cat > "$HOOK" <<'HOOK'
[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = libgpod
Target = sg3_utils

[Action]
Description = Re-apply iPod Nano 6G libhashab + sgutils compat symlink
When = PostTransaction
Exec = /usr/local/sbin/ipod-nano6g-fix.sh
HOOK

echo "== verify =="
md5sum "$DST"/libhashab.so "$DST"/libhashab32.so "$DST"/libhashab32_wrapper
ldd /usr/bin/ipod-read-sysinfo-extended | grep -E "sgutils|not found" || true
echo "expected libhashab.so md5: 436395096a2ee028aaf9c8c53e682aaa"
echo "done. Test with strawberry first, then gtkpod AUR for photos."
