#!/bin/sh
# Install Pacer. Re-run to update.
#   ./install.sh                         per-user install into ~/.local (no root)
#   PREFIX=/usr DESTDIR=pkg ./install.sh  staged system install (distribution packages)
set -e
ROOT=$(dirname "$(readlink -f "$0")")
PREFIX="${PREFIX:-$HOME/.local}"
D="${DESTDIR:-}"
LIB="$PREFIX/lib/pacer"                       # paths as seen at run time
SHARE="$PREFIX/share/pacer"
LAYERS="$PREFIX/share/vulkan/implicit_layer.d"
cd "$ROOT"
if [ ! -f build/x86_64/libVkLayer_pacer.so ] || [ -n "$REBUILD" ]; then
  if command -v make >/dev/null && command -v g++ >/dev/null; then make -s all || make -s x86_64; fi
fi
[ -f build/x86_64/libVkLayer_pacer.so ] || { echo "build failed (need g++, make, vulkan headers)" >&2; exit 1; }
mkdir -p "$D$LIB/x86_64" "$D$LIB/i386" "$D$LAYERS" "$D$PREFIX/bin" "$D$SHARE" "$D$PREFIX/share/applications"
for a in x86_64 i386; do
  for f in libVkLayer_pacer.so libpacer_gl.so; do
    if [ -f "build/$a/$f" ]; then
      # install via temp+rename so running games keep their mapped copy
      cp "build/$a/$f" "$D$LIB/$a/.$f.new" && mv -f "$D$LIB/$a/.$f.new" "$D$LIB/$a/$f"
    fi
  done
  if [ -f "$D$LIB/$a/libVkLayer_pacer.so" ]; then
    ./packaging/manifest.sh "$LIB/$a/libVkLayer_pacer.so" VK_LAYER_PACER_limiter > "$D$LAYERS/pacer.$a.json"
    echo "layer ($a): $LAYERS/pacer.$a.json"
  fi
done
# OpenGL preload tree indexed by the dynamic loader's $LIB token, so one LD_PRELOAD
# entry picks the right architecture without "wrong ELF class" noise. Which directory
# name $LIB expands to differs between distributions; probe it on this machine.
PL="$D$LIB/preload"; rm -rf "$PL"; mkdir -p "$PL"
LIB64=""
for cand in lib64 lib x86_64-linux-gnu; do
  mkdir -p "$PL/$cand"; ln -sf ../../x86_64/libpacer_gl.so "$PL/$cand/libpacer_gl.so"
  if [ -z "$(LD_PRELOAD="$PL/\$LIB/libpacer_gl.so" /bin/true 2>&1)" ]; then LIB64=$cand; break; fi
  rm -rf "${PL:?}/$cand"
done
[ -n "$LIB64" ] || { LIB64=lib; mkdir -p "$PL/lib"; ln -sf ../../x86_64/libpacer_gl.so "$PL/lib/libpacer_gl.so"; }
case "$LIB64" in lib) LIB32=lib32;; lib64) LIB32=lib;; *) LIB32=i386-linux-gnu;; esac
for d in lib64:x86_64 x86_64-linux-gnu:x86_64 i386-linux-gnu:i386 "$LIB32:i386"; do
  n=${d%%:*}; a=${d##*:}
  [ -e "$PL/$n" ] && continue
  mkdir -p "$PL/$n"; ln -sf "../../$a/libpacer_gl.so" "$PL/$n/libpacer_gl.so"
done
echo "gl preload: \$LIB=$LIB64 (64-bit), $LIB32 (32-bit)"
rm -rf "$D$SHARE/gui.new"
mkdir -p "$D$SHARE/gui.new"
cp -r gui/pacer "$D$SHARE/gui.new/pacer"
find "$D$SHARE/gui.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$D$SHARE/gui" && mv "$D$SHARE/gui.new" "$D$SHARE/gui"
for b in pacer pacer-ctl pacer-run; do
  install -m755 "bin/$b" "$D$PREFIX/bin/$b"
done
cat > "$D$PREFIX/share/applications/pacer.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Pacer
GenericName=Frame rate limiter
Comment=Frame rate limiter with per-game presets
Exec=$PREFIX/bin/pacer
Icon=video-display
Terminal=false
Categories=Game;Utility;
DESK

if [ -z "$D" ]; then
  CFGBASE="${XDG_CONFIG_HOME:-$HOME/.config}"
  CFG="$CFGBASE/pacer"
  # Pre-release name was "fpslimiter": migrate presets once and remove its hooks so
  # two limiter layers never stack in the same game.
  if [ ! -e "$CFG" ] && [ -d "$CFGBASE/fpslimiter" ]; then
    cp -r "$CFGBASE/fpslimiter" "$CFG" && echo "migrated settings from $CFGBASE/fpslimiter"
  fi
  if [ -e "$LAYERS/fpslimiter.x86_64.json" ] || [ -d "$PREFIX/lib/fpslimiter" ]; then
    rm -f "$LAYERS/fpslimiter.x86_64.json" "$LAYERS/fpslimiter.i386.json" \
          "$PREFIX/bin/fpslimiter" "$PREFIX/bin/fpslimiter-ctl" \
          "$PREFIX/share/applications/fpslimiter.desktop"
    rm -rf "$PREFIX/lib/fpslimiter" "$PREFIX/share/fpslimiter"
    # keep existing Steam launch options ("fpslimiter-run %command%") working
    [ -e "$PREFIX/bin/fpslimiter-run" ] && ln -sf pacer-run "$PREFIX/bin/fpslimiter-run"
    if [ -f "$CFGBASE/autostart/fpslimiter.desktop" ]; then
      sed "s#^Exec=.*#Exec=$PREFIX/bin/pacer --tray#" "$CFGBASE/autostart/fpslimiter.desktop" > "$CFGBASE/autostart/pacer.desktop"
      rm -f "$CFGBASE/autostart/fpslimiter.desktop"
    fi
    echo "removed the old fpslimiter install"
  fi
  mkdir -p "$CFG/presets" 2>/dev/null || true
fi
echo "installed to $D$PREFIX (bin: pacer, pacer-ctl, pacer-run)"
echo "Vulkan/DXVK/VKD3D/Proton games are hooked automatically; restart running games."
echo "OpenGL games: launch with  pacer-run <game>  (Steam launch options: pacer-run %command%)"
