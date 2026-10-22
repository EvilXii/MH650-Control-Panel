#!/usr/bin/env python3
"""
MH650 Control Panel (PyQt6) - experimental, Linux

What is real here, and how it works:
  * Lighting + button events: HID protocol decoded from MasterPlus (CMUOT.dll). Sent to the
    vendor HID node (report ID 0xFF, 63 bytes) - auto-detected from /sys/class/hidraw.
  * Volume / mic: through pactl (PipeWire-Pulse or PulseAudio) on the MH650 sink/source.
  * Bass / EQ: a PipeWire filter-chain (built-in biquads) written to
    ~/.config/pipewire/pipewire.conf.d/ and loaded by restarting the user PipeWire stack.
    Live gain changes use pw-cli after the node exists. On Windows these are driver DSP.
  * 7.1 virtual surround: same mechanism using PipeWire's HRIR convolver config, pointed
    at a HeSuVi-format HRIR .wav that you provide.

Requires: PyQt6, PipeWire (with pipewire-pulse), pactl, pw-cli. Needs access to /dev/hidrawN
(udev rule: see mh650_protocol.md / findings).
"""
import colorsys
import glob
import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import time

from PyQt6.QtCore import QProcess, QSettings, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSlider, QTabWidget, QVBoxLayout, QWidget,
)

VID = 0x2516
CACHE = os.path.expanduser("~/.cache/mh650")
PW_CONF_D = os.path.expanduser("~/.config/pipewire/pipewire.conf.d")
EQ_CONF_NAME = "99-mh650-eq.conf"
SUR_CONF_NAME = "99-mh650-surround.conf"
EQ_SINK_NAME = "effect_input.mh650_eq"
SUR_SINK_NAME = "effect_input.mh650_surround"

SURROUND_CONF_CANDIDATES = [
    "/usr/share/pipewire/filter-chain/sink-virtual-surround-7.1-hesuvi.conf",
    "/etc/pipewire/filter-chain/sink-virtual-surround-7.1-hesuvi.conf",
    "/usr/share/pipewire/filter-chain.conf.avail/sink-virtual-surround-7.1-hesuvi.conf",
]

# Extra dirs often missing when the app is started from a desktop icon / flatpak-ish env
_EXTRA_BIN_DIRS = [
    "/usr/bin",
    "/usr/local/bin",
    "/bin",
    "/sbin",
    "/usr/sbin",
    os.path.expanduser("~/.local/bin"),
    "/usr/libexec/pipewire",
]


def _ensure_path():
    """Make sure common bin dirs are on PATH for subprocesses (GUI launch often has a thin PATH)."""
    cur = os.environ.get("PATH", "")
    parts = cur.split(":") if cur else []
    for d in _EXTRA_BIN_DIRS:
        if d and d not in parts and os.path.isdir(d):
            parts.append(d)
    os.environ["PATH"] = ":".join(parts)


_ensure_path()


def find_bin(name):
    """Locate an executable even when PATH is incomplete."""
    p = shutil.which(name)
    if p:
        return p
    for d in _EXTRA_BIN_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


PACTL = find_bin("pactl")
PW_CLI = find_bin("pw-cli")
SYSTEMCTL = find_bin("systemctl")
PIPEWIRE = find_bin("pipewire")


# --------------------------------------------------------------------------- HID layer
def pkt(*b):
    p = bytearray(64)
    p[: len(b)] = bytes(b)
    return bytes(p)


LIGHT_ENABLE = pkt(0xFF, 0x10, 0x03, 0x00, 0x03, 0x01)
LIGHT_RELEASE = pkt(0xFF, 0x10, 0x03, 0x00, 0x03, 0x00)
LIGHT_OFF = pkt(0xFF, 0x10, 0x04, 0x00, 0x00, 0x00, 0x00)


def light_color(r, g, b):
    return pkt(0xFF, 0x10, 0x04, 0x00, r, g, b, 0xFF)


def parse_descriptor(raw):
    i, size, count, rid = 0, 0, 0, 0
    totals = {}
    while i < len(raw):
        b = raw[i]
        if b == 0xFE:
            i += 3 + raw[i + 1]
            continue
        sz = {0: 0, 1: 1, 2: 2, 3: 4}[b & 3]
        val = int.from_bytes(raw[i + 1: i + 1 + sz], "little")
        tag = b & 0xFC
        if tag == 0x74:
            size = val
        elif tag == 0x94:
            count = val
        elif tag == 0x84:
            rid = val
        elif tag in (0x80, 0x90, 0xB0):
            kind = {0x80: "input", 0x90: "output", 0xB0: "feature"}[tag]
            totals[(rid, kind)] = totals.get((rid, kind), 0) + size * count
        i += 1 + sz
    return totals


def find_control_node():
    """Cooler Master hidraw node whose descriptor has report ID 0xFF with 63-byte output."""
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            dev = os.path.realpath(os.path.join(node, "device"))
            vid = pid = None
            with open(os.path.join(dev, "uevent")) as f:
                for line in f:
                    if line.startswith("HID_ID="):
                        _, v, p = line.strip().split("=")[1].split(":")
                        vid, pid = int(v, 16), int(p, 16)
            if vid != VID:
                continue
            with open(os.path.join(dev, "report_descriptor"), "rb") as f:
                totals = parse_descriptor(f.read())
            if totals.get((0xFF, "output")) == 504:
                return "/dev/" + os.path.basename(node), pid
        except (OSError, ValueError):
            continue
    return None, None


class Lighting:
    """Owns the HID fd. All methods raise OSError on failure."""

    def __init__(self):
        self.fd = None
        self.path = None
        self.enabled = False

    def connect(self):
        path, pid = find_control_node()
        if not path:
            raise OSError("No MH650 control HID node found (plug in the headset / dongle).")
        self.fd = os.open(path, os.O_RDWR)
        self.path = f"{path} (2516:{pid:04x})"
        return self.path

    def _send(self, data):
        if self.fd is None:
            raise OSError("not connected")
        os.write(self.fd, data)

    def color(self, r, g, b):
        if not self.enabled:
            self._send(LIGHT_ENABLE)
            self.enabled = True
            time.sleep(0.05)
        self._send(light_color(r, g, b))

    def off(self):
        if not self.enabled:
            self._send(LIGHT_ENABLE)
            self.enabled = True
            time.sleep(0.05)
        self._send(LIGHT_OFF)

    def release(self):
        if self.fd is not None and self.enabled:
            try:
                self._send(LIGHT_RELEASE)
            finally:
                self.enabled = False

    def close(self):
        try:
            self.release()
        except OSError:
            pass
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class EventThread(QThread):
    report = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, path):
        super().__init__()
        self.path = path
        self._run = True

    def run(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            self.failed.emit(str(e))
            return
        try:
            while self._run:
                r, _, _ = select.select([fd], [], [], 0.3)
                if not r:
                    continue
                data = os.read(fd, 64)
                note = ""
                for off in (0, 1):
                    if len(data) > off + 2 and data[off] == 0x10 and data[off + 1] == 0x06:
                        note = f"   <- notification value=0x{data[off + 2]:02x}"
                        break
                self.report.emit(data.hex(" ") + note)
        except OSError as e:
            self.failed.emit(str(e))
        finally:
            os.close(fd)

    def stop(self):
        self._run = False
        self.wait(1000)


# --------------------------------------------------------------------------- audio / PipeWire helpers
def sh(*args, timeout=8):
    try:
        env = os.environ.copy()
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout or "", r.stderr or "", r.returncode
    except (OSError, subprocess.SubprocessError) as e:
        return "", str(e), 1


def sh_out(*args, timeout=8):
    out, _, _ = sh(*args, timeout=timeout)
    return out


def tools_ok():
    """Return (ok, message). Re-resolves bins in case PATH was fixed later."""
    global PACTL, PW_CLI, SYSTEMCTL
    if not PACTL:
        PACTL = find_bin("pactl")
    if not PW_CLI:
        PW_CLI = find_bin("pw-cli")
    if not SYSTEMCTL:
        SYSTEMCTL = find_bin("systemctl")
    missing = []
    if not PACTL:
        missing.append("pactl")
    if not PW_CLI:
        missing.append("pw-cli")
    if missing:
        return False, (
            f"{', '.join(missing)} not found in PATH or common locations.\n"
            "Install pipewire-pulse + pipewire-utils (or pipewire-bin), then restart this app.\n"
            f"Current PATH={os.environ.get('PATH', '')}"
        )
    return True, ""


def pa_names(kind):
    if not PACTL:
        return []
    out = []
    for line in sh_out(PACTL, "list", "short", kind).splitlines():
        parts = line.split("\t")
        if len(parts) > 1:
            out.append(parts[1])
    return out


def find_mh650(kind):
    for n in pa_names(kind):
        if re.search(r"mh650|cooler.?master", n, re.I) and not n.endswith(".monitor"):
            return n
    return None


def find_sink_by_name(name):
    for n in pa_names("sinks"):
        if n == name or name in n:
            return n
    return None


def pa_volume(kind, name):
    if not PACTL:
        return None
    out = sh_out(PACTL, f"get-{kind[:-1]}-volume", name)
    m = re.search(r"(\d+)%", out)
    return int(m.group(1)) if m else None


def pa_muted(kind, name):
    if not PACTL:
        return False
    return "yes" in sh_out(PACTL, f"get-{kind[:-1]}-mute", name)


def restart_pipewire(log_fn):
    """Restart the user PipeWire stack so conf.d changes are picked up."""
    services = ["pipewire", "wireplumber", "pipewire-pulse"]
    if SYSTEMCTL:
        for svc in services:
            out, err, rc = sh(SYSTEMCTL, "--user", "restart", svc)
            if rc != 0 and (err or out):
                log_fn(f"systemctl restart {svc}: {(err or out).strip()}")
        time.sleep(1.2)
        if PACTL:
            for _ in range(15):
                if sh_out(PACTL, "info"):
                    return True
                time.sleep(0.4)
            log_fn("PipeWire restarted but pactl is not responding yet.")
            return False
        return True
    log_fn("systemctl not found; cannot restart PipeWire automatically.")
    return False


def remove_conf(name, log_fn):
    path = os.path.join(PW_CONF_D, name)
    if os.path.isfile(path):
        try:
            os.remove(path)
            log_fn(f"Removed {path}")
            return True
        except OSError as e:
            log_fn(f"Could not remove {path}: {e}")
    return False


def write_conf(name, text, log_fn):
    os.makedirs(PW_CONF_D, exist_ok=True)
    path = os.path.join(PW_CONF_D, name)
    with open(path, "w") as f:
        f.write(text)
    log_fn(f"Wrote {path}")
    return path


def pw_node_id_by_name(node_name):
    """Return numeric node id for a PipeWire node.name, or None."""
    if not PW_CLI:
        return None
    out = sh_out(PW_CLI, "ls", "Node")
    current_id = None
    for line in out.splitlines():
        m = re.match(r"\s*id\s+(\d+),", line)
        if m:
            current_id = m.group(1)
            continue
        if current_id and f'node.name = "{node_name}"' in line:
            return current_id
    dump_bin = find_bin("pw-dump")
    if dump_bin:
        dump = sh_out(dump_bin)
        if dump:
            try:
                data = json.loads(dump)
                for obj in data:
                    if obj.get("type") == "PipeWire:Interface:Node":
                        props = (obj.get("info") or {}).get("props") or {}
                        if props.get("node.name") == node_name:
                            return str(obj.get("id"))
            except (json.JSONDecodeError, TypeError, KeyError):
                pass
    return None


def pw_set_eq_gains(node_id, bass, bands, treble, log_fn):
    """Update live control values on an already-loaded EQ filter-chain."""
    if not PW_CLI:
        log_fn("pw-cli not available")
        return False
    params = [
        f'"bass:Gain" {float(bass):.1f}',
        f'"treble:Gain" {float(treble):.1f}',
    ]
    for i, g in enumerate(bands):
        params.append(f'"band{i}:Gain" {float(g):.1f}')
    arg = "{ params = [ " + " ".join(params) + " ] }"
    out, err, rc = sh(PW_CLI, "s", str(node_id), "Props", arg)
    if rc != 0:
        log_fn(f"pw-cli set gains failed: {(err or out).strip()}")
        return False
    return True


EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_PRESETS = {
    "Flat": (0, [0] * 10, 0),
    "Bass boost": (6, [5, 4, 3, 1, 0, 0, 0, 0, 0, 0], 0),
    "FPS footsteps": (-2, [-5, -4, -3, -2, -1, 0, 2, 4, 3, 1], 1),
    "Vocal / voice": (-2, [-3, -2, -1, 0, 2, 4, 4, 2, 0, -1], 0),
    "Bright": (-1, [-2, -1, 0, 0, 0, 1, 2, 4, 5, 6], 3),
    "Movie": (4, [4, 3, 2, 0, -1, -1, 0, 2, 3, 3], 2),
}


def build_eq_conf(bass, bands, treble, target_sink):
    """
    Build a full pipewire.conf.d snippet that loads libpipewire-module-filter-chain
    with a 12-node biquad chain (bass shelf + 10 peaking + treble shelf).
    """
    chain = [("bass", "bq_lowshelf", 100, 0.7, bass)]
    chain += [(f"band{i}", "bq_peaking", f, 1.1, g) for i, (f, g) in enumerate(zip(EQ_FREQS, bands))]
    chain += [("treble", "bq_highshelf", 12000, 0.7, treble)]

    nodes_lines = []
    for n, lab, fq, q, g in chain:
        nodes_lines.append(
            f'                    {{ type = builtin name = {n} label = {lab}\n'
            f'                      control = {{ "Freq" = {fq} "Q" = {q} "Gain" = {g:.1f} }} }}'
        )
    nodes = "\n".join(nodes_lines)

    links_lines = []
    for i in range(len(chain) - 1):
        links_lines.append(
            f'                    {{ output = "{chain[i][0]}:Out" input = "{chain[i + 1][0]}:In" }}'
        )
    links = "\n".join(links_lines)

    target_line = ""
    if target_sink:
        target_line = f'\n                target.object = "{target_sink}"'

    return f"""# MH650 EQ – managed by mh650_gui.py. Do not edit by hand while the GUI is open.
context.modules = [
    {{ name = libpipewire-module-filter-chain
        flags = [ nofail ]
        args = {{
            node.description = "MH650 EQ"
            media.name       = "MH650 EQ"
            filter.graph = {{
                nodes = [
{nodes}
                ]
                links = [
{links}
                ]
            }}
            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {{
                node.name   = "{EQ_SINK_NAME}"
                media.class = Audio/Sink
                node.nick   = "MH650 EQ"
            }}
            playback.props = {{
                node.name    = "effect_output.mh650_eq"
                node.passive = true
                audio.channels = 2
                audio.position = [ FL FR ]{target_line}
            }}
        }}
    }}
]
"""


def build_surround_conf(hrir_path, target_sink):
    """
    Load the stock HeSuVi 7.1 filter-chain template, rewrite the HRIR filename
    to an absolute path, give unique node names, and optionally target the
    physical MH650 sink.
    """
    template = None
    for p in SURROUND_CONF_CANDIDATES:
        if os.path.isfile(p):
            with open(p) as f:
                template = f.read()
            break
    if not template:
        raise RuntimeError(
            "PipeWire's sink-virtual-surround-7.1-hesuvi.conf was not found.\n"
            "Install a recent PipeWire package that ships filter-chain examples, "
            "or copy the file from the PipeWire sources into one of:\n  "
            + "\n  ".join(SURROUND_CONF_CANDIDATES)
        )

    # Absolute path, escaped for SPA-JSON string
    hrir_abs = os.path.abspath(hrir_path).replace("\\", "/")
    text, n = re.subn(
        r'filename\s*=\s*"[^"]*"',
        f'filename = "{hrir_abs}"',
        template,
    )
    if n == 0:
        raise RuntimeError("Could not find any 'filename = \"...\"' lines in the surround template.")

    # Unique node names so we don't clash with a user-installed copy
    text = text.replace(
        "effect_input.virtual-surround-7.1-hesuvi",
        SUR_SINK_NAME,
    )
    text = text.replace(
        "effect_output.virtual-surround-7.1-hesuvi",
        "effect_output.mh650_surround",
    )
    text = text.replace("Virtual Surround Sink", "MH650 Virtual Surround")
    text = text.replace("node.description = \"Virtual Surround Sink\"",
                        'node.description = "MH650 Virtual Surround"')

    # Inject target.object into playback.props if we know the hardware sink
    if target_sink:
        # Insert before the closing of playback.props if not already present
        if "target.object" not in text:
            text = re.sub(
                r'(playback\.props\s*=\s*\{)',
                rf'\1\n                target.object = "{target_sink}"',
                text,
                count=1,
            )

    # Ensure flags = [ nofail ] is present
    if "flags = [ nofail ]" not in text and "flags = [nofail]" not in text:
        text = text.replace(
            "name = libpipewire-module-filter-chain",
            "name = libpipewire-module-filter-chain\n        flags = [ nofail ]",
            1,
        )

    return text, n


# --------------------------------------------------------------------------- UI
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MH650 Control Panel (experimental)")
        self.resize(780, 660)
        self.cfg = QSettings("mh650-linux", "gui")
        self.light = Lighting()
        self.events = None
        self.t0 = time.time()
        self.fx_timer = QTimer(self, interval=40)
        self.fx_timer.timeout.connect(self.fx_tick)
        self.eq_node_id = None
        self.eq_active = False
        self.sur_active = False

        tabs = QTabWidget()
        tabs.addTab(self.tab_lighting(), "Lighting")
        tabs.addTab(self.tab_sound(), "Sound")
        tabs.addTab(self.tab_eq(), "EQ / Bass")
        tabs.addTab(self.tab_surround(), "7.1 Surround")
        tabs.addTab(self.tab_log(), "Events / Log")
        self.setCentralWidget(tabs)
        self.statusBar().showMessage("Not connected")
        QTimer.singleShot(100, self._log_tools)
        QTimer.singleShot(200, self.connect_device)
        QTimer.singleShot(300, self.refresh_audio)
        QTimer.singleShot(500, self.detect_existing_filters)

    def _log_tools(self):
        self.log(f"pactl  = {PACTL or 'NOT FOUND'}")
        self.log(f"pw-cli = {PW_CLI or 'NOT FOUND'}")
        self.log(f"PATH   = {os.environ.get('PATH', '')}")

    # ---- helpers
    def log(self, text):
        self.logbox.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    def warn(self, text):
        self.statusBar().showMessage(text, 10000)
        self.log(text)

    def connect_device(self):
        try:
            path = self.light.connect()
        except OSError as e:
            msg = str(e)
            if isinstance(e, PermissionError):
                msg = ("Permission denied opening the HID node. Add a udev rule "
                       "(KERNEL==\"hidraw*\", ATTRS{idVendor}==\"2516\", MODE=\"0660\", TAG+=\"uaccess\") "
                       "and replug, or run with sudo.")
            self.statusBar().showMessage(msg)
            self.log(msg)
            return
        self.statusBar().showMessage(f"Connected: {path}")
        self.log(f"Connected to {path}")
        self.events = EventThread(self.light.path.split(" ")[0])
        self.events.report.connect(self.log)
        self.events.failed.connect(lambda m: self.log(f"event listener stopped: {m}"))
        self.events.start()

    def hid(self, fn, *a):
        try:
            if self.light.fd is None:
                self.connect_device()
            fn(*a)
            return True
        except OSError as e:
            self.fx_timer.stop()
            self.warn(f"HID write failed: {e}")
            return False

    def detect_existing_filters(self):
        if find_sink_by_name(EQ_SINK_NAME):
            self.eq_active = True
            self.eq_node_id = pw_node_id_by_name(EQ_SINK_NAME)
            self.eq_status.setText(f"EQ active (node {self.eq_node_id or '?'})")
            self.log(f"Detected existing EQ sink {EQ_SINK_NAME}")
        if find_sink_by_name(SUR_SINK_NAME):
            self.sur_active = True
            self.sur_status.setText("7.1 active")
            self.log(f"Detected existing surround sink {SUR_SINK_NAME}")

    # ---- lighting
    def tab_lighting(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("Color")
        g = QGridLayout(box)
        self.swatch = QLabel()
        self.swatch.setFixedSize(70, 40)
        self.hex = QLineEdit(self.cfg.value("color", "#FF0000"))
        self.hex.setMaximumWidth(110)
        self.hex.editingFinished.connect(self.color_changed)
        pick = QPushButton("Pick...")
        pick.clicked.connect(self.pick_color)
        g.addWidget(self.swatch, 0, 0)
        g.addWidget(self.hex, 0, 1)
        g.addWidget(pick, 0, 2)
        presets = ["#FF0000", "#FF7F00", "#FFFF00", "#00FF00", "#00FFFF", "#0000FF", "#8000FF", "#FF00AA", "#FFFFFF"]
        row = QHBoxLayout()
        for c in presets:
            b = QPushButton()
            b.setFixedSize(30, 26)
            b.setStyleSheet(f"background:{c}; border:1px solid #555;")
            b.clicked.connect(lambda _=False, c=c: self.set_hex(c))
            row.addWidget(b)
        row.addStretch()
        g.addLayout(row, 1, 0, 1, 3)
        lay.addWidget(box)

        fx = QGroupBox("Effect (software-animated)")
        f = QFormLayout(fx)
        self.fx = QComboBox()
        self.fx.addItems(["Static", "Breathing", "Color cycle", "Strobe"])
        self.fx.setCurrentText(self.cfg.value("fx", "Static"))
        self.bright = QSlider(Qt.Orientation.Horizontal)
        self.bright.setRange(5, 100)
        self.bright.setValue(int(self.cfg.value("bright", 100)))
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(1, 10)
        self.speed.setValue(int(self.cfg.value("speed", 4)))
        f.addRow("Effect", self.fx)
        f.addRow("Brightness", self.bright)
        f.addRow("Speed", self.speed)
        lay.addWidget(fx)

        btns = QHBoxLayout()
        for text, fn in (("Apply", self.apply_light), ("Lights off", self.light_off),
                         ("Release to headset", self.light_release)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        lay.addLayout(btns)
        note = QLabel("Sets one color for all LEDs (the only lighting command decoded so far). Effects are "
                      "animated by this app, so they stop when it closes; 'Release' returns control to the headset.")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch()
        self.paint_swatch()
        return w

    def cur_rgb(self):
        c = QColor(self.hex.text())
        if not c.isValid():
            c = QColor("#FF0000")
        return c.red(), c.green(), c.blue()

    def paint_swatch(self):
        r, g, b = self.cur_rgb()
        self.swatch.setStyleSheet(f"background: rgb({r},{g},{b}); border:1px solid #555;")

    def set_hex(self, c):
        self.hex.setText(c)
        self.color_changed()
        self.apply_light()

    def color_changed(self):
        self.paint_swatch()
        self.cfg.setValue("color", self.hex.text())

    def pick_color(self):
        c = QColorDialog.getColor(QColor(self.hex.text()), self, "Headset color")
        if c.isValid():
            self.set_hex(c.name().upper())

    def scaled(self, rgb, k):
        return tuple(max(0, min(255, int(v * k))) for v in rgb)

    def apply_light(self):
        self.cfg.setValue("fx", self.fx.currentText())
        self.cfg.setValue("bright", self.bright.value())
        self.cfg.setValue("speed", self.speed.value())
        self.t0 = time.time()
        if self.fx.currentText() == "Static":
            self.fx_timer.stop()
            self.hid(lambda: self.light.color(*self.scaled(self.cur_rgb(), self.bright.value() / 100)))
        else:
            self.fx_timer.start()

    def fx_tick(self):
        t = (time.time() - self.t0) * (0.4 + self.speed.value() * 0.35)
        k = self.bright.value() / 100
        mode = self.fx.currentText()
        r, g, b = self.cur_rgb()
        if mode == "Breathing":
            k *= 0.08 + 0.92 * (math.sin(t * math.pi * 2 - math.pi / 2) + 1) / 2
            rgb = self.scaled((r, g, b), k)
        elif mode == "Color cycle":
            rr, gg, bb = colorsys.hsv_to_rgb((t * 0.25) % 1.0, 1.0, 1.0)
            rgb = self.scaled((rr * 255, gg * 255, bb * 255), k)
        else:  # Strobe
            rgb = self.scaled((r, g, b), k) if int(t * 4) % 2 == 0 else (0, 0, 0)
        self.hid(lambda: self.light.color(*rgb))

    def light_off(self):
        self.fx_timer.stop()
        self.hid(self.light.off)

    def light_release(self):
        self.fx_timer.stop()
        self.hid(self.light.release)

    # ---- sound
    def tab_sound(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.snd_info = QLabel("")
        lay.addWidget(self.snd_info)
        out = QGroupBox("Headphones (output)")
        o = QHBoxLayout(out)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 150)
        self.vol_lbl = QLabel("0%")
        self.vol_mute = QCheckBox("Mute")
        o.addWidget(self.vol)
        o.addWidget(self.vol_lbl)
        o.addWidget(self.vol_mute)
        lay.addWidget(out)
        inp = QGroupBox("Microphone (input)")
        i = QHBoxLayout(inp)
        self.mic = QSlider(Qt.Orientation.Horizontal)
        self.mic.setRange(0, 150)
        self.mic_lbl = QLabel("0%")
        self.mic_mute = QCheckBox("Mute")
        i.addWidget(self.mic)
        i.addWidget(self.mic_lbl)
        i.addWidget(self.mic_mute)
        lay.addWidget(inp)
        r = QPushButton("Refresh devices")
        r.clicked.connect(self.refresh_audio)
        lay.addWidget(r)
        lay.addStretch()
        self.vol.valueChanged.connect(lambda v: self.set_vol("sinks", self.sink, v, self.vol_lbl))
        self.mic.valueChanged.connect(lambda v: self.set_vol("sources", self.source, v, self.mic_lbl))
        self.vol_mute.toggled.connect(lambda m: self.set_mute("sinks", self.sink, m))
        self.mic_mute.toggled.connect(lambda m: self.set_mute("sources", self.source, m))
        self.sink = self.source = None
        return w

    def refresh_audio(self):
        self.sink, self.source = find_mh650("sinks"), find_mh650("sources")
        self.snd_info.setText(f"Output: {self.sink or 'not found'}\nInput: {self.source or 'not found'}")
        for kind, name, sl, lbl, mu in (("sinks", self.sink, self.vol, self.vol_lbl, self.vol_mute),
                                        ("sources", self.source, self.mic, self.mic_lbl, self.mic_mute)):
            sl.setEnabled(bool(name))
            mu.setEnabled(bool(name))
            if name:
                v = pa_volume(kind, name)
                for wdg in (sl, mu):
                    wdg.blockSignals(True)
                sl.setValue(v or 0)
                lbl.setText(f"{v or 0}%")
                mu.setChecked(pa_muted(kind, name))
                for wdg in (sl, mu):
                    wdg.blockSignals(False)

    def set_vol(self, kind, name, v, lbl):
        lbl.setText(f"{v}%")
        if name and PACTL:
            sh(PACTL, f"set-{kind[:-1]}-volume", name, f"{v}%")

    def set_mute(self, kind, name, m):
        if name and PACTL:
            sh(PACTL, f"set-{kind[:-1]}-mute", name, "1" if m else "0")

    # ---- EQ / bass
    def tab_eq(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItems(EQ_PRESETS.keys())
        self.preset.currentTextChanged.connect(self.load_preset)
        top.addWidget(QLabel("Preset"))
        top.addWidget(self.preset)
        top.addStretch()
        self.eq_status = QLabel("EQ inactive")
        top.addWidget(self.eq_status)
        lay.addLayout(top)

        shelves = QGroupBox("Bass / Treble (dB)")
        s = QFormLayout(shelves)
        self.bass = self.mk_slider(-12, 12, 0)
        self.treble = self.mk_slider(-12, 12, 0)
        s.addRow("Bass", self.bass)
        s.addRow("Treble", self.treble)
        lay.addWidget(shelves)

        bands = QGroupBox("10-band equalizer (dB)")
        h = QHBoxLayout(bands)
        self.bands = []
        for f in EQ_FREQS:
            col = QVBoxLayout()
            sl = QSlider(Qt.Orientation.Vertical)
            sl.setRange(-12, 12)
            sl.setMinimumHeight(150)
            val = QLabel("0")
            val.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sl.valueChanged.connect(lambda v, val=val: val.setText(str(v)))
            name = QLabel(f"{f // 1000}k" if f >= 1000 else str(f))
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(val)
            col.addWidget(sl, 1, Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(name)
            h.addLayout(col)
            self.bands.append(sl)
        lay.addWidget(bands)

        row = QHBoxLayout()
        self.eq_btn = QPushButton("Apply / enable EQ")
        self.eq_btn.clicked.connect(self.apply_eq)
        live = QPushButton("Update gains (live)")
        live.setToolTip("Change band gains without restarting PipeWire (only works while EQ is already active).")
        live.clicked.connect(self.update_eq_live)
        off = QPushButton("Disable EQ")
        off.clicked.connect(self.stop_eq)
        row.addWidget(self.eq_btn)
        row.addWidget(live)
        row.addWidget(off)
        lay.addLayout(row)
        n = QLabel(
            "Apply writes a filter-chain into ~/.config/pipewire/pipewire.conf.d/ and restarts "
            "PipeWire (brief audio drop). The new 'MH650 EQ' sink becomes the default output and "
            "routes to your headset. 'Update gains (live)' adjusts bands without a restart once EQ is running. "
            "Do not run EQ and 7.1 at the same time."
        )
        n.setWordWrap(True)
        lay.addWidget(n)
        return w

    def mk_slider(self, lo, hi, v):
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(v)
        sl.setTickInterval(3)
        sl.setTickPosition(QSlider.TickPosition.TicksBelow)
        return sl

    def load_preset(self, name):
        bass, bands, treble = EQ_PRESETS[name]
        self.bass.setValue(bass)
        self.treble.setValue(treble)
        for sl, g in zip(self.bands, bands):
            sl.setValue(g)

    def apply_eq(self):
        ok, msg = tools_ok()
        if not ok:
            self.warn(msg)
            return
        target = find_mh650("sinks")
        if not target:
            reply = QMessageBox.question(
                self, "No MH650 sink",
                "No Cooler Master / MH650 sink was found. Continue and let WirePlumber choose the default target?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            target = None

        if self.sur_active:
            self.warn("Disable 7.1 Surround first (both replace the default sink).")
            return

        bass = self.bass.value()
        bands = [s.value() for s in self.bands]
        treble = self.treble.value()
        conf = build_eq_conf(bass, bands, treble, target)
        write_conf(EQ_CONF_NAME, conf, self.log)
        self.log(f"Using pactl={PACTL}  pw-cli={PW_CLI}")
        self.log("Restarting PipeWire to load EQ…")
        self.eq_status.setText("Restarting PipeWire…")
        QApplication.processEvents()
        if not restart_pipewire(self.log):
            self.eq_status.setText("EQ conf written; restart failed")
            self.warn("Could not restart PipeWire. Try: systemctl --user restart pipewire wireplumber pipewire-pulse")
            return

        sink = None
        for _ in range(20):
            sink = find_sink_by_name(EQ_SINK_NAME)
            if sink:
                break
            time.sleep(0.3)
        if not sink:
            self.eq_status.setText("EQ conf loaded but sink not visible")
            self.warn(f"After restart, sink '{EQ_SINK_NAME}' did not appear. Check journalctl --user -u pipewire.")
            return

        sh(PACTL, "set-default-sink", EQ_SINK_NAME)
        self.eq_node_id = pw_node_id_by_name(EQ_SINK_NAME)
        self.eq_active = True
        self.eq_status.setText(f"EQ active (node {self.eq_node_id or '?'})")
        self.log(f"EQ enabled → default sink {EQ_SINK_NAME}")
        self.refresh_audio()

    def update_eq_live(self):
        if not self.eq_active:
            self.warn("EQ is not active. Press 'Apply / enable EQ' first.")
            return
        if not self.eq_node_id:
            self.eq_node_id = pw_node_id_by_name(EQ_SINK_NAME)
        if not self.eq_node_id:
            self.warn("Could not find EQ node id. Re-apply EQ.")
            return
        ok = pw_set_eq_gains(
            self.eq_node_id,
            self.bass.value(),
            [s.value() for s in self.bands],
            self.treble.value(),
            self.log,
        )
        if ok:
            self.log("EQ gains updated live")
            self.statusBar().showMessage("EQ gains updated", 3000)

    def stop_eq(self):
        remove_conf(EQ_CONF_NAME, self.log)
        self.log("Restarting PipeWire to unload EQ…")
        restart_pipewire(self.log)
        self.eq_active = False
        self.eq_node_id = None
        self.eq_status.setText("EQ inactive")
        sink = find_mh650("sinks")
        if sink and PACTL:
            sh(PACTL, "set-default-sink", sink)
            self.log(f"Default sink restored to {sink}")
        self.refresh_audio()

    # ---- surround
    def tab_surround(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        info = QLabel(
            "Virtual 7.1 surround through PipeWire's HRIR convolver. Choose a HeSuVi-format "
            "HRIR .wav file (14-channel, e.g. Atmos / CMSS-3D from the HeSuVi project). "
            "It is not bundled here."
        )
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        self.hrir = QLineEdit(self.cfg.value("hrir", ""))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self.pick_hrir)
        row.addWidget(self.hrir)
        row.addWidget(browse)
        lay.addLayout(row)
        self.sur_status = QLabel("7.1 inactive")
        lay.addWidget(self.sur_status)
        b = QHBoxLayout()
        on = QPushButton("Enable 7.1")
        on.clicked.connect(self.start_surround)
        off = QPushButton("Disable 7.1")
        off.clicked.connect(self.stop_surround)
        b.addWidget(on)
        b.addWidget(off)
        lay.addLayout(b)
        n = QLabel(
            "Enable writes a filter-chain into ~/.config/pipewire/pipewire.conf.d/ and restarts "
            "PipeWire. Set games/apps to 7.1 output (or make the virtual sink default). "
            "Do not run this and the EQ at the same time."
        )
        n.setWordWrap(True)
        lay.addWidget(n)
        lay.addStretch()
        return w

    def pick_hrir(self):
        p, _ = QFileDialog.getOpenFileName(self, "HRIR wav", os.path.expanduser("~"), "WAV (*.wav)")
        if p:
            self.hrir.setText(p)
            self.cfg.setValue("hrir", p)

    def start_surround(self):
        p = self.hrir.text().strip()
        if not os.path.isfile(p):
            self.warn("Choose an existing HeSuVi-format HRIR .wav file first.")
            return
        ok, msg = tools_ok()
        if not ok:
            self.warn(msg)
            return
        if self.eq_active:
            self.warn("Disable EQ first (both replace the default sink).")
            return

        target = find_mh650("sinks")
        try:
            conf, n = build_surround_conf(p, target)
        except RuntimeError as e:
            self.warn(str(e))
            return

        write_conf(SUR_CONF_NAME, conf, self.log)
        self.log(f"Using pactl={PACTL}  pw-cli={PW_CLI}")
        self.log(f"Surround conf: {n} convolver filename(s) rewritten → restarting PipeWire…")
        self.sur_status.setText("Restarting PipeWire…")
        QApplication.processEvents()
        if not restart_pipewire(self.log):
            self.sur_status.setText("Conf written; restart failed")
            self.warn("Could not restart PipeWire. Try: systemctl --user restart pipewire wireplumber pipewire-pulse")
            return

        sink = None
        for _ in range(25):
            sink = find_sink_by_name(SUR_SINK_NAME)
            if sink:
                break
            time.sleep(0.3)
        if not sink:
            self.sur_status.setText("Conf loaded but sink not visible")
            self.warn(
                f"After restart, sink '{SUR_SINK_NAME}' did not appear. "
                "Check that the HRIR is a 14-channel HeSuVi wav and look at "
                "journalctl --user -u pipewire for convolver errors."
            )
            return

        sh(PACTL, "set-default-sink", SUR_SINK_NAME)
        self.sur_active = True
        self.sur_status.setText("7.1 active")
        self.log(f"7.1 enabled → default sink {SUR_SINK_NAME}")
        self.refresh_audio()

    def stop_surround(self):
        remove_conf(SUR_CONF_NAME, self.log)
        self.log("Restarting PipeWire to unload 7.1…")
        restart_pipewire(self.log)
        self.sur_active = False
        self.sur_status.setText("7.1 inactive")
        sink = find_mh650("sinks")
        if sink and PACTL:
            sh(PACTL, "set-default-sink", sink)
            self.log(f"Default sink restored to {sink}")
        self.refresh_audio()

    # ---- log
    def tab_log(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Raw reports from the headset (press its buttons / plug the charger):"))
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        lay.addWidget(self.logbox)
        return w

    def closeEvent(self, ev):
        self.fx_timer.stop()
        if self.events:
            self.events.stop()
        self.light.close()
        # Leave EQ / surround confs in place so they survive app exit.
        # User can Disable them from the tabs if they want them gone.
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(37, 37, 41))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(230, 230, 230))
    pal.setColor(QPalette.ColorRole.Base, QColor(27, 27, 30))
    pal.setColor(QPalette.ColorRole.Text, QColor(230, 230, 230))
    pal.setColor(QPalette.ColorRole.Button, QColor(52, 52, 58))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(230, 230, 230))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(60, 140, 230))
    app.setPalette(pal)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()