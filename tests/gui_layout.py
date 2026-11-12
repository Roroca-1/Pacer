"""Headless GUI check: builds the main window offscreen, walks every page / settings tab and reports
clipped labels or buttons, horizontal overflow and hotkey recording. Exits non-zero on failure.

    python3 tests/gui_layout.py        (uses a throwaway config under build/guitest)
"""
import os, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "build" / "guitest"
shutil.rmtree(TMP / "cfg", ignore_errors=True)
for k, sub in (("PACER_CONFIG_DIR", "cfg"), ("PACER_STATE_DIR", "state"), ("XDG_CACHE_HOME", "cache"),
               ("XDG_CONFIG_HOME", "xdg")):
    os.environ.setdefault(k, str(TMP / sub))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PACER_NO_DOWNLOAD"] = "1"
sys.path.insert(0, str(ROOT / "gui"))
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget, QScrollArea
from PySide6.QtCore import QTimer, Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
import pacer.app as A
from pacer.config import Preset, load_settings

app = QApplication(sys.argv); app.setStyle("Fusion")
A.apply_appearance(app, load_settings())
Preset(key="cs2", name="Counter-Strike 2", fps=237, mode="latency").save()
w = A.MainWindow(); w.show()
fails = 0

def check(tag):
    global fails
    app.processEvents()
    bad = 0
    page = w.stack.currentWidget()
    for wd in page.findChildren(QWidget):
        if not wd.isVisible(): continue
        if isinstance(wd, (QLabel, QPushButton)):
            ww = isinstance(wd, QLabel) and wd.wordWrap()
            sh = wd.minimumSizeHint() if ww else wd.sizeHint()
            g = wd.size()
            if (sh.width() > g.width() + 2 and not ww) or sh.height() > g.height() + 2:
                txt = (wd.text() if hasattr(wd, 'text') else '')[:40]
                print(f"[{tag}] CLIPPED {type(wd).__name__} '{txt}' hint={sh.width()}x{sh.height()} got={g.width()}x{g.height()}"); bad += 1
    sa = page.findChild(QScrollArea) if not hasattr(page, "tabs") else page.tabs.currentWidget()
    if sa: print(f"[{tag}] content {sa.widget().sizeHint().width()}x{sa.widget().sizeHint().height()} viewport {sa.viewport().width()}x{sa.viewport().height()} hscroll={sa.horizontalScrollBar().maximum()}")
    print(f"[{tag}] clipped={bad}")
    fails += bad

def run():
    global fails
    for i in range(w.preset_list.count()):
        d = w.preset_list.item(i).data(A.ROLE_DATA)
        if d and d[0] == "cs2": w.select(w.preset_list.item(i), w.run_list)
    print("quick:", [b.text() for b in w.page.quick_btns], "vrr", w.page.vrr_btn.property("fps"), w.page.hz_lbl.text())
    w.page.update_refresh(240.0)
    print("quick@240:", [b.text() for b in w.page.quick_btns])
    w.settings_data["refresh_hz"] = "540.000"; w.settings_changed()
    print("quick@540:", [b.text() for b in w.page.quick_btns], "hotkey_values:", load_settings().get("hotkey_values"))
    print("present tip:", w.page.present.itemData(3, Qt.ToolTipRole)[:60])
    print("title:", w.page.title.text())
    check("preset")
    # fps > 1000 through GUI
    w.page.fps.setValue(4000); w.page.save_timer.stop(); w.page._save()
    print("saved fps:", Preset.load("cs2").fps)
    # bg combo
    w.page.bg_combo.setCurrentIndex(2); w.page.bg_fps.setValue(40); w.page._save()
    print("bg extra:", Preset.load("cs2").extra)
    # graph rate
    w.page.graph_rate.setCurrentIndex(0); print("graph off: fast active", w.fast.isActive(), "graph visible", w.page.graph.isVisible())
    w.page.graph_rate.setCurrentIndex(3); print("graph 20Hz: interval", w.fast.interval())
    w.open_settings()
    for i in range(w.settings.tabs.count()):
        w.settings.tabs.setCurrentIndex(i); app.processEvents()
        sa = w.settings.tabs.currentWidget()
        print("tab", w.settings.tabs.tabText(i), "content", sa.widget().sizeHint().height(), "viewport", sa.viewport().height())
        check("settings/" + w.settings.tabs.tabText(i))
    w.settings.tabs.setCurrentIndex(1)
    w.settings.cycle.setCurrentIndex(1); w.settings.cycle_list.setText("45, 30, 90"); w.settings.save()
    print("custom values:", load_settings().get("hotkey_values"), "|", w.settings.cycle_preview.text())
    w.settings.cycle.setCurrentIndex(2); print("step mode values:", repr(load_settings().get("hotkey_values")))
    w.settings.cycle.setCurrentIndex(0)
    # hotkey record
    hk = w.settings.hk["hotkey_up"]
    hk.btn.click(); app.processEvents()
    for k, m in [(Qt.Key_Control, Qt.ControlModifier), (Qt.Key_F7, Qt.ControlModifier | Qt.AltModifier)]:
        tgt = QApplication.focusWidget() or w
        app.sendEvent(tgt, QKeyEvent(QEvent.ShortcutOverride, k, m))
        app.sendEvent(tgt, QKeyEvent(QEvent.KeyPress, k, m))
    app.processEvents()
    print("recorded:", hk.text(), "recording:", hk.btn.isChecked(), "saved:", load_settings().get("hotkey_up"))
    hk.edit.setText("shift+f9"); hk._typed(); print("typed saved:", load_settings().get("hotkey_up"))
    w.resize(860, 560); w.page.show_key("cs2"); w.stack.setCurrentWidget(w.page); check("small")
    w.filter.setCurrentIndex(2); app.processEvents(); print("all rows", w.run_list.count())
    w.filter.setCurrentIndex(0); app.processEvents(); print("games rows", w.run_list.count(), [w.run_list.item(i).text() for i in range(w.run_list.count())])
    w.grab().save(os.environ.get("SHOT", str(TMP / "shot.png")))
    print("FAILS", fails)
    app.quit()

QTimer.singleShot(500, run); app.exec()
sys.exit(1 if fails else 0)
