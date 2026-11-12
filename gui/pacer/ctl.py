"""pacer-ctl: change the limit of the running game from a terminal or a
desktop-environment global shortcut (works for native Wayland games too).

  pacer-ctl up|down|toggle|on|off|mode [smooth|latency|balanced]
  pacer-ctl set 72          # set fps
  pacer-ctl status
Options: --key <preset key> (default: the most recently presenting hooked game)
"""
from __future__ import annotations

import math
import re
import sys

from .config import MODES, Preset, load_settings
from .live import LiveRegistry, MODE_NAMES


def _target(key: str | None):
    live = LiveRegistry().poll()
    if key:
        return key, next((s for s in live if s.key == key), None)
    if not live:
        return None, None
    s = max(live, key=lambda s: s.last_present_ns)
    return s.key, s


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    key = None
    if "--key" in argv:
        i = argv.index("--key")
        key = argv[i + 1]
        del argv[i:i + 2]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "status":
        live = LiveRegistry().poll()
        if not live:
            print("no hooked games running")
        for s in live:
            ft = s.recent(120)
            fps = 1000 / (sum(ft) / len(ft)) if ft else 0
            print(f"{s.key:24s} pid {s.pid:<7d} {s.api_name:7s} "
                  f"{'ON ' if s.enabled else 'OFF'} target {s.fps_target:g} "
                  f"mode {MODE_NAMES[s.mode] if s.mode < 3 else s.mode}  actual {fps:.1f} fps")
        return 0

    key, st = _target(key)
    if not key:
        print("no hooked game running (use --key NAME)", file=sys.stderr)
        return 1
    settings = load_settings()
    step = float(settings.get("step", 5) or 5)
    pr = Preset.load(key)
    if pr is None:
        pr = Preset(key=key, name=key, enabled=True,
                    fps=st.fps_target if st else float(settings.get("default_fps", 60)),
                    mode=MODE_NAMES[st.mode] if st and st.mode < 3 else "smooth")
    values = sorted({float(v) for v in re.findall(r"[0-9]+(?:\.[0-9]+)?", settings.get("hotkey_values", ""))
                     if 1 <= float(v) <= 20000})
    if cmd == "up":
        if values:   # same preset list the in-game hotkeys cycle through
            pr.fps = next((v for v in values if v > pr.fps + 1e-3), values[-1])
        else:
            pr.fps = math.floor(pr.fps / step + 1e-3) * step + step
        pr.enabled = True
    elif cmd == "down":
        if values:
            pr.fps = next((v for v in reversed(values) if v < pr.fps - 1e-3), values[0])
        else:
            pr.fps = max(5.0, math.ceil(pr.fps / step - 1e-3) * step - step)
        pr.enabled = True
    elif cmd == "toggle":
        pr.enabled = not pr.enabled
    elif cmd in ("on", "off"):
        pr.enabled = cmd == "on"
    elif cmd == "set" and len(argv) > 1:
        pr.fps = max(1.0, min(20000.0, float(argv[1])))
        pr.enabled = True
    elif cmd == "mode":
        pr.mode = argv[1] if len(argv) > 1 and argv[1] in MODES else MODES[(MODES.index(pr.mode) + 1) % 3]
    else:
        print(__doc__, file=sys.stderr)
        return 2
    pr.fps = min(pr.fps, 20000.0)
    pr.save()
    print(f"{key}: {'on' if pr.enabled else 'off'} {pr.fps:g} fps {pr.mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
