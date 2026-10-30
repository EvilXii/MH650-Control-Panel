#!/usr/bin/env python3
"""
MH650 control panel for Linux - modelled on Cooler Master MasterPlus 1.9.6 (MH650 pages).

Runs as the normal user. The only privileged step is a ONE-TIME udev rule so the logged-in user
may open the headset's vendor HID node; the app offers to install it via pkexec (--install-udev
does the same from a terminal). Audio processing runs in a user-owned PipeWire filter-chain
child process: no /etc files, no PipeWire restart, no root.

Pages and labels follow MasterPlus: LIGHTING, SOUND (LEVELS / BASS / TREBLE / EQUALIZER /
SOUND PROFILES), SURROUND (7.1 SURROUND SOUND PROFILES / 7.1 VIRTUAL SPEAKER PLACEMENT /
SURROUND SOUND DISTANCE / SURROUND SOUND ROTATION).
"""
import math
import os
import sys

from PyQt6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QSlider, QTabWidget, QVBoxLayout, QWidget,
)

import mh650_core as core


# ----------------------------------------------------------------------------- helpers
def vslider(lo, hi, v=0, h=170):
    s = QSlider(Qt.Orientation.Vertical)
    s.setRange(lo, hi)
    s.setValue(v)
    s.setMinimumHeight(h)
    s.setTickPosition(QSlider.TickPosition.TicksRight)
    s.setTickInterval(5)
    return s


def column(title, slider, value_label=None):
    box = QVBoxLayout()
    box.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    if value_label is not None:
        box.addWidget(value_label, alignment=Qt.AlignmentFlag.AlignHCenter)
    box.addWidget(slider, alignment=Qt.AlignmentFlag.AlignHCenter, stretch=1)
    lab = QLabel(title)
    lab.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    box.addWidget(lab)
    return box


class EventThread(QThread):
    """Reads headset notifications (FF 10 06 VV) from the vendor channel."""
    notified = pyqtSignal(int, int)
    failed = pyqtSignal(str)

    def __init__(self, link):
        super().__init__()
        self.link = link
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                ev = self.link.read_event(0.2)
            except OSError as e:
                self.failed.emit(str(e))
                return
            if ev:
                self.notified.emit(*ev)

    def stop(self):
        self._stop = True
        self.wait(1000)


class StageWidget(QWidget):
    """7.1 VIRTUAL SPEAKER PLACEMENT: drag a speaker to change its angle and distance."""
    changed = pyqtSignal()
    selected = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 300)
        self.angles = list(core.VIRTUAL_DEFAULT["angle"])
        self.dists = [30] * 8
        self.sel = 1
        self.setMouseTracking(False)

    def _geom(self):
        r = min(self.width(), self.height()) / 2 - 26
        return QPointF(self.width() / 2, self.height() / 2), max(r, 40)

    def _pos(self, i):
        c, R = self._geom()
        r = R * (0.2 + 0.8 * min(max(self.dists[i], 0), 100) / 100.0)
        a = math.radians(self.angles[i])
        return QPointF(c.x() + r * math.sin(a), c.y() - r * math.cos(a))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, R = self._geom()
        pal = self.palette()
        p.setPen(QPen(pal.mid().color(), 1, Qt.PenStyle.DashLine))
        for k in (0.4, 0.7, 1.0):
            p.drawEllipse(c, R * k, R * k)
        p.setPen(QPen(pal.text().color(), 2))
        p.setBrush(pal.base())
        p.drawEllipse(c, 16, 16)
        p.drawLine(QPointF(c.x(), c.y() - 16), QPointF(c.x(), c.y() - 24))      # nose = front
        f = QFont(self.font())
        f.setPointSize(8)
        p.setFont(f)
        for i, name in enumerate(core.SPEAKERS):
            q = self._pos(i)
            col = pal.highlight().color() if i == self.sel else QColor(90, 140, 220)
            p.setPen(QPen(col.darker(130), 2))
            p.setBrush(col)
            p.drawEllipse(q, 13, 13)
            p.setPen(QColor("white"))
            p.drawText(QRectF(q.x() - 14, q.y() - 9, 28, 18), Qt.AlignmentFlag.AlignCenter, name)

    def _nearest(self, pt):
        d = [(math.hypot(pt.x() - self._pos(i).x(), pt.y() - self._pos(i).y()), i) for i in range(8)]
        dist, i = min(d)
        return i if dist < 24 else None

    def mousePressEvent(self, e):
        i = self._nearest(e.position())
        if i is not None:
            self.sel = i
            self.selected.emit(i)
            self.update()

    def mouseMoveEvent(self, e):
        c, R = self._geom()
        dx, dy = e.position().x() - c.x(), e.position().y() - c.y()
        ang = int(round(math.degrees(math.atan2(dx, -dy))))
        self.angles[self.sel] = core.wrap_angle(ang)
        r = math.hypot(dx, dy) / R
        self.dists[self.sel] = int(max(0, min(100, (r - 0.2) / 0.8 * 100)))
        self.update()
        self.changed.emit()

    def set_data(self, angles, dists):
        self.angles, self.dists = list(angles), list(dists)
        self.update()


# ----------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MH650")
        self.resize(900, 720)
        self.hid = core.HidLink()
        self.store = core.ProfileStore()
        self.eq_proc = core.FilterProcess("eq", "effect_input.mh650_eq")
        self.sur_proc = core.FilterProcess("surround", "effect_input.mh650_surround")
        self.events = None
        self.t0 = os.times()[4]
        self.fx_rgb = (255, 0, 0)
        self.last_sent = None
        self.rot_dir = 0
        self.sofa = core.sofa_search()

        # working copies (SAVE persists them, CANCEL discards them)
        self.eq_cur = {}
        self.virt_cur = {}

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background:#5a3b00;color:#ffd9a0;padding:8px;border-radius:4px;")
        self.banner_btn = QPushButton("Fix permissions (one-time, asks for your password)")
        self.banner_btn.clicked.connect(self.install_udev)
        bl = QHBoxLayout()
        bl.addWidget(self.banner, 1)
        bl.addWidget(self.banner_btn)
        self.banner_wrap = QWidget()
        self.banner_wrap.setLayout(bl)
        self.banner_wrap.hide()
        root.addWidget(self.banner_wrap)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self.tabs.addTab(self.page_lighting(), "LIGHTING")
        self.tabs.addTab(self.page_sound(), "SOUND")
        self.tabs.addTab(self.page_surround(), "SURROUND")
        self.tabs.addTab(self.page_log(), "LOG")
        self.status = self.statusBar()

        self.eq_debounce = QTimer(self, singleShot=True, interval=120, timeout=self.apply_eq)
        self.sur_debounce = QTimer(self, singleShot=True, interval=120, timeout=self.apply_surround)
        self.fx_timer = QTimer(self, interval=50, timeout=self.fx_tick)
        self.rot_timer = QTimer(self, interval=100, timeout=self.rot_tick)
        self.audio_timer = QTimer(self, interval=3000, timeout=self.refresh_audio)
        self.audio_timer.start()

        self.log("Running as uid %d (root is not required)." % os.getuid())
        if os.getuid() == 0:
            self.log("WARNING: you are running as root; this is unnecessary and not recommended.")
        self.load_profile_into_ui()
        self.connect_device()
        self.refresh_audio()

    # ------------------------------------------------------------------ logging / device
    def log(self, text):
        self.log_view.appendPlainText(text)

    def connect_device(self):
        self.banner_wrap.hide()
        try:
            where = self.hid.connect()
        except core.PermissionProblem as e:
            self.banner.setText(f"The headset control channel exists but your user may not open it ({e}). "
                                "Install the udev rule once; after that this app never needs root.")
            self.banner_wrap.show()
            self.status.showMessage("No access to headset control channel")
            self.log(f"Permission denied: {e}")
            return False
        except OSError as e:
            self.status.showMessage(str(e))
            self.log(str(e))
            return False
        self.status.showMessage(f"Connected: {where}")
        self.log(f"Connected: {where}")
        self.events = EventThread(self.hid)
        self.events.notified.connect(lambda t, v: self.log(f"notification type={t} value=0x{v:02x}"))
        self.events.failed.connect(lambda m: self.log(f"event reader stopped: {m}"))
        self.events.start()
        return True

    def install_udev(self):
        try:
            core.install_udev_rule_with_pkexec()
        except OSError as e:
            QMessageBox.warning(self, "udev rule", f"Could not install the rule:\n{e}")
            return
        self.log("udev rule installed; reconnecting")
        QTimer.singleShot(800, self.connect_device)

    # ------------------------------------------------------------------ LIGHTING
    def page_lighting(self):
        w = QWidget()
        g = QGridLayout(w)
        self.fx_combo = QComboBox()
        for eid, name in core.EFFECTS:
            self.fx_combo.addItem(name, eid)
        self.fx_combo.currentIndexChanged.connect(self.fx_changed)
        self.color_btn = QPushButton("Pick colour")
        self.color_btn.clicked.connect(self.pick_color)
        self.swatch = QLabel()
        self.swatch.setFixedSize(48, 24)
        self.paint_swatch()
        self.dim = QSlider(Qt.Orientation.Horizontal)
        self.dim.setRange(0, 100)
        self.dim.setValue(100)
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(1, 5)
        self.speed.setValue(3)
        self.direction = QComboBox()
        self.direction.addItems(["Clockwise", "Counter-clockwise"])
        self.fx_note = QLabel()
        self.fx_note.setWordWrap(True)
        g.addWidget(QLabel("Effect"), 0, 0)
        g.addWidget(self.fx_combo, 0, 1, 1, 2)
        g.addWidget(QLabel("Colour"), 1, 0)
        g.addWidget(self.color_btn, 1, 1)
        g.addWidget(self.swatch, 1, 2)
        g.addWidget(QLabel("Dim"), 2, 0)
        g.addWidget(self.dim, 2, 1, 1, 2)
        g.addWidget(QLabel("Speed"), 3, 0)
        g.addWidget(self.speed, 3, 1, 1, 2)
        g.addWidget(QLabel("Direction"), 4, 0)
        g.addWidget(self.direction, 4, 1, 1, 2)
        g.addWidget(self.fx_note, 5, 0, 1, 3)
        apply_b = QPushButton("Apply")
        apply_b.clicked.connect(self.apply_light)
        off_b = QPushButton("Turn Off")
        off_b.clicked.connect(self.light_off)
        rel_b = QPushButton("Release (back to headset control)")
        rel_b.clicked.connect(self.light_release)
        row = QHBoxLayout()
        for b in (apply_b, off_b, rel_b):
            row.addWidget(b)
        g.addLayout(row, 6, 0, 1, 3)
        g.setRowStretch(7, 1)
        self.fx_changed()
        return w

    def paint_swatch(self):
        self.swatch.setStyleSheet("background:#%02x%02x%02x;border:1px solid #888;" % self.fx_rgb)

    def pick_color(self):
        c = QColorDialog.getColor(QColor(*self.fx_rgb), self)
        if c.isValid():
            self.fx_rgb = (c.red(), c.green(), c.blue())
            self.paint_swatch()

    def fx_changed(self):
        eid = self.fx_combo.currentData()
        if eid in core.UNSUPPORTED_EFFECTS:
            self.fx_note.setText("Customization needs per-LED packets. The MH650 firmware path only exposes a single "
                                 "colour command (setSyncLightingData is an empty stub), so this cannot be applied.")
        elif eid in core.APPROXIMATED_EFFECTS:
            self.fx_note.setText("This pattern is animated by the host on Windows. Only one colour can be sent, so it "
                                 "is shown as a single-colour approximation.")
        else:
            self.fx_note.setText("Rendered as a host-driven single-colour stream, as MasterPlus does.")

    def apply_light(self):
        eid = self.fx_combo.currentData()
        if eid in core.UNSUPPORTED_EFFECTS:
            QMessageBox.information(self, "Customization", self.fx_note.text())
            return
        if self.hid.fd is None and not self.connect_device():
            return
        self.t0 = os.times()[4]
        self.last_sent = None
        if eid in (0, 6):
            self.fx_timer.stop()
            self.send_color(*core.effect_color(eid, 0, self.fx_rgb, self.dim.value()))
        else:
            self.fx_timer.start()

    def fx_tick(self):
        eid = self.fx_combo.currentData()
        t = os.times()[4] - self.t0
        self.send_color(*core.effect_color(eid, t, self.fx_rgb, self.dim.value(), self.speed.value(),
                                           self.direction.currentIndex()))

    def send_color(self, r, g, b):
        if (r, g, b) == self.last_sent:
            return
        try:
            if (r, g, b) == (0, 0, 0):
                self.hid.off()
            else:
                self.hid.color(r, g, b)
            self.last_sent = (r, g, b)
        except OSError as e:
            self.fx_timer.stop()
            self.log(f"lighting write failed: {e}")
            self.status.showMessage(f"Lighting write failed: {e}")

    def light_off(self):
        self.fx_timer.stop()
        self.send_color(0, 0, 0)

    def light_release(self):
        self.fx_timer.stop()
        try:
            self.hid.release()
            self.last_sent = None
        except OSError as e:
            self.log(f"release failed: {e}")

    # ------------------------------------------------------------------ SOUND
    def page_sound(self):
        w = QWidget()
        top = QHBoxLayout(w)

        left = QVBoxLayout()
        lv = QGroupBox("LEVELS")
        lg = QHBoxLayout(lv)
        self.vol = vslider(0, 100, 50)
        self.vol.setTickInterval(25)
        self.vol.valueChanged.connect(lambda v: self.set_level("sinks", v))
        self.vol_mute = QCheckBox("MUTE")
        self.vol_mute.toggled.connect(lambda m: self.set_mute_state("sinks", m))
        self.mic = vslider(0, 100, 50)
        self.mic.setTickInterval(25)
        self.mic.valueChanged.connect(lambda v: self.set_level("sources", v))
        self.mic_mute = QCheckBox("MUTE")
        self.mic_mute.toggled.connect(lambda m: self.set_mute_state("sources", m))
        a = column("VOL", self.vol)
        a.addWidget(self.vol_mute)
        b = column("MIC", self.mic)
        b.addWidget(self.mic_mute)
        lg.addLayout(a)
        lg.addLayout(b)
        left.addWidget(lv)
        self.ctrl_note = QLabel("")
        left.addWidget(self.ctrl_note)

        self.eq_enable = QCheckBox("EQ ENABLE")
        self.eq_enable.setChecked(True)
        self.eq_enable.toggled.connect(lambda _: self.eq_debounce.start())
        self.eq_default = QCheckBox("Apply to system output")
        self.eq_default.setChecked(True)
        left.addWidget(self.eq_enable)
        left.addWidget(self.eq_default)
        left.addStretch(1)
        top.addLayout(left)

        mid = QVBoxLayout()
        tone = QGroupBox("BASS / TREBLE")
        tg = QHBoxLayout(tone)
        self.bass = vslider(core.UI_MIN, core.UI_MAX)
        self.treble = vslider(core.UI_MIN, core.UI_MAX)
        self.bass_lbl, self.treble_lbl = QLabel("0"), QLabel("0")
        for s, l in ((self.bass, self.bass_lbl), (self.treble, self.treble_lbl)):
            s.valueChanged.connect(lambda v, l=l: l.setText(str(v)))
            s.valueChanged.connect(self.eq_edited)
        tg.addLayout(column("BASS", self.bass, self.bass_lbl))
        tg.addLayout(column("TREBLE", self.treble, self.treble_lbl))
        mid.addWidget(tone)

        eqb = QGroupBox("EQUALIZER")
        eg = QHBoxLayout(eqb)
        scale = QVBoxLayout()
        for t in ("10dB", "0dB", "-10dB"):
            scale.addWidget(QLabel(t), alignment=Qt.AlignmentFlag.AlignHCenter)
            scale.addStretch(1)
        eg.addLayout(scale)
        self.band = []
        self.band_lbl = []
        for lab in core.BAND_LABELS:
            s = vslider(core.UI_MIN, core.UI_MAX)
            vl = QLabel("0")
            s.valueChanged.connect(lambda v, l=vl: l.setText(str(v)))
            s.valueChanged.connect(self.eq_edited)
            self.band.append(s)
            self.band_lbl.append(vl)
            eg.addLayout(column(lab, s, vl))
        mid.addWidget(eqb, 1)
        top.addLayout(mid, 2)

        right = QVBoxLayout()
        right.addWidget(QLabel("SOUND PROFILES"))
        self.eq_list = QListWidget()
        self.eq_list.currentRowChanged.connect(self.eq_mode_selected)
        self.eq_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.eq_list.customContextMenuRequested.connect(lambda p: self.list_menu(self.eq_list, p, "eq"))
        right.addWidget(self.eq_list, 1)
        add = QPushButton("ADD NEW PROFILE")
        add.clicked.connect(self.add_eq_mode)
        right.addWidget(add)
        row = QHBoxLayout()
        for text, fn in (("RESET", self.eq_reset), ("SAVE", self.eq_save), ("CANCEL", self.eq_cancel)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        right.addLayout(row)
        self.eq_info = QLabel("")
        self.eq_info.setWordWrap(True)
        self.eq_info.setStyleSheet("color:gray;font-size:10px;")
        right.addWidget(self.eq_info)
        top.addLayout(right, 1)
        return w

    def list_menu(self, lst, pos, kind):
        item = lst.itemAt(pos)
        if not item:
            return
        m = QMenu(self)
        ren, dele = m.addAction("Rename"), m.addAction("Delete")
        act = m.exec(lst.mapToGlobal(pos))
        mid = item.data(Qt.ItemDataRole.UserRole)
        if act is ren:
            name, ok = QInputDialog.getText(self, "Rename", "Name:", text=item.text())
            if ok and name.strip():
                (self.store.eq_mode(mid) if kind == "eq" else self.store.virtual(mid))["name"] = name.strip()
                self.store.save()
                self.fill_lists()
        elif act is dele:
            (self.store.del_eq_mode if kind == "eq" else self.store.del_virtual)(mid)
            self.store.save()
            self.load_profile_into_ui()

    def fill_lists(self):
        self.eq_list.blockSignals(True)
        self.eq_list.clear()
        for m in self.store.eq_modes:
            it = QListWidgetItem(m["name"])
            it.setData(Qt.ItemDataRole.UserRole, m["id"])
            self.eq_list.addItem(it)
            if m["id"] == self.store.eq_cur:
                self.eq_list.setCurrentItem(it)
        self.eq_list.blockSignals(False)
        self.sur_list.blockSignals(True)
        self.sur_list.clear()
        for v in self.store.virtuals:
            it = QListWidgetItem(v["name"])
            it.setData(Qt.ItemDataRole.UserRole, v["id"])
            self.sur_list.addItem(it)
            if v["id"] == self.store.virt_cur:
                self.sur_list.setCurrentItem(it)
        self.sur_list.blockSignals(False)

    def load_profile_into_ui(self):
        self.fill_lists()
        self.load_eq_mode(self.store.eq_cur, apply=False)
        self.load_virtual(self.store.virt_cur, apply=False)
        if self.store.virtual(self.store.virt_cur)["VssEnable"]:
            self.sur_debounce.start()
        self.eq_debounce.start()

    def load_eq_mode(self, mid, apply=True):
        m = self.store.eq_mode(mid) or self.store.eq_modes[0]
        self.store.eq_cur = m["id"]
        widgets = [self.bass, self.treble] + self.band
        for s in widgets:
            s.blockSignals(True)
        self.bass.setValue(self.store.bass)
        self.treble.setValue(self.store.treble)
        for s, v in zip(self.band, m["bands"]):
            s.setValue(max(core.UI_MIN, min(core.UI_MAX, v)))
        for s in widgets:
            s.blockSignals(False)
        self.bass_lbl.setText(str(self.bass.value()))
        self.treble_lbl.setText(str(self.treble.value()))
        for s, l in zip(self.band, self.band_lbl):
            l.setText(str(s.value()))
        if apply:
            self.eq_debounce.start()

    def eq_mode_selected(self, row):
        it = self.eq_list.item(row)
        if it:
            self.load_eq_mode(it.data(Qt.ItemDataRole.UserRole))

    def eq_edited(self):
        self.eq_debounce.start()

    def add_eq_mode(self):
        name, ok = QInputDialog.getText(self, "ADD NEW PROFILE", "Name:")
        if ok and name.strip():
            mid = self.store.add_eq_mode(name.strip())
            self.store.eq_cur = mid
            self.store.save()
            self.fill_lists()
            self.load_eq_mode(mid)

    def eq_reset(self):
        for s in [self.bass, self.treble] + self.band:
            s.setValue(0)

    def eq_save(self):
        m = self.store.eq_mode(self.store.eq_cur)
        m["bands"] = [s.value() for s in self.band]
        self.store.bass, self.store.treble = self.bass.value(), self.treble.value()
        self.store.save()
        self.log(f"saved sound profile '{m['name']}'")

    def eq_cancel(self):
        self.load_eq_mode(self.store.eq_cur)

    def current_cmeq(self):
        return core.eq_to_driver(self.bass.value(), self.treble.value(), [s.value() for s in self.band])

    def apply_eq(self):
        try:
            cmeq = self.current_cmeq()
        except core.EQError as e:
            self.log(f"EQ rejected like the Windows driver would: {e}")
            return
        props = ", ".join(f"{n}={v}" for n, _, v in core.driver_properties(cmeq) if v)
        self.eq_info.setText("Driver slots: " + (props or "all 0 (flat)"))
        flat = not any(cmeq)
        if not self.eq_enable.isChecked() or flat and not self.eq_proc.running():
            if not self.eq_enable.isChecked():
                self.stop_eq()
            return
        if not core.find_bin("pipewire"):
            self.log("`pipewire` binary not found; EQ cannot be applied")
            return
        try:
            if self.eq_proc.running() and self.eq_proc.set_params(core.eq_live_params(cmeq)):
                return
            self.start_eq(cmeq)
        except OSError as e:
            self.log(f"EQ start failed: {e}")
            self.status.showMessage(f"EQ failed: {e}")

    def mh650_sink(self):
        found = core.find_mh650("sinks")
        return found[0] if found else None

    def start_eq(self, cmeq):
        sink = self.mh650_sink()
        if not sink:
            raise OSError("MH650 audio output not found (is the headset connected and set up in PipeWire?)")
        self.eq_proc.start(core.build_eq_conf(cmeq, sink))
        self.log(f"EQ filter-chain started -> {sink}")
        if self.eq_default.isChecked():
            core.set_default_sink("effect_input.mh650_eq")
        if self.sur_proc.running():
            self.sur_debounce.start()

    def stop_eq(self):
        was = self.eq_proc.running()
        self.eq_proc.stop()
        if was:
            sink = self.mh650_sink()
            if sink and self.eq_default.isChecked():
                core.set_default_sink(sink)
            self.log("EQ stopped")

    # ------------------------------------------------------------------ audio levels
    def refresh_audio(self):
        for kind, slider, mute in (("sinks", self.vol, self.vol_mute), ("sources", self.mic, self.mic_mute)):
            f = core.find_mh650(kind)
            if not f or slider.isSliderDown():
                continue
            _, pct, muted = f
            slider.blockSignals(True)
            mute.blockSignals(True)
            slider.setValue(pct)
            mute.setChecked(muted)
            slider.blockSignals(False)
            mute.blockSignals(False)

    def set_level(self, kind, v):
        f = core.find_mh650(kind)
        if f:
            core.set_volume(kind, f[0], v)

    def set_mute_state(self, kind, m):
        f = core.find_mh650(kind)
        if f:
            core.set_mute(kind, f[0], m)

    # ------------------------------------------------------------------ SURROUND
    def page_surround(self):
        w = QWidget()
        top = QHBoxLayout(w)
        left = QVBoxLayout()
        left.addWidget(QLabel("7.1 SURROUND SOUND PROFILES"))
        self.sur_list = QListWidget()
        self.sur_list.currentRowChanged.connect(self.virtual_selected)
        self.sur_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sur_list.customContextMenuRequested.connect(lambda p: self.list_menu(self.sur_list, p, "virt"))
        left.addWidget(self.sur_list, 1)
        add = QPushButton("ADD NEW PROFILE")
        add.clicked.connect(self.add_virtual)
        left.addWidget(add)
        self.vss = QCheckBox("ENABLE 7.1 VIRTUAL SURROUND")
        self.vss.toggled.connect(self.vss_toggled)
        self.smvss = QCheckBox("SMVSS")
        self.smvss.toggled.connect(self.smvss_toggled)
        left.addWidget(self.vss)
        left.addWidget(self.smvss)
        self.sofa_lbl = QLabel()
        self.sofa_lbl.setWordWrap(True)
        pick = QPushButton("Choose HRTF (.sofa)...")
        pick.clicked.connect(self.pick_sofa)
        left.addWidget(self.sofa_lbl)
        left.addWidget(pick)
        row = QHBoxLayout()
        for text, fn in (("RESET", self.sur_reset), ("SAVE", self.sur_save), ("CANCEL", self.sur_cancel)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        left.addLayout(row)
        top.addLayout(left, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel("7.1 VIRTUAL SPEAKER PLACEMENT"))
        self.stage = StageWidget()
        self.stage.changed.connect(self.stage_changed)
        self.stage.selected.connect(self.speaker_selected)
        right.addWidget(self.stage, 1)
        dist = QGroupBox("SURROUND SOUND DISTANCE")
        dl = QHBoxLayout(dist)
        near = QPushButton("NEAR")
        far = QPushButton("FAR")
        near.clicked.connect(lambda: self.nudge_distance(-5))
        far.clicked.connect(lambda: self.nudge_distance(+5))
        self.dist_slider = QSlider(Qt.Orientation.Horizontal)
        self.dist_slider.setRange(0, 100)
        self.dist_slider.valueChanged.connect(self.distance_changed)
        self.sel_lbl = QLabel("C")
        dl.addWidget(self.sel_lbl)
        dl.addWidget(near)
        dl.addWidget(self.dist_slider, 1)
        dl.addWidget(far)
        right.addWidget(dist)
        rot = QGroupBox("SURROUND SOUND ROTATION")
        rl = QHBoxLayout(rot)
        self.cw = QPushButton("CLOCKWISE")
        self.ccw = QPushButton("COUNTER-CLOCKWISE")
        for b in (self.cw, self.ccw):
            b.setCheckable(True)
        self.cw.toggled.connect(lambda on: self.rotate_toggled(+1, on))
        self.ccw.toggled.connect(lambda on: self.rotate_toggled(-1, on))
        rl.addWidget(self.ccw)
        rl.addWidget(self.cw)
        right.addWidget(rot)
        top.addLayout(right, 2)
        self.update_sofa_label()
        return w

    def update_sofa_label(self):
        if self.sofa:
            self.sofa_lbl.setText(f"HRTF: {os.path.basename(self.sofa)}")
        else:
            self.sofa_lbl.setText("No .sofa HRTF file found. Virtual surround needs one "
                                  "(e.g. from the 'sofa'/'hrtf' packages or sofacoustics.org).")

    def pick_sofa(self):
        p, _ = QFileDialog.getOpenFileName(self, "HRTF", os.path.expanduser("~"), "SOFA (*.sofa)")
        if p:
            self.sofa = p
            self.update_sofa_label()
            self.sur_debounce.start()

    def virt(self):
        return self.virt_cur

    def load_virtual(self, vid, apply=True):
        v = self.store.virtual(vid) or self.store.virtuals[0]
        self.store.virt_cur = v["id"]
        self.virt_cur = dict(v, distance=list(v["distance"]), angle=list(v["angle"]))
        self.stage.set_data(self.virt_cur["angle"], self.virt_cur["distance"])
        for cb, key in ((self.vss, "VssEnable"), (self.smvss, "SmvssEnable")):
            cb.blockSignals(True)
            cb.setChecked(bool(self.virt_cur[key]))
            cb.blockSignals(False)
        self.speaker_selected(self.stage.sel)
        if apply:
            self.sur_debounce.start()

    def virtual_selected(self, row):
        it = self.sur_list.item(row)
        if it:
            self.load_virtual(it.data(Qt.ItemDataRole.UserRole))

    def add_virtual(self):
        name, ok = QInputDialog.getText(self, "ADD NEW PROFILE", "Name:")
        if ok and name.strip():
            vid = self.store.add_virtual(name.strip())
            self.store.virt_cur = vid
            self.store.save()
            self.fill_lists()
            self.load_virtual(vid)

    def speaker_selected(self, i):
        self.sel_lbl.setText(core.SPEAKERS[i])
        self.dist_slider.blockSignals(True)
        self.dist_slider.setValue(self.virt_cur["distance"][i])
        self.dist_slider.blockSignals(False)

    def stage_changed(self):
        self.virt_cur["angle"] = list(self.stage.angles)
        self.virt_cur["distance"] = list(self.stage.dists)
        self.speaker_selected(self.stage.sel)
        self.sur_debounce.start()

    def distance_changed(self, v):
        self.stage.dists[self.stage.sel] = v
        self.stage.update()
        self.stage_changed()

    def nudge_distance(self, d):
        self.dist_slider.setValue(max(0, min(100, self.dist_slider.value() + d)))

    def rotate_toggled(self, direction, on):
        other = self.ccw if direction == +1 else self.cw
        if on:
            other.blockSignals(True)
            other.setChecked(False)
            other.blockSignals(False)
            self.rot_dir = direction
            self.rot_timer.start()
        elif self.rot_dir == direction:
            self.rot_dir = 0
            self.rot_timer.stop()

    def rot_tick(self):
        for i in range(7):                                   # LFE keeps its angle
            self.stage.angles[i] = core.wrap_angle(self.stage.angles[i] + 3 * self.rot_dir)
        self.stage.update()
        self.stage_changed()

    def vss_toggled(self, on):
        self.virt_cur["VssEnable"] = int(on)
        self.sur_debounce.start()

    def smvss_toggled(self, on):
        self.virt_cur["SmvssEnable"] = int(on)

    def sur_reset(self):
        d = core.VIRTUAL_DEFAULT
        self.virt_cur["angle"], self.virt_cur["distance"] = list(d["angle"]), list(d["distance"])
        self.stage.set_data(self.virt_cur["angle"], self.virt_cur["distance"])
        self.speaker_selected(self.stage.sel)
        self.sur_debounce.start()

    def sur_save(self):
        v = self.store.virtual(self.store.virt_cur)
        v.update(self.virt_cur)
        self.store.save()
        self.log(f"saved surround profile '{v['name']}'")

    def sur_cancel(self):
        self.load_virtual(self.store.virt_cur)

    def apply_surround(self):
        v = self.virt_cur
        if not v.get("VssEnable"):
            if self.sur_proc.running():
                self.sur_proc.stop()
                self.log("virtual surround stopped")
                if self.eq_default.isChecked():
                    core.set_default_sink("effect_input.mh650_eq" if self.eq_proc.running() else (self.mh650_sink() or ""))
            return
        if not self.sofa:
            self.log("virtual surround needs a .sofa HRTF file (choose one on the SURROUND page)")
            return
        try:
            if self.sur_proc.running() and self.sur_proc.set_params(core.surround_live_params(v)):
                return
            target = "effect_input.mh650_eq" if self.eq_proc.running() else self.mh650_sink()
            if not target:
                raise OSError("MH650 audio output not found")
            self.sur_proc.start(core.build_surround_conf(v, self.sofa, target))
            self.log(f"virtual surround started -> {target}")
            if self.eq_default.isChecked():
                core.set_default_sink("effect_input.mh650_surround")
        except OSError as e:
            self.log(f"virtual surround failed: {e}")
            self.status.showMessage(f"Virtual surround failed: {e}")

    # ------------------------------------------------------------------ LOG / lifecycle
    def page_log(self):
        w = QWidget()
        l = QVBoxLayout(w)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        l.addWidget(self.log_view)
        return w
    
    def closeEvent(self, ev):
        for t in (self.fx_timer, self.rot_timer, self.audio_timer, self.eq_debounce, self.sur_debounce):
            t.stop()
        if self.events:
            self.events.stop()
        # leave eq_proc / sur_proc running so the effect survives closing the window
        self.hid.close()
        ev.accept()


def main():
    if "--install-udev" in sys.argv:
        try:
            core.install_udev_rule_with_pkexec()
            print("udev rule installed. Re-plug the headset if it is still not accessible.")
            return 0
        except OSError as e:
            print(f"failed: {e}", file=sys.stderr)
            return 1
    if "--print-udev-rule" in sys.argv:
        print(core.UDEV_RULE, end="")
        return 0
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
