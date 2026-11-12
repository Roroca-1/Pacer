# Changelog

## 0.1.0 — 2026-11-11

First public release.

- Implicit Vulkan layer (native Vulkan, DXVK, VKD3D-Proton, Proton; 32- and 64-bit) and OpenGL preload hook (`pacer-run`).
- Three limiting methods: Smooth (absolute-grid frame pacing with constant frame-start phase), Low latency
  (wait after present), Balanced (predictive wake-up).
- Per-game presets applied automatically on launch; live reload of presets and settings.
- In-game hotkeys (X11/XWayland) that cycle refresh-rate divisors, a custom list, or a fixed step; `pacer-ctl` for
  desktop shortcuts on native Wayland; Vulkan OSD.
- Refresh-aware quick presets, locked to the exact display refresh rate.
- Live frametime graph, 1% lows, long-run frametime σ, deviation from target and frame-start σ.
- Background (unfocused) limit, Vulkan present-mode override, limits up to 20000 FPS.
- Game titles from Steam, Faugus, Lutris, Heroic and a downloaded public executable list (not bundled).
- Migrates presets from the pre-release name `fpslimiter` and removes its old install.
