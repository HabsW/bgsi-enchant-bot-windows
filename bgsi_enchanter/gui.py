"""Small Qt control panel. Put it on your other monitor, not over the game."""
import sys
import threading

from PySide6 import QtCore, QtGui, QtWidgets

from . import diag
from .bot import Bot
from .config import DEBUG_DIR, SECRET_ENCHANTS, load_config, save_config

NONE = "(nothing)"
PRESETS = [
    ("Ultra Roller + Secret Hunter", ["Ultra Roller", "Secret Hunter"]),
    ("Ultra Roller + Shiny Seeker", ["Ultra Roller", "Shiny Seeker"]),
]


class Bridge(QtCore.QObject):
    log = QtCore.Signal(str)
    status = QtCore.Signal(dict)
    done = QtCore.Signal()


class Main(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BGSI Enchant Bot")
        self.resize(600, 560)
        self.cfg = load_config()
        self.bot = None
        self.worker = None
        self.bridge = Bridge()
        self.bridge.log.connect(self.append_log)
        self.bridge.status.connect(self.show_status)
        self.bridge.done.connect(self.finished)

        lay = QtWidgets.QVBoxLayout(self)

        box = QtWidgets.QGroupBox("Enchant combination every equipped pet should get")
        form = QtWidgets.QFormLayout(box)
        t = self.cfg["targets"] + ["", ""]
        self.t1 = self._combo(t[0] or "Ultra Roller", allow_none=False)
        self.t2 = self._combo(t[1] or NONE, allow_none=True)
        form.addRow("Enchant #1 (primary)", self.t1)
        form.addRow("Enchant #2 (secondary)", self.t2)
        presets = QtWidgets.QHBoxLayout()
        for label, combo in PRESETS:
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, c=combo: self._apply_preset(c))
            presets.addWidget(b)
        form.addRow("Presets", self._wrap(presets))
        self.order = QtWidgets.QCheckBox("#1 must be in the top slot and #2 in the bottom slot")
        self.order.setChecked(self.cfg["order_matters"])
        form.addRow(self.order)
        self.unlock = QtWidgets.QCheckBox("Unlock locked enchants that are not in the combination")
        self.unlock.setChecked(self.cfg["unlock_unwanted"])
        form.addRow(self.unlock)
        self.renew = QtWidgets.QCheckBox("Re-enchant every pet once, even if it already has the combination")
        self.renew.setToolTip("Old enchants can be worn out (e.g. 100k of 1.5M left) while the enchant book\n"
                              "still shows them at full. With this on, every pet gets all its enchants\n"
                              "rerolled once and then the combination rolled again (costs crystals).")
        self.renew.setChecked(self.cfg["reenchant_once"])
        form.addRow(self.renew)
        lay.addWidget(box)

        box2 = QtWidgets.QGroupBox("Limits")
        f2 = QtWidgets.QFormLayout(box2)
        self.total = self._spin(self.cfg["max_crystals_total"])
        f2.addRow("Max Shadow Crystals in total (0 = no limit)", self.total)
        self.only = QtWidgets.QLineEdit(",".join(str(i) for i in self.cfg["only_pets"]))
        self.only.setPlaceholderText("all eligible pets   (or e.g. 1,2,5 = positions in the grid)")
        f2.addRow("Only these pets", self.only)
        lay.addWidget(box2)

        row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("▶  Start")
        self.start_btn.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.start_btn.clicked.connect(lambda: self.start(dry=False))
        self.stop_btn = QtWidgets.QPushButton("■  Stop  (F8)")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.test_btn = QtWidgets.QPushButton("Test detection (no clicks)")
        self.test_btn.clicked.connect(lambda: self.start(dry=True))
        self.mouse_btn = QtWidgets.QPushButton("Mouse test (no clicks)")
        self.mouse_btn.clicked.connect(self.mouse_test)
        dbg = QtWidgets.QPushButton("Screenshots…")
        dbg.clicked.connect(self.open_debug)
        for w in (self.start_btn, self.stop_btn, self.test_btn, self.mouse_btn, dbg):
            row.addWidget(w)
        lay.addLayout(row)

        self.status_lbl = QtWidgets.QLabel("Idle. Open the enchant book in game, then press Start.")
        lay.addWidget(self.status_lbl)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        lay.addWidget(self.log_view, 1)

    # ------------------------------------------------------------ widgets
    def _combo(self, value, allow_none):
        c = QtWidgets.QComboBox()
        c.setEditable(True)        # any enchant name can be typed in
        items = list(SECRET_ENCHANTS) + ([NONE] if allow_none else [])
        c.addItems(items)
        c.setCurrentText(value)
        return c

    def _spin(self, value):
        s = QtWidgets.QSpinBox()
        s.setRange(0, 100000)
        s.setValue(int(value))
        return s

    @staticmethod
    def _wrap(layout):
        w = QtWidgets.QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        return w

    def _apply_preset(self, combo):
        self.t1.setCurrentText(combo[0])
        self.t2.setCurrentText(combo[1] if len(combo) > 1 else NONE)

    def read_form(self):
        cfg = dict(self.cfg)
        targets = [self.t1.currentText().strip(), self.t2.currentText().strip()]
        cfg["targets"] = [t for t in targets if t and t != NONE]
        cfg["order_matters"] = self.order.isChecked()
        cfg["unlock_unwanted"] = self.unlock.isChecked()
        cfg["reenchant_once"] = self.renew.isChecked()
        cfg["max_crystals_total"] = self.total.value()
        only = []
        for part in self.only.text().replace(" ", "").split(","):
            if part.isdigit() and int(part) > 0:
                only.append(int(part))
        cfg["only_pets"] = only
        return cfg

    # ------------------------------------------------------------ actions
    def start(self, dry):
        if self.worker and self.worker.is_alive():
            return
        cfg = self.read_form()
        if not cfg["targets"]:
            self.append_log("Pick at least one enchant.")
            return
        a, b = self.t1.currentText().strip(), self.t2.currentText().strip()
        if a and a.lower() == b.lower():
            self.append_log(f"Enchant #1 and #2 are both '{a}' - a pet can only have one of each "
                            "enchant. Pick a different #2 (or '(nothing)').")
            return
        self.cfg = cfg
        save_config(cfg)
        self.bot = Bot(cfg, log=self.bridge.log.emit, status=self.bridge.status.emit, dry_run=dry)
        self._set_running(True)
        self.append_log(("--- Test detection (no clicks) ---" if dry else "--- Start ---"))
        self.worker = threading.Thread(target=self._run_bot, daemon=True)
        self.worker.start()

    def _run_bot(self):
        try:
            self.bot.run()
        finally:
            self.bridge.done.emit()

    def mouse_test(self):
        if self.worker and self.worker.is_alive():
            return
        self._set_running(True)
        self.append_log("--- Mouse test: the pointer moves over the game window, nothing is clicked ---")

        def job():
            try:
                diag.mouse_test(self.cfg, log=self.bridge.log.emit)
            except Exception as exc:
                self.bridge.log.emit(f"Mouse test error: {exc}")
            finally:
                self.bridge.done.emit()
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def stop(self):
        if self.bot:
            self.bot.stop()
            self.append_log("Stopping…")

    def finished(self):
        self._set_running(False)

    def _set_running(self, running):
        self.start_btn.setEnabled(not running)
        self.test_btn.setEnabled(not running)
        self.mouse_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def open_debug(self):
        DEBUG_DIR.mkdir(exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(DEBUG_DIR)))

    def append_log(self, text):
        self.log_view.appendPlainText(text)
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def show_status(self, st):
        self.status_lbl.setText(f"{st.get('state', '?').capitalize()}  |  Shadow Crystals used: "
                                f"{st.get('crystals', 0)}  |  pets finished: {st.get('pets_done', 0)}")

    def closeEvent(self, ev):
        if self.bot:
            self.bot.stop()
        super().closeEvent(ev)


def main():
    if sys.platform == "win32":
        try:
            from . import win  # noqa: F401  per-monitor DPI awareness before Qt starts
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("BGSI Enchant Bot")
    w = Main()
    w.move(app.primaryScreen().availableGeometry().center() - w.rect().center())
    w.show()
    return app.exec()
