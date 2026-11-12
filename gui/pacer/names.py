"""Human-readable game titles for process names — offline only.

Lookup order (first hit wins):
  1. Steam: appid from the process environment / steamapps/common/<dir>  (local appmanifests)
  2. Game launchers installed on this machine: Faugus, Lutris, Heroic   (their own local libraries)
  3. Executable → title index (~/.cache/pacer/exe_titles.tsv.gz, ~25k apps), built locally from
     Discord's public game list. Downloaded in the background on first run (and monthly), unless
     disabled in Settings. It is not shipped with Pacer.
  4. Initials of known titles: "ffxvi" → "Final Fantasy XVI", "p4g" → "Persona 4 Golden"
  5. Install folder name, then a tidied executable name.
Nothing about your processes is ever sent anywhere; the only network access is one plain GET of that
public list.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pacer" / "exe_titles.tsv.gz"
DISCORD_URL = "https://discord.com/api/v9/applications/detectable"

# executable names too generic to identify a game without a matching folder
GENERIC = {"game.exe", "launcher.exe", "client.exe", "start.exe", "main.exe", "app.exe", "run.exe",
           "play.exe", "setup.exe", "engine.exe", "unitycrashhandler64.exe", "nw.exe", "electron.exe",
           "python.exe", "java.exe", "javaw.exe", "godot.exe", "love.exe", "renpy.exe", "rpg_rt.exe",
           "game", "launcher", "main", "run.sh", "start.sh", "game.x86_64", "nw"}
_GENERIC_DIRS = {"bin", "bin32", "bin64", "binaries", "win64", "win32", "x64", "x86", "x86_64", "game",
                 "retail", "shipping", "release", "program files", "program files (x86)", "drive_c",
                 "common", "games", "build", "client", "app", "data", "steamapps", "pfx", "users",
                 "usr", "lib", "lib64", "opt", "local", "share", "tmp", "home", "sbin", "mnt"}
_STRIP = re.compile(r"(?i)([-_ ]?(win64|win32|x64|x86|64|32|shipping|dx11|dx12|vulkan|vk|gl|steam|epic|"
                    r"retail|release|launcher|game|client|demo))+$")
_ROMAN = re.compile(r"^(?=[ivxlc]+$)c{0,3}(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower()


def _stem(exe: str) -> str:
    b = _norm(exe).rsplit("/", 1)[-1]
    for ext in (".exe", ".x86_64", ".x86", ".bin", ".sh", ".appimage"):
        if b.endswith(ext):
            b = b[: -len(ext)]
    return b


def _words(title: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", title.lower().replace("&", " and "))


def acronyms(title: str) -> set[str]:
    """Plausible abbreviations: 'Final Fantasy XVI' → ffxvi, ff16; 'Persona 4 Golden' → p4g."""
    w = _words(title)
    if not w:
        return set()
    out = set()
    for drop_small in (False, True):
        ws = [x for x in w if not (drop_small and x in ("the", "of", "a", "an", "and", "to", "in", "on"))]
        if not ws:
            continue
        a = "".join(x if (x.isdigit() or _ROMAN.match(x)) else x[0] for x in ws)
        out.add(a)
    return {a for a in out if len(a) >= 2}


_TIDY_STRIP = re.compile(r"(?i)([-_ ]?(win64|win32|x64|x86|shipping|dx11|dx12|retail|release))+$")


def _tidy(stem: str) -> str:
    s = _TIDY_STRIP.sub("", stem) or stem
    s = re.sub(r"[_\-.]+", " ", s).strip()
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    return s[:1].upper() + s[1:] if s else stem


class NameResolver:
    def __init__(self):
        self._index: dict[str, list[tuple[str, str]]] | None = None
        self._launchers: list[tuple[str, str]] = []   # (normalized exe path or dir prefix, title)
        self._launchers_t = 0.0
        self._cache: dict[tuple[str, str, int], str] = {}
        self._known: set[str] = set()

    # -- sources
    def _load_index(self):
        if self._index is not None:
            return
        idx: dict[str, list[tuple[str, str]]] = {}
        try:
            for line in gzip.decompress(CACHE.read_bytes()).decode("utf-8", "replace").splitlines():
                path, _, title = line.partition("\t")
                if title:
                    idx.setdefault(path.rsplit("/", 1)[-1], []).append((path, title))
        except (OSError, EOFError):
            pass
        self._index = idx

    def _load_launchers(self):
        if time.time() - self._launchers_t < 60:
            return
        self._launchers_t = time.time()
        out: list[tuple[str, str]] = []
        home = Path.home()
        # Faugus Launcher
        try:
            for g in json.loads((home / ".local/share/faugus-launcher/games.json").read_text()):
                p, t = str(g.get("path", "")), str(g.get("title", ""))
                if t and "/" in p:
                    out.append((_norm(p), t))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        # Heroic (Legendary/GOG/Nile/sideloaded)
        hero = home / ".config/heroic"
        for f in (hero / "sideload_apps/library.json", hero / "store_cache/gog_library.json",
                  hero / "store_cache/legendary_library.json", hero / "store_cache/nile_library.json"):
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            games = data.get("games") or data.get("library") or [] if isinstance(data, dict) else data
            for g in games if isinstance(games, list) else []:
                if not isinstance(g, dict):
                    continue
                t = g.get("title") or ""
                inst = g.get("install") or {}
                p = (inst.get("executable") or inst.get("install_path") or g.get("folder_name") or "")
                if t and isinstance(p, str) and "/" in p:
                    out.append((_norm(p).rstrip("/") + ("" if p.lower().endswith(".exe") else "/"), t))
        # Lutris
        db = home / ".local/share/lutris/pga.db"
        if db.exists():
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2)
                for name, directory in con.execute("select name, directory from games where directory != ''"):
                    if name and directory:
                        out.append((_norm(directory).rstrip("/") + "/", name))
                con.close()
            except sqlite3.Error:
                pass
        self._launchers = out

    # -- lookup
    def title(self, exe: str, steam_title: str = "", appid: int = 0) -> str:
        """Best display title for an executable path (Windows or Unix)."""
        ck = (exe, steam_title, appid)
        if ck in self._cache:
            return self._cache[ck]
        t = steam_title or self._lookup(exe)
        self._cache[ck] = t
        return t

    def known(self, exe: str) -> bool:
        """True if the executable is a known game (launcher entry or index match)."""
        self.title(exe)
        return exe in self._known

    def _lookup(self, exe: str) -> str:
        low = _norm(exe)
        base = low.rsplit("/", 1)[-1]
        # 2. launchers
        self._load_launchers()
        best, blen = "", 0
        for p, t in self._launchers:
            if (p == low or (p.endswith("/") and low.startswith(p))) and len(p) > blen:
                best, blen = t, len(p)
        if best:
            self._known.add(exe)
            return best
        # 3. bundled index (match the longest path suffix; generic names need a folder match)
        self._load_index()
        cands = self._index.get(base, []) if self._index else []
        if not cands and self._index and "." not in base and len(base) > 3 and base + ".exe" not in GENERIC:
            # native Linux build of a game known by its Windows executable (osu! → osu!.exe)
            cands = [(p[:-4], t) for p, t in self._index.get(base + ".exe", [])]
        best, blen = "", -1
        for path, t in cands:
            if low.endswith("/" + path) or low == path:
                n = path.count("/")
                if n > blen:
                    best, blen = t, n
        if best and (blen > 0 or base not in GENERIC):
            self._known.add(exe)
            return best
        if cands and base not in GENERIC and len({t for _, t in cands}) == 1:
            self._known.add(exe)
            return cands[0][1]
        # 4. initials of known titles (installed launcher games + index titles sharing this stem)
        stem = _STRIP.sub("", _stem(exe)) or _stem(exe)
        if 2 <= len(stem) <= 12 and stem.isalnum():
            pool = {t for _, t in self._launchers}
            pool.update(_steam_titles())
            for t in sorted(pool):
                if stem in acronyms(t):
                    return t
        # 5. folder / tidy executable name
        raw = exe.replace("\\", "/").rsplit("/", 1)[-1]
        raw = re.sub(r"(?i)\.(exe|x86_64|x86|bin|sh|appimage)$", "", raw)
        return _folder_title(exe) or _tidy(raw)

    # -- download / refresh
    @staticmethod
    def index_age_days() -> float | None:
        try:
            return (time.time() - CACHE.stat().st_mtime) / 86400
        except OSError:
            return None

    @staticmethod
    def update_index(timeout: float = 60) -> int:
        req = urllib.request.Request(DISCORD_URL, headers={"User-Agent": "pacer"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            apps = json.load(r)
        blob = build_index(apps)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_name(CACHE.name + ".tmp")
        tmp.write_bytes(blob)
        tmp.replace(CACHE)
        return len(blob)


def build_index(apps) -> bytes:
    """Discord 'detectable applications' JSON → gzip TSV "<exe path suffix>\t<title>" (games only)."""
    rows = set()
    for a in apps:
        title = " ".join(str(a.get("name", "")).split())
        if not title:
            continue
        for e in a.get("executables") or []:
            if e.get("is_launcher") or e.get("os") not in ("win32", "linux", None):
                continue
            n = str(e.get("name") or "").strip().lower().replace("\\", "/").lstrip(">").strip("/")
            if n and "\t" not in n:
                rows.add((n, title))
    return gzip.compress("\n".join(f"{p}\t{t}" for p, t in sorted(rows)).encode(), 9, mtime=0)


_steam_cache: tuple[float, set[str]] = (0.0, set())


def _steam_titles() -> set[str]:
    global _steam_cache
    if time.time() - _steam_cache[0] < 60:
        return _steam_cache[1]
    titles: set[str] = set()
    try:
        from .live import SteamLibrary
        s = SteamLibrary()
        s.refresh(force=True)
        titles = set(s.names.values())
    except Exception:
        pass
    _steam_cache = (time.time(), titles)
    return titles


def _folder_title(exe: str) -> str:
    parts = [p for p in exe.replace("\\", "/").split("/") if p][:-1]
    for d in reversed(parts[-4:]):
        if d.lower() not in _GENERIC_DIRS and not d.endswith(":") and len(d) > 2 and not d.startswith("."):
            return d
    return ""


resolver = NameResolver()
