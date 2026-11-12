"""Readers for the live status files and for running processes."""
from __future__ import annotations

import fcntl
import mmap
import os
import struct
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .names import resolver
from .config import basename_any, sanitize_key, state_dir

MAGIC = 0x52434150
RING = 256
SIZE = 2320          # version 1 (minimum)
SIZE_V2 = 2392       # + long-run statistics
API_NAMES = {1: "Vulkan", 2: "OpenGL", 3: "OpenGL (EGL)"}
PRESENT_MODE_NAMES = {0: "immediate", 1: "mailbox", 2: "fifo", 3: "fifo_relaxed"}
MODE_NAMES = ["smooth", "latency", "balanced"]

_HDR = struct.Struct("<IIII64s128sqIIfIIIIIQqII")
assert _HDR.size == 272
_STATS = struct.Struct("<QdddddqfII")      # @2320: n sum sumsq absdev s_sum s_sumsq reset target hitches s_n
assert _STATS.size == 2392 - 2320 - 4


@dataclass
class Stats:
    """Pacing statistics accumulated by the layer since the last change of limit / method."""
    n: int = 0
    mean: float = 0.0          # mean frametime (ms)
    sigma: float = 0.0         # standard deviation of frametime (ms)
    abs_dev: float = 0.0       # mean |frametime - target| (ms)
    start_sigma: float = 0.0   # σ of frame-start intervals (when the game samples input)
    target: float = 0.0        # ms, 0 = unlimited
    hitches: int = 0
    reset_ns: int = 0

    @staticmethod
    def from_sums(n, s, ss, ad, s_n, s_s, s_ss, target, hitches, reset_ns) -> "Stats":
        st = Stats(n=n, target=target, hitches=hitches, reset_ns=reset_ns)
        if n > 1:
            st.mean = s / n
            st.sigma = max(0.0, ss / n - st.mean ** 2) ** 0.5
            st.abs_dev = ad / n
        if s_n > 1:
            m = s_s / s_n
            st.start_sigma = max(0.0, s_ss / s_n - m * m) ** 0.5
        return st

    def minus(self, raw0: tuple | None, raw: tuple) -> "Stats":
        """Statistics since a GUI-side reset snapshot (raw tuples from LiveStatus.raw_stats)."""
        if not raw0 or raw0[6] != raw[6]:      # layer reset in between → layer values are newer
            r = raw
        else:
            r = tuple(a - b for a, b in zip(raw[:6], raw0[:6])) + raw[6:7] + (raw[7],) \
                + (raw[8] - raw0[8], raw[9] - raw0[9])
        n, s, ss, ad, s_s, s_ss, reset, tgt, hitches, s_n = r
        return Stats.from_sums(n, s, ss, ad, s_n, s_s, s_ss, tgt, hitches, reset)


@dataclass
class LiveStatus:
    path: Path
    pid: int
    api: int
    key: str
    exe: str
    start_ns: int
    enabled: bool
    mode: int
    fps_target: float
    has_preset: bool
    present_mode: int
    width: int
    height: int
    frames: int
    last_present_ns: int
    ring_head: int
    flags: int
    frametimes: list[float]
    waits: list[float]
    raw_stats: tuple | None = None   # version-2 layer only

    @property
    def stats(self) -> Stats | None:
        return Stats().minus(None, self.raw_stats) if self.raw_stats else None

    @property
    def background(self) -> bool:
        return bool(self.flags & 1)

    @property
    def api_name(self) -> str:
        return API_NAMES.get(self.api, "?")

    @property
    def present_mode_name(self) -> str:
        return PRESENT_MODE_NAMES.get(self.present_mode, "-")

    def recent(self, n: int) -> list[float]:
        """last n frametimes in chronological order (zeros skipped)."""
        h = self.ring_head
        seq = [self.frametimes[(h - n + i) % RING] for i in range(n)]
        return [x for x in seq if x > 0]

    def recent_waits(self, n: int) -> list[float]:
        h = self.ring_head
        return [self.waits[(h - n + i) % RING] for i in range(n)]


class StatusFile:
    """Keeps one shm file mapped; cheap to poll at GUI rates."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        # older layers publish the 2320-byte version-1 block (no statistics)
        self.size = SIZE_V2 if os.fstat(self.fd).st_size >= SIZE_V2 else SIZE
        self.mm = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED, mmap.PROT_READ)

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)

    def alive(self) -> bool:
        # the hooked process holds an exclusive flock for its whole lifetime
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    def read(self) -> LiveStatus | None:
        b = self.mm[:self.size]
        h = _HDR.unpack_from(b, 0)
        if h[0] != MAGIC:
            return None
        ft = list(struct.unpack_from(f"<{RING}f", b, 272))
        wt = list(struct.unpack_from(f"<{RING}f", b, 272 + RING * 4))
        raw = _STATS.unpack_from(b, SIZE) if self.size >= SIZE_V2 and h[1] >= 2 else None
        return LiveStatus(
            path=self.path, pid=h[2], api=h[3],
            key=h[4].split(b"\0", 1)[0].decode(errors="replace"),
            exe=h[5].split(b"\0", 1)[0].decode(errors="replace"),
            start_ns=h[6], enabled=bool(h[7]), mode=h[8], fps_target=h[9], has_preset=bool(h[10]),
            present_mode=h[11], width=h[12], height=h[13], frames=h[15], last_present_ns=h[16],
            ring_head=h[17], flags=h[18], frametimes=ft, waits=wt, raw_stats=raw,
        )


class LiveRegistry:
    """Tracks every live hooked process via the run directory."""

    def __init__(self):
        self.files: dict[Path, StatusFile] = {}

    def poll(self) -> list[LiveStatus]:
        run = state_dir() / "run"
        seen = set()
        out = []
        if run.is_dir():
            for p in run.glob("*.shm"):
                seen.add(p)
                sf = self.files.get(p)
                if sf is None:
                    try:
                        if p.stat().st_size < SIZE:
                            continue
                        sf = StatusFile(p)
                    except OSError:
                        continue
                    if not sf.alive():
                        sf.close()
                        # stale file of a crashed/exited game
                        try:
                            if time.time() - p.stat().st_mtime > 5:
                                p.unlink()
                        except OSError:
                            pass
                        continue
                    self.files[p] = sf
                st = sf.read()
                if st:
                    out.append(st)
        for p in list(self.files):
            if p not in seen:
                self.files.pop(p).close()
        # periodically drop dead ones (cheap flock probe)
        for p, sf in list(self.files.items()):
            if not sf.alive():
                sf.close()
                self.files.pop(p)
                try:
                    p.unlink()
                except OSError:
                    pass
                out = [s for s in out if s.path != p]
        return out


# ------------------------------------------------------------------ processes
GFX_MARKERS = (
    ("libvulkan", "Vulkan"), ("dxvk", "DXVK"), ("d3d12", "VKD3D"), ("vkd3d", "VKD3D"),
    ("wined3d", "WineD3D"), ("libGLX", "OpenGL"), ("libGL.so", "OpenGL"), ("libEGL", "OpenGL"),
)
# graphics APIs that only games / Wine use (desktop programs use plain GL/EGL via their toolkit)
GAME_GFX = {"DXVK", "VKD3D", "WineD3D"}
GAME_DIRS = ("/steamapps/common/", "/games/", "/heroic/", "/lutris/", "/bottles/", "/itch/",
             "/gog games/", "/epic games/", "/drive_c/")
# Steam / Proton / Wine plumbing that inherits SteamAppId or looks like a game
NOT_GAMES = {
    "steam", "steamwebhelper", "steam.exe", "reaper", "pressure-vessel-wrap", "pressure-vessel-adverb",
    "pv-bwrap", "srt-bwrap", "steam-runtime-launcher-service", "steam-runtime-launch-client",
    "gameoverlayui", "proton", "python3", "python", "sh", "bash", "fish", "zsh", "wineserver",
    "wine", "wine64", "wine-preloader", "wine64-preloader", "services.exe", "winedevice.exe",
    "plugplay.exe", "svchost.exe", "explorer.exe", "rpcss.exe", "tabtip.exe", "rundll32.exe",
    "conhost.exe", "start.exe", "winemenubuilder.exe", "wineboot.exe", "iexplore.exe", "msedgewebview2.exe",
    "crashpad_handler", "crashreporter", "unitycrashhandler64.exe", "unitycrashhandler32.exe",
    "easyanticheat", "easyanticheat_eos.exe", "start_protected_game.exe", "battleye", "beservice.exe",
    "pacer", "pacer-gui", "gamescope", "gamescopereaper", "mangohud", "xdg-desktop-portal",
    "systemd", "dbus-daemon", "dbus-broker", "pipewire", "wireplumber", "kwin_wayland", "kwin_x11",
    "plasmashell", "xwayland", "kded6", "krunner", "dolphin", "konsole", "baloo_file", "kactivitymanagerd",
}
STEAM_ENV = re.compile(rb"(?:^|\0)(?:SteamGameId|SteamAppId|STEAM_COMPAT_APP_ID)=(\d+)")


@dataclass
class ProcInfo:
    pid: int
    key: str
    name: str            # display name (Steam title if known)
    exe: str
    gfx: str             # "" if no graphics API detected
    wine: bool
    rss_mb: float
    appid: int = 0
    game: bool = False   # heuristic: looks like a game
    inspected: float = 0.0


def _proc_identity(pid: int) -> tuple[str, str, bool]:
    try:
        self_exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        self_exe = ""
    try:
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
    except OSError:
        argv0 = ""
    base = basename_any(self_exe)
    wine = "wine" in base or "preloader" in base
    if wine and argv0:
        return sanitize_key(basename_any(argv0)), argv0, True
    exe = self_exe or argv0
    return sanitize_key(base or basename_any(argv0)), exe, False


_GENERIC_DIRS = {"bin", "bin32", "bin64", "binaries", "win64", "win32", "x64", "x86", "x86_64", "game",
                 "retail", "shipping", "release", "program files", "program files (x86)", "drive_c",
                 "common", "games", "build", "client", "app", "data"}


def _folder_title(exe: str) -> str:
    """Best-effort title for non-Steam games: first non-generic parent directory."""
    parts = [p for p in exe.replace("\\", "/").split("/") if p][:-1]
    for d in reversed(parts[-4:]):
        if d.lower() not in _GENERIC_DIRS and not d.endswith(":") and len(d) > 2:
            return d
    return ""


class SteamLibrary:
    """App names from Steam's local appmanifest files (no network)."""
    ROOTS = ("~/.local/share/Steam", "~/.steam/steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")

    def __init__(self):
        self.names: dict[int, str] = {}
        self.dirs: dict[str, str] = {}   # lowercase install dir name -> title
        self._loaded = 0.0

    def refresh(self, force=False):
        if not force and time.time() - self._loaded < 60:
            return
        self._loaded = time.time()
        libs = set()
        for r in self.ROOTS:
            root = Path(os.path.expanduser(r))
            vdf = root / "steamapps/libraryfolders.vdf"
            if root.is_dir():
                libs.add(root.resolve())
            try:
                for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                    libs.add(Path(m.group(1).replace("\\\\", "\\")))
            except OSError:
                pass
        names, dirs = {}, {}
        for lib in libs:
            try:
                manifests = list((lib / "steamapps").glob("appmanifest_*.acf"))
            except OSError:
                continue
            for mf in manifests:
                try:
                    t = mf.read_text(errors="replace")
                except OSError:
                    continue
                a = re.search(r'"appid"\s+"(\d+)"', t)
                n = re.search(r'"name"\s+"([^"]*)"', t)
                d = re.search(r'"installdir"\s+"([^"]*)"', t)
                if a and n:
                    names[int(a.group(1))] = n.group(1)
                    if d:
                        dirs[d.group(1).lower()] = n.group(1)
        self.names, self.dirs = names, dirs

    def title_for(self, appid: int, exe: str) -> str:
        if appid and appid in self.names:
            return self.names[appid]
        low = exe.replace("\\", "/").lower()
        i = low.find("/steamapps/common/")
        if i >= 0:
            d = low[i + 18:].split("/", 1)[0]
            return self.dirs.get(d, "")
        return ""


class ProcessScanner:
    RECHECK = 5.0   # seconds: re-inspect game-like processes that have not loaded a graphics API yet

    def __init__(self):
        self.cache: dict[tuple[int, int], ProcInfo | None] = {}
        self.uid = os.getuid()
        self.steam = SteamLibrary()

    def scan(self, mode: str = "games", pinned: set[str] = frozenset(),
             hidden: set[str] = frozenset()) -> list[ProcInfo]:
        """mode: "games" (heuristic + pinned), "graphics" (any graphics API), "all"."""
        self.steam.refresh()
        out = []
        alive = set()
        now = time.time()
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            pid = int(d)
            try:
                st = os.stat(f"/proc/{pid}")
                if st.st_uid != self.uid:
                    continue
                stat = Path(f"/proc/{pid}/stat").read_text()
                fields = stat.rsplit(")", 1)[1].split()
                starttime = int(fields[19])
                rss_pages = int(fields[21])
            except (OSError, ValueError, IndexError):
                continue
            ck = (pid, starttime)
            alive.add(ck)
            info = self.cache.get(ck, 0)
            if info == 0 or (info is not None and not info.gfx and (info.game or info.wine)
                             and now - info.inspected > self.RECHECK):
                info = self.cache[ck] = self._inspect(pid)
            if info is None:
                continue
            info.rss_mb = rss_pages * 4096 / 2**20
            if info.key in hidden and mode != "all":
                continue
            if mode == "all" or info.key in pinned:
                out.append(info)
            elif mode == "graphics":
                if info.gfx and info.key not in NOT_GAMES:
                    out.append(info)
            elif info.game and info.gfx:
                out.append(info)
        for k in list(self.cache):
            if k not in alive:
                del self.cache[k]
        best: dict[str, ProcInfo] = {}
        for p in out:
            if p.key not in best or p.rss_mb > best[p.key].rss_mb:
                best[p.key] = p
        return sorted(best.values(), key=lambda p: (not p.game, not p.gfx, p.name.lower()))

    def _inspect(self, pid: int) -> ProcInfo | None:
        try:
            key, exe, wine = _proc_identity(pid)
        except Exception:
            return None
        if not exe:
            return None  # kernel thread
        gfx = ""
        try:
            with open(f"/proc/{pid}/maps", "rb") as f:
                data = f.read()
            for marker, name in GFX_MARKERS:
                if marker.encode() in data:
                    gfx = name
                    break
        except OSError:
            pass
        appid = 0
        try:
            m = STEAM_ENV.search(Path(f"/proc/{pid}/environ").read_bytes())
            if m:
                appid = int(m.group(1))
        except (OSError, ValueError):
            pass
        if appid in (0, 769):   # 769 = Steam client itself / non-Steam shortcut placeholder
            appid = 0
        low = exe.replace("\\", "/").lower()
        game = False
        if key not in NOT_GAMES and not key.startswith(("kworker", "steam")):
            if gfx in GAME_GFX:
                game = True
            elif appid:
                game = True
            elif wine and key.endswith(".exe"):
                game = True
            elif any(d in low for d in GAME_DIRS):
                game = True
            elif gfx and resolver.known(exe):   # listed in a launcher / the bundled game index
                game = True
        name = resolver.title(exe, self.steam.title_for(appid, exe), appid) if game else ""
        if not name:
            name = basename_any(exe)
            if name.lower().endswith(".exe"):
                name = name[:-4]
        return ProcInfo(pid=pid, key=key, name=name, exe=exe, gfx=gfx, wine=wine, rss_mb=0,
                        appid=appid, game=game, inspected=time.time())
