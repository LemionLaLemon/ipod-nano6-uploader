#!/bin/bash
# Re-apply Nano 6G files after libgpod/sg3_utils upgrades.
# Installed by ~/ipod-nano6g-fix/install.sh, triggered by pacman hook.
set -u
SRC="/usr/local/share/ipod-nano6g/nano6g-libs"
DST="/usr/lib/libgpod"
[ -d "$SRC" ] || exit 0
[ -d "$DST" ] || exit 0
cp -f "$SRC/libhashab64_original.so" "$DST/libhashab.so"
cp -f "$SRC/libhashab32.so" "$DST/libhashab32.so"
cp -f "$SRC/libhashab32_wrapper" "$DST/libhashab32_wrapper"
chmod 755 "$DST/libhashab.so" "$DST/libhashab32.so" "$DST/libhashab32_wrapper"
# compat symlink: libgpod 0.8.3-20.1 was built against libsgutils2-1.48,
# repo now ships 1.49. Remove when libgpod is rebuilt.
if [ ! -e /usr/lib/libsgutils2-1.48.so.2 ] && [ -e /usr/lib/libsgutils2-1.49.so.2 ]; then
  ln -sf libsgutils2-1.49.so.2 /usr/lib/libsgutils2-1.48.so.2
fi
ldconfig 2>/dev/null || true
