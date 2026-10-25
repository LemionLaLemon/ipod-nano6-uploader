#!/bin/bash
# Revert to stock, run as: sudo bash ~/ipod-nano6g-fix/restore-stock.sh
set -euo pipefail
DST="/usr/lib/libgpod"
if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi
cp -a "$DST/stock-backup-20260930/libhashab.so" "$DST/libhashab.so"
cp -a "$DST/stock-backup-20260930/libhashab32.so" "$DST/libhashab32.so" 2>/dev/null || true
cp -a "$DST/stock-backup-20260930/libhashab32_wrapper" "$DST/libhashab32_wrapper" 2>/dev/null || true
rm -f /etc/pacman.d/hooks/libhashab-nano6g.hook
rm -f /usr/local/sbin/ipod-nano6g-fix.sh
echo "reverted. Stock md5s:"
md5sum "$DST"/libhashab*
