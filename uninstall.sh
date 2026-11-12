#!/bin/sh
# Removes the per-user install. Presets in ~/.config/pacer are kept.
PREFIX="${PREFIX:-$HOME/.local}"
rm -f "$PREFIX/share/vulkan/implicit_layer.d/pacer.x86_64.json" \
      "$PREFIX/share/vulkan/implicit_layer.d/pacer.i386.json" \
      "$PREFIX/bin/pacer" "$PREFIX/bin/pacer-ctl" "$PREFIX/bin/pacer-run" \
      "$PREFIX/share/applications/pacer.desktop" "$PREFIX/bin/fpslimiter-run"
rm -rf "$PREFIX/lib/pacer" "$PREFIX/share/pacer"
echo "pacer removed (presets kept in ${XDG_CONFIG_HOME:-$HOME/.config}/pacer)"
