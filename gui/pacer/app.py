"""pacer GUI (PySide6)."""
from __future__ import annotations

import os
import re
import statistics
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QKeyEvent,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                               QSlider, QStackedWidget, QStyle, QStyledItemDelegate, QSystemTrayIcon, QTabWidget,
                               QVBoxLayout, QWidget)

from .config import (MODES, PRESENT_MODES, Preset, config_dir, list_presets, load_settings,
                     presets_dir, sanitize_key, save_settings)
from .names import resolver
from .live import MODE_NAMES, NOT_GAMES, LiveRegistry, LiveStatus, ProcessScanner, Stats

ROOT = Path(__file__).resolve().parents[2]
ACCENT = "#2bd47c"
LAYER_MANIFESTS = [Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
                   / "vulkan/implicit_layer.d/pacer.x86_64.json"] + \
                  [Path(d) / "vulkan/implicit_layer.d/pacer.x86_64.json"
                   for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d] + \
                  [Path("/etc/vulkan/implicit_layer.d/pacer.x86_64.json")]
AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart/pacer.desktop"
FPS_MIN, FPS_MAX = 1.0, 20000.0
GRAPH_RATES = [("Off", 0), ("5 Hz", 5), ("10 Hz", 10), ("20 Hz", 20), ("30 Hz", 30), ("60 Hz", 60)]

MODE_INFO = {
    "smooth": ("Smooth", "Releases every frame on a fixed time grid and starts the next one at a constant phase "
                         "→ flat frametimes and even animation. Best for single-player, controllers, VRR / V-Sync."),
    "latency": ("Low latency", "Waits right after present, so the game samples input as late as possible. "
                               "Lowest input lag; for competitive games."),
    "balanced": ("Balanced", "Learns the frame's CPU time, sleeps after present and wakes just in time "
                             "for the grid. Near-Smooth pacing with lower latency."),
}


PRESENT_INFO = [
    ("Keep game setting", "Leave the game's own V-Sync choice alone.\nUse this unless you know you want something else."),
    ("FIFO (V-Sync)", "Classic V-Sync: frames wait for the display's refresh, no tearing.\n"
                      "Smoothest with the Smooth method and a limit at or below the refresh rate; with VRR "
                      "(FreeSync/G-Sync) cap a few fps below refresh for tear-free, low-lag play.\n"
                      "Adds latency if the game runs faster than the refresh rate."),
    ("Mailbox", "Fast V-Sync / triple buffering: the newest finished frame is shown at each refresh, older "
                "ones are dropped. No tearing and less lag than FIFO, but uneven frame pacing when the "
                "frame rate is not a multiple of the refresh rate.\nGood for high fps on a fixed-refresh display."),
    ("Immediate (tearing)", "No V-Sync: frames are shown the moment they are ready.\nLowest possible latency, "
                            "with visible tearing. Best for competitive games together with the Low latency method."),
    ("FIFO relaxed", "Adaptive V-Sync: synced when the game keeps up with the refresh rate, tears instead of "
                     "stuttering when a frame is late.\nA compromise for games that hover around the refresh rate."),
]


def preset_title(pr, fallback: str = "") -> str:
    """Preset name, unless it is just an executable file name (layer / manual add) → resolved title."""
    n = (pr.name or "").strip() if pr else ""
    if not n or n.lower() == pr.key or re.search(r"(?i)\.(exe|x86_64|x86|sh|bin)$", n):
        return fallback or resolver.title(n or pr.key)
    return n


def fmt_fps(v: float) -> str:
    return f"{v:.0f}" if abs(v - round(v)) < 0.005 else f"{v:.2f}"


# ------------------------------------------------------------------ style
def build_style(pt: float) -> str:
    small = max(7.0, pt - 1.0)
    return f"""
QMainWindow, QWidget#root, QDialog {{ background: #131519; color: #dfe2e7; }}
QWidget {{ color: #dfe2e7; }}
QToolTip {{ background: #22262d; color: #dfe2e7; border: 1px solid #3a3f49; padding: 3px; }}
QFrame#sidebar {{ background: #17191e; border-right: 1px solid #262931; }}
QFrame#card {{ background: #181b20; border: 1px solid #262a31; border-radius: 3px; }}
QLabel#title {{ font-size: {pt + 5:.1f}pt; font-weight: 600; }}
QLabel#h2 {{ font-weight: 600; color: #f0f2f5; }}
QLabel#muted {{ color: #8a919d; font-size: {small:.1f}pt; }}
QLabel#section {{ color: #7c8390; font-size: {small:.1f}pt; font-weight: 700; padding: 6px 2px 2px 2px; }}
QLabel#formlabel {{ color: #a3aab5; }}
QLabel#chip {{ background: #22262d; border-radius: 2px; padding: 2px 8px; color: #a3aab5; font-size: {small:.1f}pt; }}
QLabel#chipLive {{ background: #15382a; border-radius: 2px; padding: 2px 8px; color: {ACCENT}; font-size: {small:.1f}pt; }}
QLabel#chipBg {{ background: #3a3015; border-radius: 2px; padding: 2px 8px; color: #ffc861; font-size: {small:.1f}pt; }}
QLabel#stat {{ font-size: {pt + 3:.1f}pt; font-weight: 600; }}
QListWidget {{ background: transparent; border: none; outline: 0; }}
QListWidget::item {{ border-radius: 2px; }}
QListWidget::item:hover {{ background: #1f2228; }}
QListWidget::item:selected {{ background: #1d3328; }}
QPushButton {{ background: #23272e; border: 1px solid #30353e; border-radius: 3px; padding: 3px 10px; min-height: 18px; }}
QPushButton:hover {{ background: #2a2f37; border-color: #3a404a; }}
QPushButton:pressed {{ background: #1f2228; }}
QPushButton:checked {{ background: #1b3b2c; border-color: {ACCENT}; color: #ffffff; }}
QPushButton:disabled {{ color: #5d636d; }}
QPushButton#primary {{ background: {ACCENT}; color: #0b1a12; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: #45e290; }}
QPushButton#danger {{ background: transparent; color: #ff7070; border: 1px solid #4d2a2a; }}
QPushButton#danger:hover {{ background: #2a1a1a; }}
QPushButton#quick {{ padding: 3px 6px; min-width: 30px; }}
QPushButton#seg {{ border-radius: 0px; padding: 4px 14px; }}
QPushButton#segL {{ border-top-right-radius: 0; border-bottom-right-radius: 0; padding: 4px 14px; }}
QPushButton#segR {{ border-top-left-radius: 0; border-bottom-left-radius: 0; padding: 4px 14px; }}
QPushButton#nav {{ text-align: left; background: transparent; border: none; padding: 5px 8px; color: #a3aab5; }}
QPushButton#nav:hover {{ color: #ffffff; }}
QPushButton#nav:checked {{ background: #1f2228; color: #ffffff; }}
QPushButton#icon {{ padding: 0px; min-width: 22px; max-width: 22px; min-height: 20px; max-height: 20px; }}
QDoubleSpinBox#fps {{ font-size: {pt + 6:.1f}pt; font-weight: 600; padding: 1px 6px; min-width: 120px; }}
QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox {{ background: #0f1114; border: 1px solid #30353e;
    border-radius: 3px; padding: 2px 5px; min-height: 18px; selection-background-color: #1f5c3f; }}
QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: #181b20; border: 1px solid #30353e; selection-background-color: #1d3328; }}
QSlider::groove:horizontal {{ height: 4px; background: #2a2e36; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #e8eaed; width: 12px; margin: -5px 0; border-radius: 2px; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #4a505b; border-radius: 2px; background: #0f1114; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({{CHECK}}); }}
QTabWidget::pane {{ border: none; border-top: 1px solid #262a31; }}
QTabBar::tab {{ background: transparent; color: #8a919d; padding: 5px 14px; border: none;
    border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: #dfe2e7; }}
QTabBar::tab:selected {{ color: #ffffff; border-bottom: 2px solid {ACCENT}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: #2c3038; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QMenu {{ background: #1b1e23; border: 1px solid #30353e; padding: 3px; }}
QMenu::item {{ padding: 4px 18px; }}
QMenu::item:selected {{ background: #1d3328; }}
"""


def _check_image() -> str:
    d = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pacer"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "check.png"
    pm = QPixmap(28, 28)
    pm.fill(Qt.transparent)
    g = QPainter(pm)
    g.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("#0b1a12"), 4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    g.setPen(pen)
    path = QPainterPath()
    path.moveTo(6, 14.5)
    path.lineTo(11.5, 20)
    path.lineTo(22, 8)
    g.drawPath(path)
    g.end()
    pm.save(str(p))
    return str(p)


UI_FONTS = ["A-OTF Shin Go Pr6N", "A-OTF Shin Go Pro", "A-OTF Shin Go Pr5"]


def apply_appearance(app: QApplication, s: dict):
    """Fixed UI font (A-OTF Shin Go, system font if missing), size from settings (0 = system size)."""
    base = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    f = QFont(base)
    fams = set(QFontDatabase.families())
    for fam in UI_FONTS:
        if fam in fams:
            f.setFamilies([fam, base.family()])   # weights: M = normal, DB = semi-bold
            break
    try:
        size = float(s.get("gui_font_size", "0") or 0)
    except ValueError:
        size = 0
    if size <= 0:
        size = base.pointSizeF() if base.pointSizeF() > 0 else 10.0
    f.setPointSizeF(size)
    f.setHintingPreference(QFont.PreferFullHinting)   # crisp glyph edges
    f.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(f)
    app.setStyleSheet(build_style(size).replace("{CHECK}", _check_image()))


def refresh_presets(hz: float) -> list[float]:
    """Quick / hotkey FPS presets that divide evenly into the refresh rate.
    540 Hz → 30 60 90 108 135 180 270 540 1080; 240 Hz → 30 40 48 60 80 120 240 480 960;
    144 Hz → 36 48 72 144 288 576. Non-integer rates (59.94, 143.98) use exact fractions."""
    r = round(hz)
    # Monitors rarely run at exactly their nominal rate (a "540 Hz" panel reports 540.189 Hz). Within
    # 0.25 % use the nominal integer for the buttons; the layer snaps e.g. 90 → 540.189 / 6 itself
    # (settings key display_hz), so pacing stays locked to the real refresh.
    if r < 30 or abs(hz - r) > max(0.05, hz * 0.0025):
        return [v for v in (round(hz / d, 3) for d in (4, 3, 2)) if v >= 25] + [round(hz * m, 3) for m in (1, 2, 4)]
    divs = [r // d for d in range(r // 30, 0, -1) if r % d == 0]          # ascending, all >= 30
    nice = [v for v in divs if v % 10 == 0 or v % 12 == 0 or v == r]
    if len(nice) < 4:
        nice = divs
    if len(nice) > 8:                                                     # too many buttons:
        keep = {r, r // 2, nice[0]}                                       # prefer multiples of 30, then 60
        for v in sorted(nice, key=lambda v: (v % 30 != 0, v % 60 != 0, -v)):
            if len(keep) >= 8:
                break
            keep.add(v)
        nice = sorted(keep)
    ups = [r * 2, r * 4] if r * 4 <= 2000 else [r * 2]
    return [float(v) for v in nice + ups]


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    l = QLabel(text)
    if name:
        l.setObjectName(name)
    l.setWordWrap(wrap)
    return l


def card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(12, 9, 12, 10)
    lay.setSpacing(7)
    if title:
        lay.addWidget(label(title, "h2"))
    return f, lay


def repolish(w: QWidget, name: str):
    if w.objectName() != name:
        w.setObjectName(name)
        w.style().unpolish(w)
        w.style().polish(w)


def app_icon() -> QIcon:
    """Installed theme icon "pacer" (also used by the .desktop entry), else the SVG next to this file."""
    return QIcon.fromTheme("pacer", QIcon(str(Path(__file__).resolve().parent / "pacer.svg")))


# ------------------------------------------------------------------ sidebar rows
ROLE_DATA = Qt.UserRole          # (key, exe, name)
ROLE_DETAIL = Qt.UserRole + 1    # right-aligned dim text
ROLE_COLOR = Qt.UserRole + 2     # status dot colour


class RowDelegate(QStyledItemDelegate):
    """One compact line: ● name ............ detail"""

    def sizeHint(self, opt, idx):
        return QSize(100, opt.fontMetrics.height() + 10)

    def paint(self, g: QPainter, opt, idx):
        self.initStyleOption(opt, idx)
        opt.text = ""
        opt.widget.style().drawControl(QStyle.CE_ItemViewItem, opt, g, opt.widget)
        r = opt.rect.adjusted(8, 0, -8, 0)
        g.save()
        g.setRenderHint(QPainter.Antialiasing)
        col = idx.data(ROLE_COLOR)
        if col:
            g.setPen(Qt.NoPen)
            g.setBrush(QColor(col))
            g.drawEllipse(QRectF(r.left(), r.center().y() - 3, 6, 6))
            r.setLeft(r.left() + 13)
        detail = idx.data(ROLE_DETAIL) or ""
        fm = opt.fontMetrics
        dw = 0
        if detail:
            f = QFont(opt.font)
            f.setPointSizeF(max(7.0, opt.font.pointSizeF() - 1))
            g.setFont(f)
            dw = min(g.fontMetrics().horizontalAdvance(detail), r.width() // 2)
            g.setPen(QColor("#7c8390"))
            g.drawText(QRect(r.right() - dw, r.top(), dw, r.height()), Qt.AlignVCenter | Qt.AlignRight,
                       g.fontMetrics().elidedText(detail, Qt.ElideRight, dw))
        g.setFont(opt.font)
        sel = bool(opt.state & QStyle.State_Selected)
        g.setPen(QColor("#ffffff" if sel else "#dfe2e7") if idx.flags() & Qt.ItemIsEnabled else QColor("#5d636d"))
        tw = r.width() - dw - (8 if dw else 0)
        g.drawText(QRect(r.left(), r.top(), tw, r.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(idx.data(Qt.DisplayRole) or "", Qt.ElideRight, tw))
        g.restore()


# ------------------------------------------------------------------ graph
class FrameGraph(QWidget):
    def __init__(self):
        super().__init__()
        self.ft: list[float] = []
        self.waits: list[float] = []
        self.target_ms = 0.0
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, ft, waits, target_ms):
        self.ft, self.waits, self.target_ms = ft, waits, target_ms
        self.update()

    def paintEvent(self, ev):
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        g.fillRect(self.rect(), QColor("#0f1114"))
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1.5))
        g.setFont(f)
        lw = g.fontMetrics().horizontalAdvance("000.0") + 8
        r = self.rect().adjusted(lw, 6, -6, -6)
        ft = self.ft[-240:]
        top = max([self.target_ms * 2 if self.target_ms else 0, 2.0] + [x * 1.15 for x in ft])
        top = min(top, 100.0)
        for frac in (0, 0.5, 1.0):
            y = int(r.bottom() - frac * r.height())
            g.setPen(QPen(QColor("#1f2329"), 1))
            g.drawLine(r.left(), y, r.right(), y)
            g.setPen(QColor("#5d636d"))
            g.drawText(0, y - 7, lw - 5, 14, Qt.AlignRight | Qt.AlignVCenter, f"{top * frac:.1f}")
        if not ft:
            g.setPen(QColor("#5d636d"))
            g.drawText(r, Qt.AlignCenter, "no frames")
            return
        n = 240
        waits = self.waits[-len(ft):]
        dx = r.width() / (n - 1)
        x0 = r.right() - (len(ft) - 1) * dx

        def Y(v):
            return r.bottom() - min(v, top) / top * r.height()

        if waits and len(waits) == len(ft):
            p = QPainterPath()
            p.moveTo(x0, r.bottom())
            for i, w in enumerate(waits):
                p.lineTo(x0 + i * dx, Y(w))
            p.lineTo(x0 + (len(waits) - 1) * dx, r.bottom())
            g.fillPath(p, QColor(43, 212, 124, 34))
        if self.target_ms:
            g.setPen(QPen(QColor("#9aa1ab"), 1, Qt.DashLine))
            y = Y(self.target_ms)
            g.drawLine(r.left(), int(y), r.right(), int(y))
        p = QPainterPath()
        for i, v in enumerate(ft):
            (p.moveTo if i == 0 else p.lineTo)(x0 + i * dx, Y(v))
        g.setPen(QPen(QColor(ACCENT), 1.5))
        g.drawPath(p)


# ------------------------------------------------------------------ hotkey editor
_SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
            ")": "0", "_": "minus", "+": "equal", "{": "[", "}": "]"}
# X keysym names for punctuation (XStringToKeysym does not accept the bare characters)
_PUNCT = {",": "comma", ".": "period", "/": "slash", ";": "semicolon", "'": "apostrophe", "`": "grave",
          "\\": "backslash", "-": "minus", "=": "equal", "[": "bracketleft", "]": "bracketright",
          "<": "comma", ">": "period", "?": "slash", ":": "semicolon", '"': "apostrophe", "~": "grave",
          "|": "backslash"}
_KEYNAMES = {Qt.Key_PageUp: "pgup", Qt.Key_PageDown: "pgdn", Qt.Key_Home: "home", Qt.Key_End: "end",
             Qt.Key_Insert: "insert", Qt.Key_Delete: "delete", Qt.Key_Up: "up", Qt.Key_Down: "down",
             Qt.Key_Left: "left", Qt.Key_Right: "right", Qt.Key_Pause: "pause",
             Qt.Key_ScrollLock: "scrolllock", Qt.Key_Space: "space", Qt.Key_Minus: "minus",
             Qt.Key_Equal: "equal", Qt.Key_Plus: "plus", Qt.Key_BracketLeft: "[",
             Qt.Key_BracketRight: "]", Qt.Key_Tab: "tab", Qt.Key_Backtab: "tab", Qt.Key_Backspace: "backspace"}
_MODKEYS = {Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr, Qt.Key_Super_L,
            Qt.Key_Super_R, Qt.Key_Hyper_L, Qt.Key_Hyper_R, Qt.Key_CapsLock, Qt.Key_NumLock}


def hotkey_from_event(e: QKeyEvent) -> str | None:
    """Layer hotkey spec ("ctrl+shift+pgup") for a key event; None for bare modifiers."""
    k = Qt.Key(e.key()) if not isinstance(e.key(), Qt.Key) else e.key()
    if k in _MODKEYS:
        return None
    mods = e.modifiers()
    kv = int(k.value if hasattr(k, "value") else k)
    name = None
    if k in _KEYNAMES:
        name = _KEYNAMES[k]
    elif int(Qt.Key_F1.value) <= kv <= int(Qt.Key_F35.value):
        name = f"f{kv - int(Qt.Key_F1.value) + 1}"
    elif mods & Qt.KeypadModifier and e.text() in ("+", "-"):
        name = "kp_add" if e.text() == "+" else "kp_subtract"
    elif 0x20 < kv < 0x7F:
        ch = _SHIFTED.get(chr(kv).lower(), chr(kv).lower())
        name = _PUNCT.get(ch, ch)
    if not name:
        return ""
    parts = []
    if mods & Qt.ControlModifier:
        parts.append("ctrl")
    if mods & Qt.ShiftModifier:
        parts.append("shift")
    if mods & Qt.AltModifier:
        parts.append("alt")
    if mods & Qt.MetaModifier:
        parts.append("super")
    return "+".join(parts + [name])


class HotkeyEdit(QWidget):
    """Text field + Record button. While recording, every key press in the application is
    captured (event filter + keyboard grab), so focus handling, shortcut overrides and
    read-only line edits can no longer swallow the keys."""
    changed = Signal()

    def __init__(self, text: str = ""):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText("none")
        self.edit.setToolTip("Type e.g. ctrl+shift+pgup, alt+f5 — or click Record")
        self.edit.editingFinished.connect(self._typed)
        self.btn = QPushButton("Record")
        self.btn.setCheckable(True)
        self.btn.setFixedWidth(80)
        self.btn.toggled.connect(self._toggle)
        clr = QPushButton("✕")
        clr.setObjectName("icon")
        clr.setToolTip("Clear")
        clr.clicked.connect(lambda: self._set("none"))
        h.addWidget(self.edit, 1)
        h.addWidget(self.btn)
        h.addWidget(clr)
        self._last = text
        self._timeout = QTimer(self, singleShot=True, interval=10000, timeout=lambda: self.btn.setChecked(False))

    def text(self) -> str:
        return self.edit.text().strip() or "none"

    def _set(self, t: str):
        self.edit.setText(t)
        if t != self._last:
            self._last = t
            self.changed.emit()

    def _typed(self):
        t = self.edit.text().strip().lower().replace(" ", "")
        self._set(t or "none")

    def _toggle(self, on: bool):
        app = QApplication.instance()
        if on:
            self.btn.setText("Press keys…")
            self.edit.setText("")
            app.installEventFilter(self)
            self.btn.grabKeyboard()
            self._timeout.start()
        else:
            app.removeEventFilter(self)
            self.btn.releaseKeyboard()
            self._timeout.stop()
            self.btn.setText("Record")
            if not self.edit.text():
                self.edit.setText(self._last)

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:
        t = ev.type()
        if t == QEvent.ShortcutOverride:
            ev.accept()              # keep menu / window shortcuts from eating the combo
            return True
        if t == QEvent.KeyPress:
            spec = hotkey_from_event(ev)
            if spec is None:         # modifier only: show progress
                return True
            if ev.key() == Qt.Key_Escape and not ev.modifiers():
                self.btn.setChecked(False)
                return True
            if spec:
                self.btn.setChecked(False)
                self._set(spec)
            return True
        if t == QEvent.KeyRelease:
            return True
        return False


# ------------------------------------------------------------------ process picker
class PickProcessDialog(QDialog):
    def __init__(self, parent, procs):
        super().__init__(parent)
        self.setWindowTitle("Add program")
        self.resize(520, 480)
        self.result_key = self.result_name = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.addWidget(label("Pick a running process to create a preset for it. The program will always be "
                            "shown in the list from now on.", "muted", wrap=True))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)
        self.list = QListWidget()
        self.list.setItemDelegate(RowDelegate(self.list))
        self.list.setAlternatingRowColors(False)
        for p in sorted(procs, key=lambda p: (not p.game, not p.gfx, p.name.lower())):
            it = QListWidgetItem(p.name)
            it.setData(ROLE_DATA, (p.key, p.exe, p.name))
            it.setData(ROLE_DETAIL, " · ".join(x for x in (p.gfx, "Wine" if p.wine else "", p.key) if x))
            it.setData(ROLE_COLOR, ACCENT if p.game else ("#5aa9ff" if p.gfx else "#3a3f4a"))
            it.setToolTip(p.exe)
            self.list.addItem(it)
        self.list.itemDoubleClicked.connect(lambda _: self._accept())
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addWidget(label("…or executable name:", "formlabel"))
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("e.g. eldenring.exe")
        row.addWidget(self.manual, 1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Add")
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _filter(self, t):
        t = t.lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            k, exe, name = it.data(ROLE_DATA)
            it.setHidden(bool(t) and t not in name.lower() and t not in k and t not in exe.lower())

    def _accept(self):
        m = self.manual.text().strip()
        if m:
            self.result_key = sanitize_key(Path(m.replace("\\", "/")).name)
            self.result_name = resolver.title(m)
        elif self.list.currentItem() and not self.list.currentItem().isHidden():
            self.result_key, _, self.result_name = self.list.currentItem().data(ROLE_DATA)
        else:
            return
        self.accept()


# ------------------------------------------------------------------ preset page
class PresetPage(QWidget):
    changed = Signal()

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.key: str | None = None
        self.preset: Preset | None = None
        self._loading = False
        self._saved_sig = None
        self._name = self._exe = ""
        self.save_timer = QTimer(self, singleShot=True, interval=150, timeout=self._save)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("root")
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(10)

        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        self.title = label("", "title")
        hdr.addWidget(self.title)
        self.chip = label("", "chip")
        hdr.addWidget(self.chip, 0, Qt.AlignVCenter)
        hdr.addStretch(1)
        self.delete_btn = QPushButton("Delete preset")
        self.delete_btn.setObjectName("danger")
        self.delete_btn.clicked.connect(self.delete_preset)
        hdr.addWidget(self.delete_btn)
        lay.addLayout(hdr)
        self.subtitle = label("", "muted")
        self.subtitle.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.subtitle)

        # create card
        self.create_card, cl = card()
        row = QHBoxLayout()
        row.addWidget(label("No preset for this program. A preset is stored per executable and applied "
                            "automatically every time it starts.", "muted", wrap=True), 1)
        b = QPushButton("Create preset")
        b.setObjectName("primary")
        b.clicked.connect(self.create_preset)
        row.addWidget(b)
        cl.addLayout(row)
        lay.addWidget(self.create_card)

        # settings card (form)
        self.form_card, fl = card()
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        r = 0
        grid.addWidget(label("Limit", "formlabel"), r, 0)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.enabled = QCheckBox("Enabled")
        row.addWidget(self.enabled)
        self.fps = QDoubleSpinBox()
        self.fps.setObjectName("fps")
        self.fps.setRange(FPS_MIN, FPS_MAX)
        self.fps.setDecimals(2)
        self.fps.setSuffix(" fps")
        self.fps.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self.fps.setKeyboardTracking(False)
        self.fps.setToolTip(f"{FPS_MIN:g} – {FPS_MAX:g} fps. Values above 1000 are supported.")
        row.addWidget(self.fps)
        self.slider = QSlider(Qt.Horizontal)
        row.addWidget(self.slider, 1)
        self.frametime_lbl = label("", "muted")
        self.frametime_lbl.setMinimumWidth(70)
        row.addWidget(self.frametime_lbl)
        grid.addLayout(row, r, 1)
        r += 1
        self.quick_lbl = label("Quick", "formlabel")
        self.quick_lbl.setToolTip("FPS values that divide evenly into the display refresh rate (Settings → Display)")
        grid.addWidget(self.quick_lbl, r, 0)
        self.quick_row = QHBoxLayout()
        self.quick_row.setSpacing(4)
        self.quick_btns = []
        self.vrr_btn = QPushButton("VRR")
        self.vrr_btn.setObjectName("quick")
        self.vrr_btn.clicked.connect(lambda: self.fps.setValue(self.vrr_btn.property("fps")))
        self.quick_row.addWidget(self.vrr_btn)
        self.hz_lbl = label("", "muted")    # shown in the "Quick" row label tooltip / Settings
        self.quick_row.addStretch(1)
        grid.addLayout(self.quick_row, r, 1)
        r += 1
        grid.addWidget(label("Method", "formlabel"), r, 0)
        mrow = QHBoxLayout()
        mrow.setSpacing(0)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for i, m in enumerate(MODES):
            btn = QPushButton(MODE_INFO[m][0])
            btn.setObjectName("segL" if i == 0 else "segR" if i == len(MODES) - 1 else "seg")
            btn.setCheckable(True)
            btn.setToolTip(MODE_INFO[m][1])
            self.mode_group.addButton(btn, i)
            self.mode_buttons[m] = btn
            mrow.addWidget(btn)
        mrow.addStretch(1)
        grid.addLayout(mrow, r, 1)
        r += 1
        self.mode_desc = label("", "muted", wrap=True)
        grid.addWidget(self.mode_desc, r, 1)
        r += 1
        grid.addWidget(label("In background", "formlabel"), r, 0)
        brow = QHBoxLayout()
        brow.setSpacing(6)
        self.bg_combo = QComboBox()
        self.bg_combo.addItems(["Use global setting", "Don't change", "Limit to"])
        self.bg_combo.setToolTip("Frame-rate limit while the game window is not focused (alt-tabbed). "
                                 "Works for X11 / XWayland / Proton games.")
        brow.addWidget(self.bg_combo)
        self.bg_fps = QDoubleSpinBox()
        self.bg_fps.setRange(1, 1000)
        self.bg_fps.setDecimals(0)
        self.bg_fps.setSuffix(" fps")
        self.bg_fps.setValue(30)
        brow.addWidget(self.bg_fps)
        self.bg_hint = label("", "muted")
        brow.addWidget(self.bg_hint)
        brow.addStretch(1)
        grid.addLayout(brow, r, 1)
        r += 1
        grid.addWidget(label("Present mode", "formlabel"), r, 0)
        prow = QHBoxLayout()
        self.present = QComboBox()
        for i, (t, tip) in enumerate(PRESENT_INFO):
            self.present.addItem(t)
            self.present.setItemData(i, tip, Qt.ToolTipRole)
        self.present.currentIndexChanged.connect(
            lambda i: self.present.setToolTip(PRESENT_INFO[i][1] + "\n\nVulkan only; applied when the game "
                                              "(re)creates its swapchain (restart, resolution or V-Sync change)."))
        prow.addWidget(self.present)
        prow.addWidget(label("Vulkan; applies on swapchain re-creation", "muted"))
        prow.addStretch(1)
        grid.addLayout(prow, r, 1)
        fl.addLayout(grid)
        lay.addWidget(self.form_card)

        # live card
        self.live_card, lv = card()
        row = QHBoxLayout()
        row.addWidget(label("Live", "h2"))
        self.live_info = label("", "muted")
        row.addWidget(self.live_info, 1)
        self.graph_rate = QComboBox()
        for t, v in GRAPH_RATES:
            self.graph_rate.addItem("Graph " + t.lower() if v == 0 else f"Graph {t}", v)
        self.graph_rate.setToolTip("Frametime graph refresh rate (Off saves CPU; stats still update once a second)")
        row.addWidget(self.graph_rate)
        lv.addLayout(row)
        self.graph = FrameGraph()
        lv.addWidget(self.graph)
        stats = QGridLayout()
        stats.setHorizontalSpacing(16)
        stats.setVerticalSpacing(0)
        self.stat = {}
        for i, (k, t) in enumerate([("fps", "fps"), ("avg", "avg frametime"), ("low", "1% low"),
                                    ("sd", "frametime σ"), ("wait", "limiter wait")]):
            v = label("–", "stat")
            stats.addWidget(v, 0, i)
            stats.addWidget(label(t, "muted"), 1, i)
            self.stat[k] = v
        lv.addLayout(stats)
        # long-run averages accumulated by the layer itself (every frame, independent of graph rate)
        avg_row = QHBoxLayout()
        avg_row.setSpacing(16)
        self.avg_stat = {}
        avg_tips = {
            "asd": "Average frametime deviation: standard deviation of every frametime since the limit / method "
                   "last changed (or Reset). Lower = more even frame pacing.",
            "adev": "Mean absolute difference between each frametime and the target frametime.",
            "ssd": "σ of the time between frame starts (when the game reads input / advances its simulation). "
                   "Uneven frame starts look like judder even when presents are evenly spaced.",
            "n": "Frames in the averages; hitches = frames over 4× the target (loading screens) left out.",
        }
        for k, t in [("asd", "avg frametime σ"), ("adev", "avg |Δ target|"), ("ssd", "frame-start σ"),
                     ("n", "frames")]:
            col = QVBoxLayout()
            col.setSpacing(0)
            v = label("–", "stat")
            v.setToolTip(avg_tips[k])
            col.addWidget(v)
            col.addWidget(label(t, "muted"))
            avg_row.addLayout(col)
            self.avg_stat[k] = v
        avg_row.addStretch(1)
        self.avg_reset = QPushButton("Reset")
        self.avg_reset.setToolTip("Start the averages again from now")
        self.avg_reset.clicked.connect(self._reset_avg)
        avg_row.addWidget(self.avg_reset, 0, Qt.AlignVCenter)
        self._avg_base = None
        self._last_raw = None
        lv.addLayout(avg_row)
        lay.addWidget(self.live_card)
        self.hint = label("", "muted", wrap=True)
        lay.addWidget(self.hint)
        lay.addStretch(1)

        self.enabled.toggled.connect(self._edited)
        self.fps.valueChanged.connect(self._fps_changed)
        self.slider.valueChanged.connect(self._slider_moved)
        self.mode_group.idClicked.connect(lambda _: self._edited())
        self.present.currentIndexChanged.connect(lambda _: self._edited())
        self.bg_combo.currentIndexChanged.connect(lambda _: self._edited())
        self.bg_fps.valueChanged.connect(lambda _: self._edited())
        self.graph_rate.currentIndexChanged.connect(self._graph_rate_changed)

    # -- refresh-rate presets
    def update_refresh(self, hz: float):
        self.hz = hz
        vals = refresh_presets(hz)
        if [b.property("fps") for b in self.quick_btns] != vals:
            for b in self.quick_btns:
                self.quick_row.removeWidget(b)
                b.deleteLater()
            self.quick_btns = []
            for i, v in enumerate(vals):
                qb = QPushButton(fmt_fps(v))
                qb.setObjectName("quick")
                qb.setProperty("fps", v)
                ratio = v / hz
                d = round(1 / ratio) if ratio < 1 else round(ratio)
                exact = hz / d if ratio < 1 else hz * d
                qb.setToolTip(f"{v:g} fps = refresh " + (f"÷ {d}" if ratio < 1 else f"× {d}")
                              + (f"  (locked to {exact:.3f} fps on this {hz:.3f} Hz display)"
                                 if abs(exact - v) > 0.0005 else ""))
                qb.clicked.connect(lambda _=False, b=qb: self.fps.setValue(b.property("fps")))
                self.quick_row.insertWidget(i, qb)
                self.quick_btns.append(qb)
        vrr = round(hz - hz * hz / 3600, 1)
        self.vrr_btn.setProperty("fps", vrr)
        self.vrr_btn.setToolTip(f"VRR cap {vrr:g} fps (refresh − refresh²/3600): keeps FreeSync / G-Sync engaged")
        self.hz_lbl.setText(f"display {hz:.2f} Hz")
        self.quick_lbl.setText(f"Quick · {hz:.0f} Hz" if abs(hz - round(hz)) < 0.05 else f"Quick · {hz:.2f} Hz")
        self._loading = True
        self.slider.setRange(10, int(max(1000, hz * 4)))
        if self.preset:
            self.slider.setValue(int(round(self.preset.fps)))
        self._loading = False

    # -- data
    def show_key(self, key: str, name: str = "", exe: str = ""):
        if self.save_timer.isActive():
            self.save_timer.stop()
            self._save()
        self.key = key
        self.preset = Preset.load(key)
        self._name, self._exe = name or resolver.title(self.preset.name if self.preset else key), exe
        rate = int(self.win.settings_data.get("graph_rate", "20") or 0)
        self.graph_rate.blockSignals(True)
        self.graph_rate.setCurrentIndex(max(0, [v for _, v in GRAPH_RATES].index(rate)
                                            if rate in [v for _, v in GRAPH_RATES] else 3))
        self.graph_rate.blockSignals(False)
        self.graph.setVisible(rate > 0)
        self._avg_base = None
        self._load_widgets()
        self.update_live(None)

    def _load_widgets(self):
        pr = self.preset
        self._loading = True
        self.title.setText(preset_title(pr, self._name) if pr else self._name)
        sub = f"key: {self.key}"
        if self._exe:
            sub += f"   ·   {self._exe}"
        self.subtitle.setText(sub)
        has = pr is not None
        self.create_card.setVisible(not has)
        self.form_card.setVisible(has)
        self.delete_btn.setVisible(has)
        if has:
            self.enabled.setChecked(pr.enabled)
            self.fps.setValue(pr.fps)
            self.slider.setValue(int(round(pr.fps)))
            self.mode_buttons.get(pr.mode, self.mode_buttons["smooth"]).setChecked(True)
            self.mode_desc.setText(MODE_INFO.get(pr.mode, MODE_INFO["smooth"])[1])
            self.present.setCurrentIndex(PRESENT_MODES.index(pr.present_mode)
                                         if pr.present_mode in PRESENT_MODES else 0)
            bg = pr.extra.get("background_fps", "").strip()
            if bg in ("", "global"):
                self.bg_combo.setCurrentIndex(0)
            elif bg in ("0", "off"):
                self.bg_combo.setCurrentIndex(1)
            else:
                self.bg_combo.setCurrentIndex(2)
                try:
                    self.bg_fps.setValue(float(bg))
                except ValueError:
                    pass
            self._bg_widgets()
            self.frametime_lbl.setText(f"{1000 / pr.fps:.3f} ms")
            self._saved_sig = self._file_sig()
        self._loading = False

    def _bg_widgets(self):
        i = self.bg_combo.currentIndex()
        self.bg_fps.setVisible(i == 2)
        g = float(self.win.settings_data.get("background_fps", "0") or 0)
        self.bg_hint.setText((f"global: {g:g} fps" if g > 0 else "global: off") if i == 0 else "")

    def _file_sig(self):
        try:
            st = (presets_dir() / f"{self.key}.conf").stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def poll_external(self):
        """Reload when the preset was changed outside the GUI (in-game hotkey / ctl)."""
        if not self.key or self.save_timer.isActive():
            return
        if self._file_sig() != self._saved_sig:
            self.preset = Preset.load(self.key)
            self._load_widgets()
            self.changed.emit()

    def _slider_moved(self, v):
        if not self._loading:
            self.fps.setValue(v)

    def _fps_changed(self, v):
        if self._loading:
            return
        self._loading = True
        self.slider.setValue(int(round(v)))
        self._loading = False
        self.frametime_lbl.setText(f"{1000 / v:.3f} ms")
        self._edited()

    def _edited(self, *_):
        if self._loading or not self.preset:
            return
        self.preset.enabled = self.enabled.isChecked()
        self.preset.fps = round(self.fps.value(), 3)
        self.preset.mode = MODES[max(0, self.mode_group.checkedId())]
        self.mode_desc.setText(MODE_INFO[self.preset.mode][1])
        self.preset.present_mode = PRESENT_MODES[self.present.currentIndex()]
        i = self.bg_combo.currentIndex()
        if i == 0:
            self.preset.extra.pop("background_fps", None)
        else:
            self.preset.extra["background_fps"] = "off" if i == 1 else f"{self.bg_fps.value():g}"
        self._bg_widgets()
        self.save_timer.start()

    def _save(self):
        if self.preset:
            self.preset.save()
            self._saved_sig = self._file_sig()
            self.changed.emit()

    def _graph_rate_changed(self, _):
        v = self.graph_rate.currentData()
        self.graph.setVisible(v > 0)
        self.win.set_graph_rate(v)

    def create_preset(self):
        s = self.win.settings_data
        live = self.win.live_for(self.key)
        fps = live.fps_target if live and live.enabled and live.fps_target > 0 else float(s.get("default_fps", 60))
        self.preset = Preset(key=self.key, name=self._name, enabled=True, fps=fps,
                             mode=s.get("default_mode", "smooth"))
        self.preset.save()
        self._load_widgets()
        self.changed.emit()

    def delete_preset(self):
        if not self.preset:
            return
        if QMessageBox.question(self, "Delete preset", f"Delete the preset for “{self.preset.name}”?") \
                != QMessageBox.Yes:
            return
        self.save_timer.stop()
        self.preset.delete()
        self.preset = None
        self._load_widgets()
        self.changed.emit()

    # -- live
    def update_live(self, st: LiveStatus | None, draw_graph: bool = True):
        if st is None:
            repolish(self.chip, "chip")
            self.chip.setText("not running")
            self.live_info.setText("")
            self.graph.set_data([], [], 0)
            for v in self.stat.values():
                v.setText("–")
            self._update_avg(None)
            s = self.win.settings_data
            self.hint.setText(f"In game: {s.get('hotkey_up')} / {s.get('hotkey_down')} change the limit · "
                              f"{s.get('hotkey_toggle')} on/off · {s.get('hotkey_mode')} method")
            return
        if st.background:
            repolish(self.chip, "chipBg")
            self.chip.setText(f"background · {st.api_name}")
        else:
            repolish(self.chip, "chipLive")
            self.chip.setText(f"live · {st.api_name}")
        ft = st.recent(240)
        waits = st.recent_waits(len(ft)) if ft else []
        res = f"{st.width}×{st.height}  " if st.width else ""
        pm = f"{st.present_mode_name}  " if st.api == 1 and st.present_mode != 0xFFFFFFFF else ""
        state = (f"limit {fmt_fps(st.fps_target)} · {MODE_NAMES[st.mode] if st.mode < 3 else '?'}"
                 if st.enabled else "limiter off")
        self.live_info.setText(f"   pid {st.pid}  {res}{pm}{state}")
        if draw_graph and self.graph.isVisible():
            self.graph.set_data(ft, waits, 1000 / st.fps_target if st.enabled and st.fps_target > 0 else 0)
        if len(ft) >= 10:
            last = ft[-120:]
            avg = sum(last) / len(last)
            srt = sorted(last)
            p99 = srt[min(len(srt) - 1, int(len(srt) * 0.99))]
            self.stat["fps"].setText(f"{1000 / avg:.1f}")
            self.stat["avg"].setText(f"{avg:.2f} ms")
            self.stat["low"].setText(f"{1000 / p99:.1f}")
            self.stat["sd"].setText(f"{statistics.pstdev(last):.2f} ms")
            w = waits[-120:]
            self.stat["wait"].setText(f"{sum(w) / max(1, len(w)):.2f} ms")
        self._update_avg(st)

    def _reset_avg(self):
        self._avg_base = self._last_raw

    def _update_avg(self, st):
        raw = st.raw_stats if st else None
        self._last_raw = raw
        self.avg_reset.setEnabled(raw is not None)
        if raw is None:
            for v in self.avg_stat.values():
                v.setText("–")
            if st is not None:
                self.avg_stat["n"].setToolTip("Needs the updated layer: re-run install and restart the game")
            return
        a = Stats().minus(self._avg_base, raw)
        if a.n < 2:
            for k in ("asd", "adev", "ssd"):
                self.avg_stat[k].setText("–")
        else:
            self.avg_stat["asd"].setText(f"{a.sigma:.3f} ms")
            self.avg_stat["adev"].setText(f"{a.abs_dev:.3f} ms" if a.target > 0 else "–")
            self.avg_stat["ssd"].setText(f"{a.start_sigma:.3f} ms" if a.start_sigma > 0 else "–")
        secs = max(0.0, a.n * a.mean / 1000)
        dur = f"{secs:.0f} s" if secs < 120 else f"{secs / 60:.0f} min"
        self.avg_stat["n"].setText(f"{a.n}" + (f" · {dur}" if a.n > 1 else ""))
        self.avg_stat["n"].setToolTip(f"{a.n} frames over {dur}; {a.hitches} hitch(es) > 4× target excluded")


# ------------------------------------------------------------------ settings page
class SettingsPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        s = win.settings_data
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)
        outer.addWidget(label("Settings", "title"))
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs, 1)

        def tab(title):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body = QWidget()
            body.setObjectName("root")
            scroll.setWidget(body)
            tl = QVBoxLayout(body)
            tl.setContentsMargins(0, 10, 4, 4)
            tl.setSpacing(10)
            self.tabs.addTab(scroll, title)
            return tl

        t_general = tab("General")
        t_hotkeys = tab("Hotkeys")
        t_display = tab("Display && OSD")
        t_bg = tab("Background")
        t_app = tab("Application")

        def form(c_lay):
            g = QGridLayout()
            g.setHorizontalSpacing(12)
            g.setVerticalSpacing(6)
            g.setColumnMinimumWidth(0, 170)
            g.setColumnStretch(2, 1)
            c_lay.addLayout(g)
            return g

        def row(g, r, text, w, hint=""):
            g.addWidget(label(text, "formlabel"), r, 0)
            g.addWidget(w, r, 1)
            if hint:
                g.addWidget(label(hint, "muted"), r, 2)

        # installation
        c, cl = card("Installation")
        self.install_lbl = label("", "muted", wrap=True)
        cl.addWidget(self.install_lbl)
        r_ = QHBoxLayout()
        self.install_btn = QPushButton("Install / update")
        self.install_btn.setObjectName("primary")
        self.install_btn.clicked.connect(lambda: self._run_script("install.sh"))
        self.uninstall_btn = QPushButton("Uninstall")
        self.uninstall_btn.setObjectName("danger")
        self.uninstall_btn.clicked.connect(lambda: self._run_script("uninstall.sh"))
        r_.addWidget(self.install_btn)
        r_.addWidget(self.uninstall_btn)
        r_.addStretch(1)
        cl.addLayout(r_)
        if not (ROOT / "install.sh").exists():          # system package: managed by the package manager
            self.install_btn.hide()
            self.uninstall_btn.hide()
        cl.addWidget(label("Vulkan, DXVK (D3D9–11), VKD3D (D3D12) and Proton games are hooked automatically. "
                           "Native OpenGL games: Steam launch option  pacer-run %command%", "muted", wrap=True))
        t_general.addWidget(c)
        self.refresh_install()

        # hotkeys
        c, cl = card("In-game hotkeys")
        cl.addWidget(label("Read inside the game via X11/XWayland (Proton and most native games). For native "
                           "Wayland games bind  pacer-ctl up | down | toggle | mode  to a desktop shortcut.",
                           "muted", wrap=True))
        g = form(cl)
        self.hk = {}
        for i, (k, t) in enumerate([("hotkey_up", "Increase limit"), ("hotkey_down", "Decrease limit"),
                                    ("hotkey_toggle", "Toggle limiter"), ("hotkey_mode", "Cycle method")]):
            e = HotkeyEdit(s.get(k, ""))
            e.setMinimumWidth(300)
            e.changed.connect(self.save)
            row(g, i, t, e)
            self.hk[k] = e
        self.hotkeys_on = QCheckBox("Enabled")
        self.hotkeys_on.setChecked(s.get("hotkeys", "1") == "1")
        self.hotkeys_on.toggled.connect(self.save)
        row(g, 4, "Hotkeys", self.hotkeys_on)
        t_hotkeys.addWidget(c)

        c, cl = card("Increase / decrease steps through")
        g = form(cl)
        self.cycle = QComboBox()
        self.cycle.addItem("Quick presets (from refresh rate)", "presets")
        self.cycle.addItem("Custom list", "custom")
        self.cycle.addItem("Fixed step", "step")
        self.cycle.setCurrentIndex(max(0, self.cycle.findData(s.get("hotkey_cycle", "presets"))))
        self.cycle.currentIndexChanged.connect(self._cycle_changed)
        row(g, 0, "Mode", self.cycle)
        self.cycle_list = QLineEdit(s.get("hotkey_custom_values", ""))
        self.cycle_list.setPlaceholderText("e.g. 30, 45, 60, 90, 120")
        self.cycle_list.editingFinished.connect(self.save)
        row(g, 1, "Custom FPS list", self.cycle_list)
        self.step = QDoubleSpinBox()
        self.step.setRange(0.5, 1000)
        self.step.setSuffix(" fps")
        self.step.setValue(float(s.get("step", 5)))
        self.step.valueChanged.connect(self.save)
        row(g, 2, "Step", self.step)
        self.cycle_preview = label("", "muted", wrap=True)
        cl.addWidget(self.cycle_preview)
        t_hotkeys.addWidget(c)

        # display / refresh
        c, cl = card("Display")
        g = form(cl)
        self.hz_combo = QComboBox()
        self.hz_combo.addItem("Auto (screen of this window)", "auto")
        for scr in QGuiApplication.screens():
            self.hz_combo.addItem(f"{scr.name()} · {scr.refreshRate():.3f} Hz", f"{scr.refreshRate():.3f}")
        self.hz_combo.addItem("Custom…", "custom")
        self.hz_custom = QDoubleSpinBox()
        self.hz_custom.setRange(20, 1000)
        self.hz_custom.setDecimals(3)
        self.hz_custom.setSuffix(" Hz")
        cur = s.get("refresh_hz", "auto")
        idx = self.hz_combo.findData(cur)
        if idx < 0 and cur != "auto":
            idx = self.hz_combo.count() - 1
            try:
                self.hz_custom.setValue(float(cur))
            except ValueError:
                pass
        self.hz_combo.setCurrentIndex(max(0, idx))
        self.hz_custom.setVisible(self.hz_combo.currentData() == "custom")
        self.hz_combo.currentIndexChanged.connect(self._hz_changed)
        self.hz_custom.valueChanged.connect(self.save)
        hz_row = QWidget()
        hl = QHBoxLayout(hz_row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.hz_combo)
        hl.addWidget(self.hz_custom)
        row(g, 0, "Refresh rate", hz_row)
        cl.addWidget(label("Quick buttons and hotkey presets are FPS values that divide evenly into this "
                           "refresh rate (plus 2× / 4× for high frame rates).", "muted", wrap=True))
        t_display.addWidget(c)

        # background
        c, cl = card("Background limit")
        cl.addWidget(label("Lower the limit while a game window is not focused (alt-tabbed) to save power and "
                           "heat. Applies to programs with a preset; can be overridden per preset. "
                           "X11 / XWayland / Proton games.", "muted", wrap=True))
        g = form(cl)
        self.bg_on = QCheckBox("Enabled")
        bgv = float(s.get("background_fps", "0") or 0)
        self.bg_on.setChecked(bgv > 0)
        self.bg_val = QDoubleSpinBox()
        self.bg_val.setRange(1, 1000)
        self.bg_val.setDecimals(0)
        self.bg_val.setSuffix(" fps")
        self.bg_val.setValue(bgv if bgv > 0 else 30)
        self.bg_on.toggled.connect(self.save)
        self.bg_val.valueChanged.connect(self.save)
        row(g, 0, "Background limit", self.bg_on)
        row(g, 1, "Limit to", self.bg_val)
        t_bg.addWidget(c)

        # OSD
        c, cl = card("On-screen display (Vulkan)")
        g = form(cl)
        self.osd = QCheckBox("Show when the limit changes")
        self.osd.setChecked(s.get("osd", "1") == "1")
        self.osd_always = QCheckBox("Always show")
        self.osd_always.setChecked(s.get("osd_always", "0") == "1")
        self.osd_secs = QDoubleSpinBox()
        self.osd_secs.setRange(0.5, 30)
        self.osd_secs.setSuffix(" s")
        self.osd_secs.setValue(float(s.get("osd_seconds", 2.5)))
        for w in (self.osd, self.osd_always):
            w.toggled.connect(self.save)
        self.osd_secs.valueChanged.connect(self.save)
        row(g, 0, "OSD", self.osd)
        row(g, 1, "", self.osd_always)
        row(g, 2, "Visible for", self.osd_secs)
        t_display.addWidget(c)

        # defaults
        c, cl = card("Programs without a preset")
        g = form(cl)
        self.def_en = QCheckBox("Limit every game by default")
        self.def_en.setChecked(s.get("default_enabled", "0") == "1")
        self.def_fps = QDoubleSpinBox()
        self.def_fps.setRange(FPS_MIN, FPS_MAX)
        self.def_fps.setSuffix(" fps")
        self.def_fps.setValue(float(s.get("default_fps", 60)))
        self.def_mode = QComboBox()
        self.def_mode.addItems([MODE_INFO[m][0] for m in MODES])
        self.def_mode.setCurrentIndex(MODES.index(s.get("default_mode", "smooth"))
                                      if s.get("default_mode") in MODES else 0)
        self.def_en.toggled.connect(self.save)
        self.def_fps.valueChanged.connect(self.save)
        self.def_mode.currentIndexChanged.connect(self.save)
        self.ignore = QLineEdit(s.get("ignore", ""))
        self.ignore.setPlaceholderText("built-in: desktop shell, browsers, OBS, Discord…")
        self.ignore.editingFinished.connect(self.save)
        row(g, 0, "Default", self.def_en)
        row(g, 1, "Default / new preset limit", self.def_fps)
        row(g, 2, "Default method", self.def_mode)
        row(g, 3, "Never hook (comma-separated keys)", self.ignore)
        t_general.addWidget(c)

        # appearance + app
        c, cl = card("Application")
        g = form(cl)
        self.font_size = QDoubleSpinBox()
        self.font_size.setRange(0, 24)
        self.font_size.setDecimals(1)
        self.font_size.setSingleStep(0.5)
        self.font_size.setSpecialValueText("System size")
        self.font_size.setSuffix(" pt")
        self.font_size.setValue(float(s.get("gui_font_size", "0") or 0))
        self.font_size.valueChanged.connect(self.save)
        frow = self.font_size
        row(g, 0, "Font size", frow, "font: " + QApplication.font().family())
        self.tray = QCheckBox("Close to system tray")
        self.tray.setChecked(s.get("gui_close_to_tray", "0") == "1")
        self.tray.toggled.connect(self.save)
        row(g, 1, "Window", self.tray)
        self.autostart = QCheckBox("Start in tray on login")
        self.autostart.setChecked(AUTOSTART.exists())
        self.autostart.toggled.connect(self._autostart)
        row(g, 2, "", self.autostart)
        cl.addWidget(label("The GUI does not need to run: presets are applied by the layer itself. "
                           f"Config: {config_dir()}", "muted", wrap=True))
        t_app.addWidget(c)

        c, cl = card("Game names")
        cl.addWidget(label("Titles come from your Steam library, Faugus / Lutris / Heroic, and a list of ~25 000 "
                           "game executables built from Discord's public game list, then initials of installed "
                           "titles (ffxvi → Final Fantasy XVI). Your process list never leaves this PC.",
                           "muted", wrap=True))
        self.names_auto = QCheckBox("Download the game list automatically (first run, then monthly)")
        self.names_auto.setChecked(s.get("names_auto", "1") == "1")
        self.names_auto.toggled.connect(self.save)
        cl.addWidget(self.names_auto)
        nr = QHBoxLayout()
        self.names_btn = QPushButton("Update game list")
        self.names_btn.setToolTip("Downloads the public list from discord.com once (~13 MB, nothing is uploaded)")
        self.names_btn.clicked.connect(self._update_names)
        self.names_lbl = label("", "muted")
        nr.addWidget(self.names_btn)
        nr.addWidget(self.names_lbl, 1)
        cl.addLayout(nr)
        t_app.addWidget(c)
        for t in (t_general, t_hotkeys, t_display, t_bg, t_app):
            t.addStretch(1)
        self._names_info()
        self._cycle_changed(None, save=False)

    def _cycle_changed(self, _, save=True):
        m = self.cycle.currentData()
        self.cycle_list.setEnabled(m == "custom")
        self.step.setEnabled(m == "step")
        if save:
            self.save()
            return
        vals = self.win.hotkey_values()
        self.cycle_preview.setText(("Hotkeys step through: " + "  ".join(fmt_fps(v) for v in vals))
                                   if vals else f"Hotkeys change the limit by {self.step.value():g} fps.")

    def _names_info(self):
        from .names import CACHE
        w = getattr(self.win, "names_job", None)
        if w and w.get("running"):
            self.names_lbl.setText("downloading…")
            return
        try:
            import datetime
            d = datetime.date.fromtimestamp(CACHE.stat().st_mtime)
            self.names_lbl.setText("updated " + d.isoformat())
        except OSError:
            err = w.get("error") if w else None
            self.names_lbl.setText(f"failed: {err}" if err else "not downloaded")
        self.names_btn.setEnabled(not (w and w.get("running")))

    def _update_names(self):
        self.win.update_names()

    def _hz_changed(self, _):
        self.hz_custom.setVisible(self.hz_combo.currentData() == "custom")
        self.save()

    def save(self, *_):
        s = dict(self.win.settings_data)
        for k, e in self.hk.items():
            s[k] = e.text()
        s["step"] = f"{self.step.value():g}"
        s["hotkeys"] = "1" if self.hotkeys_on.isChecked() else "0"
        s["osd"] = "1" if self.osd.isChecked() else "0"
        s["osd_always"] = "1" if self.osd_always.isChecked() else "0"
        s["osd_seconds"] = f"{self.osd_secs.value():g}"
        s["default_enabled"] = "1" if self.def_en.isChecked() else "0"
        s["default_fps"] = f"{self.def_fps.value():g}"
        s["default_mode"] = MODES[self.def_mode.currentIndex()]
        s["background_fps"] = f"{self.bg_val.value():g}" if self.bg_on.isChecked() else "0"
        hz = self.hz_combo.currentData()
        s["refresh_hz"] = f"{self.hz_custom.value():.3f}" if hz == "custom" else hz
        ign = self.ignore.text().strip()
        if ign:
            s["ignore"] = ign
        else:
            s.pop("ignore", None)
        s["gui_close_to_tray"] = "1" if self.tray.isChecked() else "0"
        s["names_auto"] = "1" if self.names_auto.isChecked() else "0"
        old_font = self.win.settings_data.get("gui_font_size", "0")
        s.pop("gui_font", None)
        s["gui_font_size"] = f"{self.font_size.value():g}"
        s["hotkey_cycle"] = self.cycle.currentData()
        s["hotkey_custom_values"] = self.cycle_list.text().strip()
        self.win.settings_data = s
        self.win.settings_changed()          # also writes hotkey_values + saves
        if s["gui_font_size"] != old_font:
            apply_appearance(QApplication.instance(), s)
        self._cycle_changed(None, save=False)

    def _autostart(self, on):
        try:
            if on:
                exe = Path.home() / ".local/bin/pacer"
                cmd = str(exe) if exe.exists() else str(ROOT / "bin/pacer")
                AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
                AUTOSTART.write_text("[Desktop Entry]\nType=Application\nName=pacer\n"
                                     f"Exec={cmd} --tray\nIcon=video-display\nTerminal=false\n"
                                     "X-GNOME-Autostart-enabled=true\n")
            else:
                AUTOSTART.unlink(missing_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Pacer", f"Could not update autostart: {e}")

    def refresh_install(self):
        found = next((m for m in LAYER_MANIFESTS if m.exists()), None)
        if found:
            self.install_lbl.setText(f"<span style='color:{ACCENT}'>● Installed</span> — {found}")
        else:
            self.install_lbl.setText("<span style='color:#ffb454'>● Vulkan layer not installed</span> — "
                                     "games are not limited until it is installed.")

    def _run_script(self, name):
        script = ROOT / name
        if not script.exists():
            QMessageBox.warning(self, "Pacer", f"{script} not found")
            return
        p = subprocess.run(["sh", str(script)], capture_output=True, text=True)
        self.refresh_install()
        (QMessageBox.information if p.returncode == 0 else QMessageBox.warning)(
            self, "Pacer", (p.stdout + p.stderr)[-3000:] or "done")


# ------------------------------------------------------------------ main window
FILTERS = [("Games", "games"), ("Graphics apps", "graphics"), ("All processes", "all")]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pacer")
        self.setWindowIcon(app_icon())
        self.resize(1000, 700)
        self.setMinimumSize(900, 560)
        self.settings_data = load_settings()
        self.scanner = ProcessScanner()
        self.registry = LiveRegistry()
        self.live: list[LiveStatus] = []
        self.procs = []
        self._list_sig = None
        self.current: str | None = None

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(250)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, 10, 8, 8)
        sl.setSpacing(2)
        brand = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(app_icon().pixmap(20, 20))
        brand.addWidget(ic)
        brand.addWidget(label("Pacer", "h2"))
        brand.addStretch(1)
        sl.addLayout(brand)

        hdr = QHBoxLayout()
        hdr.addWidget(label("RUNNING", "section"))
        hdr.addStretch(1)
        self.filter = QComboBox()
        for t, v in FILTERS:
            self.filter.addItem(t, v)
        f = self.settings_data.get("list_filter", "games")
        self.filter.setCurrentIndex(max(0, self.filter.findData(f)))
        self.filter.setToolTip("Games: detected games, programs with a preset and pinned programs.\n"
                               "Right-click a program to pin or hide it.")
        self.filter.currentIndexChanged.connect(self._filter_changed)
        hdr.addWidget(self.filter)
        sl.addLayout(hdr)
        self.run_list = QListWidget()
        self.run_list.setItemDelegate(RowDelegate(self.run_list))
        self.run_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.run_list.customContextMenuRequested.connect(self._run_menu)
        sl.addWidget(self.run_list, 3)

        hdr = QHBoxLayout()
        hdr.addWidget(label("PRESETS", "section"))
        hdr.addStretch(1)
        add = QPushButton("+")
        add.setObjectName("icon")
        add.setToolTip("Add a program that is not in the list")
        add.clicked.connect(self.add_program)
        hdr.addWidget(add)
        sl.addLayout(hdr)
        self.preset_list = QListWidget()
        self.preset_list.setItemDelegate(RowDelegate(self.preset_list))
        sl.addWidget(self.preset_list, 2)
        self.nav_settings = QPushButton("Settings")
        self.nav_settings.setObjectName("nav")
        self.nav_settings.setCheckable(True)
        self.nav_settings.clicked.connect(self.open_settings)
        sl.addWidget(self.nav_settings)
        h.addWidget(side)

        self.stack = QStackedWidget()
        self.welcome = QWidget()
        wl = QVBoxLayout(self.welcome)
        wl.addStretch(1)
        w1 = label("Select a game", "title")
        w1.setAlignment(Qt.AlignCenter)
        w2 = label("Pick a program on the left and create a preset. It is applied automatically every time the "
                   "program starts and can be changed live while you play. Missing a game? Use + or switch "
                   "the filter.", "muted", wrap=True)
        w2.setAlignment(Qt.AlignCenter)
        w2.setMaximumWidth(460)
        wl.addWidget(w1)
        wl.addWidget(w2, 0, Qt.AlignHCenter)
        wl.addStretch(1)
        self.page = PresetPage(self)
        self.settings = SettingsPage(self)
        self.stack.addWidget(self.welcome)
        self.stack.addWidget(self.page)
        self.stack.addWidget(self.settings)
        h.addWidget(self.stack, 1)

        self.run_list.itemClicked.connect(lambda it: self.select(it, self.preset_list))
        self.preset_list.itemClicked.connect(lambda it: self.select(it, self.run_list))
        self.page.changed.connect(lambda: self.refresh(force=True, scan=False))

        self.names_job: dict | None = None
        self.slow = QTimer(self, interval=1000, timeout=self.refresh)
        self.fast = QTimer(self, timeout=self.tick)
        self.slow.start()
        self.set_graph_rate(int(self.settings_data.get("graph_rate", "20") or 0), save=False)
        self.settings_changed()
        self.refresh(force=True)
        self._setup_tray()
        if self.settings_data.get("names_auto", "1") == "1" and not os.environ.get("PACER_NO_DOWNLOAD"):
            from .names import NameResolver
            age = NameResolver.index_age_days()
            if age is None or age > 30:
                QTimer.singleShot(2000, self.update_names)

    # ---- game-name list (downloaded in a worker thread, applied on the GUI thread)
    def update_names(self):
        if self.names_job and self.names_job.get("running"):
            return
        from .names import NameResolver
        job = self.names_job = {"running": True, "error": None}

        def work():
            try:
                NameResolver.update_index()
            except Exception as e:  # offline, blocked, …: names fall back to the other sources
                job["error"] = str(e)[:80]
            job["running"] = False

        threading.Thread(target=work, daemon=True, name="pacer-names").start()
        self.settings._names_info()
        self._names_poll = QTimer(self, interval=500)
        self._names_poll.timeout.connect(self._names_done)
        self._names_poll.start()

    def _names_done(self):
        if self.names_job and self.names_job.get("running"):
            return
        self._names_poll.stop()
        if not self.names_job.get("error"):
            resolver.__init__()
            self.scanner.cache.clear()
            self.refresh(force=True)
        self.settings._names_info()

    # ---- settings / timers
    def refresh_hz(self) -> float:
        v = self.settings_data.get("refresh_hz", "auto")
        if v and v != "auto":
            try:
                return float(v)
            except ValueError:
                pass
        scr = self.screen() or QGuiApplication.primaryScreen()
        return scr.refreshRate() if scr and scr.refreshRate() > 1 else 60.0

    def hotkey_values(self) -> list[float]:
        m = self.settings_data.get("hotkey_cycle", "presets")
        if m == "step":
            return []
        if m == "custom":
            out = []
            for t in re.split(r"[,\s]+", self.settings_data.get("hotkey_custom_values", "")):
                try:
                    v = float(t)
                except ValueError:
                    continue
                if FPS_MIN <= v <= FPS_MAX:
                    out.append(v)
            return sorted(set(out))
        return refresh_presets(self.refresh_hz())

    def settings_changed(self):
        vals = ", ".join(f"{v:g}" for v in self.hotkey_values())
        if self.settings_data.get("hotkey_values", "") != vals:
            self.settings_data["hotkey_values"] = vals
        # the real refresh rate for the layer's divisor snapping (refresh_hz may be "auto")
        self.settings_data["display_hz"] = f"{self.refresh_hz():.3f}"
        save_settings(self.settings_data)
        self.page.update_refresh(self.refresh_hz())
        if self.page.preset:
            self.page._bg_widgets()

    def set_graph_rate(self, rate: int, save: bool = True):
        if rate > 0:
            self.fast.start(int(1000 / rate))
        else:
            self.fast.stop()
        if save:
            self.settings_data["graph_rate"] = str(rate)
            save_settings(self.settings_data)

    def _active(self) -> bool:
        return self.isVisible() and not self.isMinimized()

    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange and not self.isMinimized():
            self.refresh(force=True)
        super().changeEvent(e)

    def moveEvent(self, e):
        super().moveEvent(e)
        if self.settings_data.get("refresh_hz", "auto") == "auto":
            hz = self.refresh_hz()
            if abs(hz - getattr(self.page, "hz", 0)) > 0.01:
                self.settings_changed()

    # ---- tray
    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        m = QMenu()
        m.addAction(QAction("Show", self, triggered=self.show_window))
        m.addAction(QAction("Quit", self, triggered=self.quit))
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self.show_window() if r == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("Pacer")
        self.tray.show()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.refresh(force=True)

    def quit(self):
        if self.page.save_timer.isActive():
            self.page.save_timer.stop()
            self.page._save()
        QApplication.quit()

    def closeEvent(self, e):
        if self.tray and self.settings_data.get("gui_close_to_tray") == "1":
            e.ignore()
            self.hide()
        else:
            self.quit()

    # ---- data
    def live_for(self, key) -> LiveStatus | None:
        cands = [s for s in self.live if s.key == key]
        return max(cands, key=lambda s: s.last_present_ns) if cands else None

    def _keyset(self, name: str) -> set[str]:
        return {k.strip() for k in self.settings_data.get(name, "").split(",") if k.strip()}

    def _set_keyset(self, name: str, keys: set[str]):
        self.settings_data[name] = ",".join(sorted(keys))
        save_settings(self.settings_data)

    def _filter_changed(self, _):
        self.settings_data["list_filter"] = self.filter.currentData()
        save_settings(self.settings_data)
        self.refresh(force=True)

    def refresh(self, force=False, scan=True):
        if not self._active() and not force:
            return   # hidden / minimised / in tray: no scanning at all
        try:
            self.live = self.registry.poll()
        except Exception as ex:  # never let a malformed file kill the GUI
            print("live poll:", ex, file=sys.stderr)
            self.live = []
        if scan:
            self.procs = self.scanner.scan("all")
        presets = {p.key: p for p in list_presets()}
        pinned = self._keyset("pinned") | set(presets)
        hidden = self._keyset("hidden")
        mode = self.filter.currentData()
        by_key = {}
        for p in self.procs:
            if p.key not in by_key or p.rss_mb > by_key[p.key].rss_mb:
                by_key[p.key] = p

        def show(key, info, live):
            if mode == "all":
                return True
            if key in hidden:
                return False
            if key in pinned:
                return True
            if mode == "graphics":
                return bool(live) or bool(info and info.gfx and key not in NOT_GAMES)
            return bool(info and info.game and (info.gfx or live))

        run_rows = []
        seen = set()
        for s in sorted(self.live, key=lambda s: -s.last_present_ns):
            if s.key in seen:
                continue
            info = by_key.get(s.key)
            if not show(s.key, info, s):
                continue
            seen.add(s.key)
            pr = presets.get(s.key)
            name = preset_title(pr, info.name if info else "") if pr else \
                (info.name if info else resolver.title(s.exe or s.key))
            if s.background:
                detail, col = "background", "#ffc861"
            elif s.enabled:
                detail, col = f"{fmt_fps(s.fps_target)} fps", ACCENT
            else:
                detail, col = f"{s.api_name} · off", "#5aa9ff"
            run_rows.append((s.key, name, detail, col, s.exe))
        for key, p in sorted(by_key.items(), key=lambda kv: (not kv[1].game, kv[1].name.lower())):
            if key in seen or not show(key, p, None):
                continue
            seen.add(key)
            pr = presets.get(key)
            detail = " · ".join(x for x in (p.gfx or "", "not hooked" if p.gfx else "") if x)
            run_rows.append((key, preset_title(pr, p.name) if pr else p.name, detail, "#4a505b", p.exe))
        live_keys = {s.key: s for s in self.live}
        preset_rows = []
        for k, pr in sorted(presets.items(), key=lambda kv: kv[1].name.lower()):
            lv = live_keys.get(k)
            col = ("#ffc861" if lv.background else ACCENT if lv.enabled else "#5aa9ff") if lv else "#3a3f49"
            detail = f"{fmt_fps(pr.fps)} · {MODE_INFO.get(pr.mode, MODE_INFO['smooth'])[0].lower()}" \
                if pr.enabled else "off"
            preset_rows.append((k, preset_title(pr, by_key[k].name if k in by_key else ""), detail, col, ""))
        sig = (tuple(run_rows), tuple(preset_rows))
        if force or sig != self._list_sig:
            self._list_sig = sig
            self._fill(self.run_list, run_rows, "no games running")
            self._fill(self.preset_list, preset_rows, "no presets yet")
        if self.stack.currentWidget() is self.page:
            self.page.poll_external()
            if not self.fast.isActive():
                self.tick(draw_graph=False)
        self.settings.refresh_install()

    def _fill(self, lw: QListWidget, rows, empty: str):
        lw.blockSignals(True)
        sel = self.current if lw.currentItem() and lw.currentItem().isSelected() else None
        lw.clear()
        for key, name, detail, color, exe in rows:
            it = QListWidgetItem(name)
            it.setData(ROLE_DATA, (key, exe, name))
            it.setData(ROLE_DETAIL, detail)
            it.setData(ROLE_COLOR, color)
            it.setToolTip(f"{name}\n{key}" + (f"\n{exe}" if exe else ""))
            lw.addItem(it)
            if sel and key == sel:
                it.setSelected(True)
                lw.setCurrentItem(it)
        if not lw.count():
            it = QListWidgetItem(empty)
            it.setFlags(Qt.NoItemFlags)
            lw.addItem(it)
        lw.blockSignals(False)

    def _run_menu(self, pos):
        it = self.run_list.itemAt(pos)
        if not it or not it.data(ROLE_DATA):
            return
        key, exe, name = it.data(ROLE_DATA)
        pinned, hidden = self._keyset("pinned"), self._keyset("hidden")
        m = QMenu(self)
        a_pin = m.addAction("Unpin from list" if key in pinned else "Always show in list")
        a_hide = m.addAction("Show again" if key in hidden else "Hide from list")
        a = m.exec(self.run_list.viewport().mapToGlobal(pos))
        if a is a_pin:
            pinned ^= {key}
            self._set_keyset("pinned", pinned)
        elif a is a_hide:
            hidden ^= {key}
            self._set_keyset("hidden", hidden)
        self.refresh(force=True, scan=False)

    def select(self, it: QListWidgetItem, other: QListWidget):
        data = it.data(ROLE_DATA)
        if not data:
            return
        other.clearSelection()
        key, exe, name = data
        self.current = key
        self.nav_settings.setChecked(False)
        self.page.show_key(key, name, exe)
        self.stack.setCurrentWidget(self.page)
        self.tick()

    def open_settings(self):
        self.nav_settings.setChecked(True)
        self.run_list.clearSelection()
        self.preset_list.clearSelection()
        self.current = None
        self.stack.setCurrentWidget(self.settings)

    def add_program(self):
        dlg = PickProcessDialog(self, self.scanner.scan("all"))
        if dlg.exec() != QDialog.Accepted or not dlg.result_key:
            return
        key, name = dlg.result_key, dlg.result_name
        if not Preset.load(key):
            s = self.settings_data
            Preset(key=key, name=name, fps=float(s.get("default_fps", 60)),
                   mode=s.get("default_mode", "smooth")).save()
        self._set_keyset("hidden", self._keyset("hidden") - {key})
        self.refresh(force=True, scan=False)
        self.current = key
        self.page.show_key(key, name)
        self.stack.setCurrentWidget(self.page)

    def tick(self, draw_graph: bool = True):
        if self.stack.currentWidget() is not self.page or not self.current or not self._active():
            return
        st = self.live_for(self.current)
        if st:
            sf = self.registry.files.get(st.path)
            st = (sf.read() if sf else None) or st
        self.page.update_live(st, draw_graph)


def main():
    if "--version" in sys.argv:
        from . import __version__
        print(f"pacer {__version__}")
        return 0
    if "--ctl" in sys.argv:
        from .ctl import main as ctl
        sys.argv.remove("--ctl")
        return ctl()
    QGuiApplication.setDesktopFileName("pacer")
    app = QApplication(sys.argv)
    app.setApplicationName("Pacer")
    from . import __version__
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    apply_appearance(app, load_settings())
    w = MainWindow()
    if "--screenshot" in sys.argv:
        out = sys.argv[sys.argv.index("--screenshot") + 1]
        sel = os.environ.get("PACER_SELECT")

        def shoot():
            if sel:
                for lw in (w.run_list, w.preset_list):
                    for i in range(lw.count()):
                        d = lw.item(i).data(ROLE_DATA)
                        if d and d[0] == sel:
                            w.select(lw.item(i), w.preset_list if lw is w.run_list else w.run_list)
                            break
                    else:
                        continue
                    break
            if os.environ.get("PACER_SETTINGS"):
                w.open_settings()
            QTimer.singleShot(1200, lambda: (w.grab().save(out), app.quit()))

        QTimer.singleShot(300, shoot)
    if "--tray" in sys.argv and w.tray:
        pass   # start hidden in the tray
    else:
        w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
