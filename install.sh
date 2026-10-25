#!/bin/bash
# One-time install, run as: sudo bash ~/ipod-nano6g-fix/install.sh
# Only use after raw dd image verified (you did: 152f9bfc... match).
set -euo pipefail
STAGE="$HOME/ipod-nano6g-fix/nano6g-libs"
if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi
USER_HOME="$(getent passwd "${SUDO_USER:-lemion}" | cut -d: -f6)"
STAGE="$USER_HOME/ipod-nano6g-fix/nano6g-libs"
DST="/usr/lib/libgpod"
STORE="/usr/local/share/ipod-nano6g/nano6g-libs"
HOOK="/etc/pacman.d/hooks/libhashab-nano6g.hook"
FIXER="/usr/local/sbin/ipod-nano6g-fix.sh"

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
cp -f "$USER_HOME/ipod-nano6g-fix/reapply.sh" "$FIXER"
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
