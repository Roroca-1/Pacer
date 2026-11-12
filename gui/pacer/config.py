"""Config / preset files shared with the layer (simple `key = value` text files).

~/.config/pacer/settings.conf          global settings (hotkeys, OSD, defaults)
~/.config/pacer/presets/<key>.conf     one preset per program key
~/.local/state/pacer/run/<key>.<pid>.shm   live status published by hooked games
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MODES = ["smooth", "latency", "balanced"]
PRESENT_MODES = ["keep", "fifo", "mailbox", "immediate", "fifo_relaxed"]

DEFAULT_SETTINGS = {
    "hotkeys": "1",
    "hotkey_up": "ctrl+shift+pgup",
    "hotkey_down": "ctrl+shift+pgdn",
    "hotkey_toggle": "ctrl+shift+end",
    "hotkey_mode": "ctrl+shift+home",
    "step": "5",
    "osd": "1",
    "osd_seconds": "2.5",
    "osd_always": "0",
    "default_enabled": "0",
    "default_fps": "60",
    "default_mode": "smooth",
}


def config_dir() -> Path:
    if o := os.environ.get("PACER_CONFIG_DIR"):
        return Path(o)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
    new = base / "pacer"
    old = base / "fpslimiter"            # pre-0.1 name: carry settings and presets over once
    if not new.exists() and old.is_dir():
        try:
            import shutil
            shutil.copytree(old, new)
        except OSError:
            pass
    return new


def state_dir() -> Path:
    if o := os.environ.get("PACER_STATE_DIR"):
        return Path(o)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "pacer"


def presets_dir() -> Path:
    return config_dir() / "presets"


def sanitize_key(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9.\-_+]", "_", s)[:60]
    return s or "unknown"


def basename_any(p: str) -> str:
    return re.split(r"[/\\]", p)[-1]


def read_kv(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line[0] in "#;[" or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out.append((k.strip(), v.strip()))
    except FileNotFoundError:
        pass
    return out


def write_kv(path: Path, kv: list[tuple[str, str]] | dict, header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = kv.items() if isinstance(kv, dict) else kv
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        if header:
            f.write(f"# {header}\n")
        for k, v in items:
            f.write(f"{k} = {v}\n")
    os.replace(tmp, path)


def parse_bool(s: str | None, default=False) -> bool:
    if s is None:
        return default
    return s.strip().lower() in ("1", "true", "yes", "on")


def _mode(s: str) -> str:
    s = s.strip().lower()
    return {"lowlatency": "latency", "predictive": "balanced", "0": "smooth", "1": "latency",
            "2": "balanced"}.get(s, s if s in MODES else "smooth")


@dataclass
class Preset:
    key: str
    name: str = ""
    enabled: bool = True
    fps: float = 60.0
    mode: str = "smooth"
    present_mode: str = "keep"
    extra: dict = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return presets_dir() / f"{self.key}.conf"

    @classmethod
    def load(cls, key: str) -> "Preset | None":
        p = presets_dir() / f"{key}.conf"
        if not p.exists():
            return None
        d = dict(read_kv(p))
        try:
            fps = float(d.pop("fps", "60"))
        except ValueError:
            fps = 60.0
        pr = cls(
            key=key,
            name=d.pop("name", key),
            enabled=parse_bool(d.pop("enabled", "1"), True),
            fps=fps,
            mode=_mode(d.pop("mode", "smooth")),
            present_mode=d.pop("present_mode", "keep"),
        )
        pr.extra = d
        return pr

    def save(self) -> None:
        kv = {
            "name": self.name or self.key,
            "enabled": "1" if self.enabled else "0",
            "fps": f"{self.fps:g}",
            "mode": self.mode,
            "present_mode": self.present_mode,
        }
        kv.update(self.extra)
        write_kv(self.path, kv, "pacer preset")

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def list_presets() -> list[Preset]:
    out = []
    d = presets_dir()
    if d.is_dir():
        for p in sorted(d.glob("*.conf")):
            pr = Preset.load(p.stem)
            if pr:
                out.append(pr)
    return out


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(dict(read_kv(config_dir() / "settings.conf")))
    return s


def save_settings(s: dict) -> None:
    write_kv(config_dir() / "settings.conf", s, "pacer global settings")
