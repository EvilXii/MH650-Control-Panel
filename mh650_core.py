#!/usr/bin/env python3
"""
MH650 core (no GUI): everything here is testable without Qt, hardware or PipeWire.

Provenance of each piece (from MasterPlus 1.9.6, CMUOT.dll + MH650SoundWid.dll + PDBs):

  EQ driver model  (EXACT, from disassembly)
    MH650SoundWid::updateSdkEqBand  -> reads 7 VolSlider values (bass, treble, 5 bands)
    CMUOT_MH650::setEQData          -> forwards struct mh650_eq {int bass, treble, eq_value[5]}
    MH650::setMH650DeviceEQData     -> rejects any band outside [-20, 20]; mixes bass into
                                       bands 0,1 and treble into bands 3,4 (AudioEQ::calBandEQValue);
                                       builds CMEQ[10] = {0, x0, x1, 0, x2, x3, x4, 0, 0, 0}
    CMEDIA::setEQData               -> writes CMEQ[i] as int to driver property EQ_Slider(i+1)
    There is NO USB/HID EQ protocol. On Windows the values go to the C-Media APO
    (CMEffectAPO_CMTAC.dll, class CEQ15BandLib) through the endpoint property store.

  Not recoverable statically (flagged ASSUMED below): the APO's filter shapes (Q) and the
  dB-per-unit scale. These are the only guesses in the EQ path.
"""
import glob
import json
import math
import os
import re
import select
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

VID = 0x2516
CONTROL_PID = 0x0111          # vendor HID control channel
AUDIO_PID = 0x0129            # USB audio + media keys

# ----------------------------------------------------------------------------- HID
def pkt(*b):
    p = bytearray(64)
    p[: len(b)] = bytes(b)
    return bytes(p)


LIGHT_ENABLE = pkt(0xFF, 0x10, 0x03, 0x00, 0x03, 0x01)    # setLightingInit
LIGHT_RELEASE = pkt(0xFF, 0x10, 0x03, 0x00, 0x03, 0x00)   # setLightingUninit
LIGHT_OFF = pkt(0xFF, 0x10, 0x04, 0x00, 0x00, 0x00, 0x00)  # setLightingTurnOff


def light_color(r, g, b):                                  # setLightingPortColor
    return pkt(0xFF, 0x10, 0x04, 0x00, r & 255, g & 255, b & 255, 0xFF)


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


def find_control_node(sys_root="/sys/class/hidraw"):
    """hidraw node of the vendor channel (report ID 0xFF, 63-byte output)."""
    for node in sorted(glob.glob(os.path.join(sys_root, "hidraw*"))):
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


class PermissionProblem(OSError):
    """hidraw node exists but this user may not open it (fix: udev rule, not root)."""


UDEV_RULE_NAME = "70-mh650.rules"
UDEV_RULE = (
    '# Cooler Master MH650 vendor control channel: grant the logged-in user access.\n'
    'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2516", '
    'ATTRS{idProduct}=="0111", MODE="0660", TAG+="uaccess"\n'
)


def udev_rule_installed():
    return any(os.path.exists(os.path.join(d, UDEV_RULE_NAME))
               for d in ("/etc/udev/rules.d", "/usr/lib/udev/rules.d", "/lib/udev/rules.d"))


def install_udev_rule_with_pkexec():
    """One-time privileged step. The application itself never runs as root."""
    pk = shutil.which("pkexec")
    if not pk:
        raise OSError("pkexec not found; run: sudo install -m0644 <rule> /etc/udev/rules.d/")
    tmp = os.path.join(tempfile.mkdtemp(prefix="mh650-"), UDEV_RULE_NAME)
    with open(tmp, "w") as f:
        f.write(UDEV_RULE)
    script = (f"install -m 0644 '{tmp}' /etc/udev/rules.d/{UDEV_RULE_NAME} && "
              "udevadm control --reload-rules && "
              "udevadm trigger --subsystem-match=hidraw --attr-match=idVendor=2516")
    r = subprocess.run([pk, "sh", "-c", script], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout or "pkexec failed").strip())


class HidLink:
    """Owns the hidraw fd. Non-blocking; raises OSError / PermissionProblem."""

    def __init__(self):
        self.fd = None
        self.path = None
        self.lighting_on = False

    def connect(self):
        path, pid = find_control_node()
        if not path:
            raise OSError("No MH650 control channel found (2516:0111). Plug in the headset/dongle.")
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as e:
            raise PermissionProblem(f"{path}: permission denied") from e
        self.path = f"{path} (2516:{pid:04x})"
        return self.path

    def _send(self, data):
        if self.fd is None:
            raise OSError("not connected")
        os.write(self.fd, data)

    def _init(self):
        if not self.lighting_on:
            self._send(LIGHT_ENABLE)
            self.lighting_on = True
            time.sleep(0.05)

    def color(self, r, g, b):
        self._init()
        self._send(light_color(r, g, b))

    def off(self):
        self._init()
        self._send(LIGHT_OFF)

    def release(self):
        if self.fd is not None and self.lighting_on:
            try:
                self._send(LIGHT_RELEASE)
            finally:
                self.lighting_on = False

    def read_event(self, timeout=0.2):
        """Return (type, value) for a notification FF 10 06 VV, else None."""
        if self.fd is None:
            return None
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        data = os.read(self.fd, 64)
        return parse_notification(data)

    def close(self):
        try:
            self.release()
        except OSError:
            pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


def parse_notification(buf):
    """MH650RxUsbThread: buf[0]=report id, buf[1]==0x10 and buf[2]==0x06 -> NotifyMH650(6, buf[3])."""
    if len(buf) >= 4 and buf[1] == 0x10 and buf[2] == 0x06:
        return (6, buf[3])
    return None


# ----------------------------------------------------------------------------- Lighting effects
EFFECTS = [  # component.json, in MasterPlus order
    (0, "Static Color"), (1, "Breathing"), (2, "Color Cycle"), (3, "Wave"),
    (4, "Star"), (5, "Marquee"), (6, "Turn Off"), (7, "Customization"),
]
# The MH650 firmware path only exposes ONE colour command (setSyncLightingData is an empty stub
# in CMUOT.dll), so every animated effect is host-driven, exactly as a single-colour stream.
HARDWARE_EFFECTS = {0, 1, 2, 6}        # rendered faithfully as single-colour streams
APPROXIMATED_EFFECTS = {3, 4, 5}       # per-LED pattern collapsed to one colour
UNSUPPORTED_EFFECTS = {7}              # needs per-LED packets that do not exist on this path


def hsv_to_rgb(h, s, v):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255 + .5), int(g * 255 + .5), int(b * 255 + .5)


def effect_color(effect_id, t, rgb, dim=100, speed=3, direction=0):
    """Colour to send at time t (seconds). dim 0..100, speed 1..5."""
    k = max(0, min(100, dim)) / 100.0
    sp = max(1, min(5, speed))
    r, g, b = rgb
    if effect_id == 0:
        f = 1.0
    elif effect_id == 1:                                   # Breathing
        f = 0.5 - 0.5 * math.cos(2 * math.pi * t * sp / 8.0)
    elif effect_id == 2:                                   # Color Cycle
        return hsv_to_rgb(t * sp / 12.0, 1.0, k)
    elif effect_id == 3:                                   # Wave (approx)
        ph = (t * sp / 3.0) * (1 if direction == 0 else -1)
        f = 0.5 + 0.5 * math.sin(2 * math.pi * ph)
    elif effect_id == 4:                                   # Star (approx)
        f = 0.35 + 0.65 * abs(math.sin(t * sp * 2.3) * math.sin(t * sp * 0.7))
    elif effect_id == 5:                                   # Marquee (approx)
        f = 1.0 if int(t * sp * 2) % 2 == 0 else 0.15
    elif effect_id == 6:
        return (0, 0, 0)
    else:
        f = 1.0
    f *= k
    return int(r * f + .5), int(g * f + .5), int(b * f + .5)


# ----------------------------------------------------------------------------- EQ driver model
EQ_MIN, EQ_MAX = -20, 20            # AudioEQ limits set in MH650 ctor (0x14 / 0xffffffec)
UI_MIN, UI_MAX = -10, 10            # MasterPlus slider scale (-10dB .. 10dB, BASS/TREBLE -10..10)
BAND_LABELS = ["60Hz", "120Hz", "500Hz", "1000Hz", "2000Hz"]   # MH650SoundWid labels
BAND_ATTRS = ["Hz64", "Hz250", "Hz1K", "Hz4K", "Hz16K"]        # profile XML attribute names
# 10 C-Media driver slots; MH650 only ever drives slots 2,3,5,6,7 (see module docstring).
# Slot centres are the octave series that makes slots 2,3,5,6,7 land exactly on the UI labels.
SLOT_FREQ = [30, 60, 120, 250, 500, 1000, 2000, 4000, 8000, 16000]
USED_SLOTS = [2, 3, 5, 6, 7]
# property name -> property id, read from CMUOT.dll .data (EQ_Slider10 is 0x3E9, the rest 0x3EA+)
EQ_PROPERTY_IDS = {f"EQ_Slider{i}": (0x3EA + i - 1 if i < 10 else 0x3E9) for i in range(1, 11)}
ASSUMED_Q = 1.41                    # ASSUMED: one-octave graphic EQ band
ASSUMED_DB_PER_UNIT = 1.0           # ASSUMED: driver integer unit == 1 dB


def c_round(x):
    """C round(): half away from zero (CMUOT imports api-ms-win-crt-math round)."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def cal_band_eq_value(knob, band):
    """AudioEQ::calBandEQValue(const int& knob, const int& band, int& out)  (RVA 0x260660).
    knob>=0: band + (max-band)*knob/10 ; knob<0: band + (band-min)*knob/10 ; then round()."""
    if knob >= 0:
        v = band + (EQ_MAX - band) * knob / 10.0
    else:
        v = band + (band - EQ_MIN) * knob / 10.0
    return c_round(v)


class EQError(ValueError):
    pass


def eq_to_driver(bass, treble, bands):
    """mh650_eq -> CMEQ[10]. Raises EQError like setMH650DeviceEQData's early return."""
    if len(bands) != 5:
        raise EQError("need 5 band values")
    for v in bands:
        if not (EQ_MIN <= v <= EQ_MAX):
            raise EQError(f"band value {v} outside [{EQ_MIN},{EQ_MAX}]")
    x0 = cal_band_eq_value(bass, bands[0])
    x1 = cal_band_eq_value(bass, bands[1])
    x2 = bands[2]
    x3 = cal_band_eq_value(treble, bands[3])
    x4 = cal_band_eq_value(treble, bands[4])
    return [0, x0, x1, 0, x2, x3, x4, 0, 0, 0]


def driver_properties(cmeq):
    """What CMEDIA::setEQData writes on Windows: [(property name, id, int value)]."""
    return [(f"EQ_Slider{i + 1}", EQ_PROPERTY_IDS[f"EQ_Slider{i + 1}"], int(v)) for i, v in enumerate(cmeq)]


def slot_gain_db(v):
    return float(v) * ASSUMED_DB_PER_UNIT


# ----------------------------------------------------------------------------- Surround model
SPEAKERS = ["LF", "C", "RF", "RS", "RB", "LB", "LS", "LFE"]        # SS_*_ANGLE order in CMUOT
# map to PipeWire 7.1 positions
SPEAKER_POS = {"LF": "FL", "C": "FC", "RF": "FR", "RS": "SR", "RB": "RR", "LB": "RL", "LS": "SL", "LFE": "LFE"}
VIRTUAL_DEFAULT = dict(name="Default", VssEnable=0, SmvssEnable=1,
                       distance=[30] * 8, angle=[-45, 0, 45, 90, 135, -135, -90, 15])
ENVIRONMENTS = ["DEFAULT", "GENERIC", "PADDED_CELL", "ROOM", "BATHROOM", "LIVING_ROOM", "STONE_ROOM",
                "AUDITORIUM", "CONCERT_HALL", "CAVE", "ARENA", "HANGAR", "CARPETED_HALLWAY", "HALLWAY",
                "STONE_CORRIDOR", "ALLEY", "FOREST", "CITY", "MOUNTAINS", "QUARRY", "PLAIN", "PARKING_LOT",
                "SEWER_PIPE", "UNDER_WATER", "DRUGGED", "DIZZY", "PSYCHOTIC", "KARAOKE"]
ROOM_SIZES = ["SMALL_ROOM", "MEDIUM_ROOM", "LARGE_ROOM"]


def wrap_angle(a):
    a = (a + 180) % 360 - 180
    return 180 if a == -180 else a


# ----------------------------------------------------------------------------- Profiles (MasterPlus XML)
DEFAULT_EQ_MODES = [
    dict(id=1, name="POP MUSIC", bands=[7, 6, 5, 7, 7]),
    dict(id=2, name="MOVIE", bands=[8, 8, 7, 7, 5]),
    dict(id=3, name="GAMING(BASS+)", bands=[5, 6, 5, 8, 6]),
]


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "mh650")


class ProfileStore:
    """Same element/attribute names as MasterPlus' profile files (<mh650><profile><sound><eq/>...).
    bass/treble are stored as extra attributes on <sound> (the Windows schema for them is unknown)."""

    def __init__(self, path=None):
        self.path = path or os.path.join(config_dir(), "profile", "MH650", "default.xml")
        self.eq_modes = [dict(m, bands=list(m["bands"])) for m in DEFAULT_EQ_MODES]
        self.eq_cur = 1
        self.bass = 0
        self.treble = 0
        self.virtuals = [dict(VIRTUAL_DEFAULT, id=1, distance=list(VIRTUAL_DEFAULT["distance"]),
                              angle=list(VIRTUAL_DEFAULT["angle"]))]
        self.virt_cur = 1
        self.load()

    # -- eq modes
    def eq_mode(self, mid):
        return next((m for m in self.eq_modes if m["id"] == mid), None)

    def add_eq_mode(self, name):
        nid = max([m["id"] for m in self.eq_modes] + [0]) + 1
        self.eq_modes.append(dict(id=nid, name=name, bands=[0] * 5))
        return nid

    def del_eq_mode(self, mid):
        if len(self.eq_modes) > 1:
            self.eq_modes = [m for m in self.eq_modes if m["id"] != mid]
            if self.eq_cur == mid:
                self.eq_cur = self.eq_modes[0]["id"]

    # -- virtual (surround) profiles
    def virtual(self, vid):
        return next((v for v in self.virtuals if v["id"] == vid), None)

    def add_virtual(self, name):
        nid = max([v["id"] for v in self.virtuals] + [0]) + 1
        v = dict(VIRTUAL_DEFAULT, id=nid, name=name, distance=[30] * 8, angle=list(VIRTUAL_DEFAULT["angle"]))
        self.virtuals.append(v)
        return nid

    def del_virtual(self, vid):
        if len(self.virtuals) > 1:
            self.virtuals = [v for v in self.virtuals if v["id"] != vid]
            if self.virt_cur == vid:
                self.virt_cur = self.virtuals[0]["id"]

    # -- persistence
    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        root = ET.Element("mh650", exportPath="", openExePath="")
        prof = ET.SubElement(root, "profile", id="0", name="PROFILE0", exe="")
        snd = ET.SubElement(prof, "sound", currModeId=str(self.eq_cur), bass=str(self.bass), treble=str(self.treble))
        for m in self.eq_modes:
            e = ET.SubElement(snd, "eq", id=str(m["id"]), name=m["name"])
            for a, v in zip(BAND_ATTRS, m["bands"]):
                e.set(a, str(v))
        sur = ET.SubElement(prof, "surround", currModeId=str(self.virt_cur))
        for v in self.virtuals:
            ET.SubElement(sur, "virtual", id=str(v["id"]), name=v["name"], VssEnable=str(v["VssEnable"]),
                          SmvssEnable=str(v["SmvssEnable"]),
                          distance=",".join(map(str, v["distance"])), angle=",".join(map(str, v["angle"])))
        tmp = self.path + ".tmp"
        ET.ElementTree(root).write(tmp, encoding="UTF-8", xml_declaration=True)
        os.replace(tmp, self.path)

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            root = ET.parse(self.path).getroot()
            prof = root.find("profile")
            snd = prof.find("sound")
            modes = []
            for e in snd.findall("eq"):
                modes.append(dict(id=int(e.get("id")), name=e.get("name"),
                                  bands=[int(e.get(a, "0")) for a in BAND_ATTRS]))
            if modes:
                self.eq_modes = modes
                self.eq_cur = int(snd.get("currModeId", modes[0]["id"]))
            self.bass = int(snd.get("bass", "0"))
            self.treble = int(snd.get("treble", "0"))
            sur = prof.find("surround")
            virts = []
            for v in sur.findall("virtual"):
                virts.append(dict(id=int(v.get("id")), name=v.get("name"), VssEnable=int(v.get("VssEnable", "0")),
                                  SmvssEnable=int(v.get("SmvssEnable", "1")),
                                  distance=[int(x) for x in v.get("distance").split(",")],
                                  angle=[int(x) for x in v.get("angle").split(",")]))
            if virts:
                self.virtuals = virts
                self.virt_cur = int(sur.get("currModeId", virts[0]["id"]))
        except (ET.ParseError, AttributeError, ValueError, TypeError):
            pass    # keep defaults on a corrupt file


# ----------------------------------------------------------------------------- PipeWire (user-level, no root)
_EXTRA_BIN_DIRS = ["/usr/bin", "/usr/local/bin", "/bin", os.path.expanduser("~/.local/bin"), "/usr/libexec/pipewire"]


def find_bin(name):
    p = shutil.which(name)
    if p:
        return p
    for d in _EXTRA_BIN_DIRS:
        c = os.path.join(d, name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def run(*args, timeout=8):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


FILTER_CHAIN_HEAD = """context.properties = { }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt args = { nice.level = -11 } flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
"""


def _q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _eq_module(cmeq, target_sink, sink_name="effect_input.mh650_eq"):
    """filter-chain config: one peaking biquad per USED slot, per channel. Gain = slot value * dB/unit."""
    nodes, links = [], []
    for ch in ("L", "R"):
        prev = None
        for slot in USED_SLOTS:
            n = f"eq{ch}{slot}"
            nodes.append(f'          {{ type = builtin name = {n} label = bq_peaking control = '
                         f'{{ "Freq" = {SLOT_FREQ[slot - 1]} "Q" = {ASSUMED_Q} "Gain" = {slot_gain_db(cmeq[slot - 1]):.3f} }} }}')
            if prev:
                links.append(f'          {{ output = "{prev}:Out" input = "{n}:In" }}')
            prev = n
    first = [f"eq{c}{USED_SLOTS[0]}" for c in "LR"]
    last = [f"eq{c}{USED_SLOTS[-1]}" for c in "LR"]
    return (f"""    {{ name = libpipewire-module-filter-chain
      args = {{
        node.description = "MH650 EQ"
        media.name = "MH650 EQ"
        filter.graph = {{
          nodes = [
{chr(10).join(nodes)}
          ]
          links = [
{chr(10).join(links)}
          ]
          inputs  = [ "{first[0]}:In" "{first[1]}:In" ]
          outputs = [ "{last[0]}:Out" "{last[1]}:Out" ]
        }}
        audio.channels = 2
        audio.position = [ FL FR ]
        capture.props = {{ node.name = {_q(sink_name)} media.class = Audio/Sink }}
        playback.props = {{ node.name = "effect_output.mh650_eq" node.passive = true target.object = {_q(target_sink)} }}
      }}
    }}
""")


def build_eq_conf(cmeq, target_sink, sink_name="effect_input.mh650_eq"):
    """Standalone config for a private `pipewire -c` child process."""
    return FILTER_CHAIN_HEAD + _eq_module(cmeq, target_sink, sink_name) + "]\n"


# ---- permanent EQ: a drop-in loaded by the user's own PipeWire at every login (no app needed)
DROPIN_TAG = "# mh650-gui target="


def dropin_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "pipewire", "pipewire.conf.d", "mh650-eq.conf")


def build_eq_dropin(cmeq, target_sink, sink_name="effect_input.mh650_eq"):
    return (f"{DROPIN_TAG}{target_sink}\n"
            "# Written by the MH650 app. Delete this file and restart PipeWire to remove the EQ.\n"
            "context.modules = [\n" + _eq_module(cmeq, target_sink, sink_name) + "]\n")


def dropin_installed():
    return os.path.isfile(dropin_path())


def dropin_target():
    try:
        with open(dropin_path()) as f:
            first = f.readline().strip()
        return first[len(DROPIN_TAG):] if first.startswith(DROPIN_TAG) else None
    except OSError:
        return None


def write_eq_dropin(cmeq, target_sink):
    p = dropin_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(build_eq_dropin(cmeq, target_sink))
    os.replace(tmp, p)


def remove_eq_dropin():
    try:
        os.remove(dropin_path())
        return True
    except FileNotFoundError:
        return False


def restart_pipewire():
    """Reload the user's PipeWire so it picks up (or drops) the drop-in. Audio blips for ~1 s."""
    sc = find_bin("systemctl")
    if not sc:
        raise OSError("`systemctl` not found: restart PipeWire yourself (or log out and in) to apply the permanent EQ")
    rc, _, err = run(sc, "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber", timeout=20)
    if rc != 0:
        raise OSError(f"could not restart PipeWire: {err.strip()[-300:]}")


def find_node_id(node_name):
    pd = find_bin("pw-dump")
    if not pd:
        return None
    rc, out, _ = run(pd)
    if rc != 0:
        return None
    try:
        for o in json.loads(out):
            props = (o.get("info") or {}).get("props") or {}
            if props.get("node.name") == node_name:
                return o["id"]
    except (ValueError, KeyError):
        return None
    return None


def wait_for_node(node_name, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if find_node_id(node_name) is not None:
            return True
        time.sleep(0.4)
    return False


class NodeProps:
    """Live-parameter access to a PipeWire node by name. The node id is cached so a slider drag
    doesn't run `pw-dump` (parses the whole graph) on every change."""
    node_name = ""
    cached_id = None

    def node_id(self, refresh=False):
        if refresh or self.cached_id is None:
            self.cached_id = find_node_id(self.node_name)
        return self.cached_id

    def props_argv(self, params):
        pc = find_bin("pw-cli")
        nid = self.node_id()
        if not (pc and nid is not None):
            return None
        return [pc, "s", str(nid), "Props", "{ params = [ " + " ".join(params) + " ] }"]

    def read_params(self):
        """Current Props params of the node as {name: float}, or None if they can't be read."""
        pd = find_bin("pw-dump")
        nid = self.node_id()
        if not pd or nid is None:
            return None
        rc, out, _ = run(pd, str(nid))
        if rc != 0:
            return None
        try:
            found = {}
            for o in json.loads(out):
                if o.get("id") != nid:
                    continue
                for pr in ((o.get("info") or {}).get("params") or {}).get("Props", []):
                    flat = pr.get("params") if isinstance(pr, dict) else None
                    if isinstance(flat, list):
                        for k, v in zip(flat[0::2], flat[1::2]):
                            if isinstance(v, (int, float)):
                                found[str(k)] = float(v)
            return found or None
        except (ValueError, KeyError, TypeError):
            return None

    def set_params(self, params, verify=True):
        """Live update. pw-cli exits 0 even on failure, so check its output and read the values back.
        Returns True only if the node really has the new values (or they can't be read back)."""
        for attempt in (0, 1):
            argv = self.props_argv(params)
            if not argv:
                return False
            rc, out, err = run(*argv)
            bad = rc != 0 or "error" in (out + err).lower()
            if not bad and verify:
                time.sleep(0.05)
                have = self.read_params()
                if have:
                    want = {params[i].strip('"'): float(params[i + 1]) for i in range(0, len(params), 2)}
                    bad = any(k in have and abs(have[k] - v) > 0.01 for k, v in want.items())
            if not bad:
                return True
            self.cached_id = None          # stale id: re-resolve once and retry
        return False


class PersistentEQ(NodeProps):
    """Handle to the EQ node living inside the user's own PipeWire (loaded from the drop-in)."""

    def __init__(self, node_name):
        self.node_name = node_name
        self.cached_id = None

    def running(self):
        return self.node_id(refresh=True) is not None


def eq_live_params(cmeq):
    out = []
    for ch in "LR":
        for slot in USED_SLOTS:
            out += [_q(f"eq{ch}{slot}:Gain"), f"{slot_gain_db(cmeq[slot - 1]):.3f}"]
    return out


def sofa_search():
    for pat in ("/usr/share/sofa/*.sofa", "/usr/share/hrtf/*.sofa", "/usr/local/share/sofa/*.sofa",
                os.path.expanduser("~/.local/share/sofa/*.sofa"), os.path.join(config_dir(), "*.sofa")):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def speaker_azimuth(angle):
    """Windows: 0=front, positive = clockwise (right). SOFA/PipeWire: counter-clockwise positive."""
    return float((-angle) % 360)


def speaker_radius(distance):
    """ASSUMED mapping: MasterPlus distance 30 (default) == 1.0 m."""
    return max(0.2, min(5.0, distance / 30.0))


def build_surround_conf(virtual, sofa_path, target_sink, rotate=0, sink_name="effect_input.mh650_surround"):
    nodes, links = [], []
    names = {}
    for i, sp in enumerate(SPEAKERS):
        if sp == "LFE":
            continue
        n = f"sp{sp}"
        names[sp] = n
        az = speaker_azimuth(wrap_angle(virtual["angle"][i] + rotate))
        nodes.append(f'          {{ type = sofa name = {n} label = spatializer config = {{ filename = {_q(sofa_path)} }} '
                     f'control = {{ "Azimuth" = {az:.1f} "Elevation" = 0.0 "Radius" = {speaker_radius(virtual["distance"][i]):.2f} }} }}')
    nodes.append('          { type = builtin name = lfe label = copy }')
    nodes.append('          { type = builtin name = mixL label = mixer control = { "Gain 8" = 0.5 } }')
    nodes.append('          { type = builtin name = mixR label = mixer control = { "Gain 8" = 0.5 } }')
    order = [s for s in SPEAKERS if s != "LFE"]
    for k, sp in enumerate(order, start=1):
        links.append(f'          {{ output = "{names[sp]}:Out L" input = "mixL:In {k}" }}')
        links.append(f'          {{ output = "{names[sp]}:Out R" input = "mixR:In {k}" }}')
    links.append('          { output = "lfe:Out" input = "mixL:In 8" }')
    links.append('          { output = "lfe:Out" input = "mixR:In 8" }')
    pos = ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"]
    rev = {v: k for k, v in SPEAKER_POS.items()}
    inputs = [(f'"{names[rev[p]]}:In"' if p != "LFE" else '"lfe:In"') for p in pos]
    return (FILTER_CHAIN_HEAD + f"""    {{ name = libpipewire-module-filter-chain
      args = {{
        node.description = "MH650 7.1 Virtual Surround"
        media.name = "MH650 7.1 Virtual Surround"
        filter.graph = {{
          nodes = [
{chr(10).join(nodes)}
          ]
          links = [
{chr(10).join(links)}
          ]
          inputs  = [ {' '.join(inputs)} ]
          outputs = [ "mixL:Out" "mixR:Out" ]
        }}
        capture.props = {{ node.name = {_q(sink_name)} media.class = Audio/Sink audio.channels = 8
            audio.position = [ {' '.join(pos)} ] }}
        playback.props = {{ node.name = "effect_output.mh650_surround" node.passive = true audio.channels = 2
            audio.position = [ FL FR ] target.object = {_q(target_sink)} }}
      }}
    }}
]
""")


def surround_live_params(virtual, rotate=0):
    out = []
    for i, sp in enumerate(SPEAKERS):
        if sp == "LFE":
            continue
        az = speaker_azimuth(wrap_angle(virtual["angle"][i] + rotate))
        out += [_q(f"sp{sp}:Azimuth"), f"{az:.1f}", _q(f"sp{sp}:Radius"), f"{speaker_radius(virtual['distance'][i]):.2f}"]
    return out


class FilterProcess(NodeProps):
    """A private PipeWire instance-less filter-chain: `pipewire -c <conf>` as a child of the app.
    No files in /etc, no PipeWire restart, no root. Killing the child removes the sink."""

    def __init__(self, tag, node_name):
        self.tag, self.node_name = tag, node_name
        self.proc = None
        self.cached_id = None
        self.conf = os.path.join(config_dir(), f"{tag}.conf")

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, text):
        self.stop()
        exe = find_bin("pipewire")
        if not exe:
            raise OSError("`pipewire` binary not found")
        os.makedirs(config_dir(), exist_ok=True)
        with open(self.conf, "w") as f:
            f.write(text)
        self.proc = subprocess.Popen([exe, "-c", self.conf], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        time.sleep(0.6)
        if self.proc.poll() is not None:
            err = (self.proc.stderr.read() or b"").decode(errors="replace")[-400:]
            self.proc = None
            raise OSError(f"filter-chain exited immediately: {err.strip()}")

    def stop(self):
        self.cached_id = None
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


# ----------------------------------------------------------------------------- PulseAudio-compatible controls (user-level)
def pactl_json(kind):
    pa = find_bin("pactl")
    if not pa:
        return []
    rc, out, _ = run(pa, "--format=json", "list", kind)
    if rc != 0:
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


def find_mh650(kind):
    """Return (name, volume_percent, muted) for the MH650 sink/source or None."""
    for d in pactl_json(kind):
        props = d.get("properties", {})
        name = d.get("name", "")
        if name.endswith(".monitor") or name.startswith("effect_"):
            continue
        blob = " ".join([name, d.get("description", ""), str(props.get("device.vendor.id", "")),
                         str(props.get("device.product.id", ""))]).lower()
        if "mh650" in blob or ("2516" in blob and "0129" in blob):
            vols = d.get("volume", {})
            pct = 0
            if vols:
                first = next(iter(vols.values()))
                m = re.match(r"(\d+)", str(first.get("value_percent", "0")))
                pct = int(m.group(1)) if m else 0
            return name, pct, bool(d.get("mute"))
    return None


def set_volume(kind, name, pct):
    pa = find_bin("pactl")
    if pa:
        run(pa, "set-sink-volume" if kind == "sinks" else "set-source-volume", name, f"{int(pct)}%")


def set_mute(kind, name, muted):
    pa = find_bin("pactl")
    if pa:
        run(pa, "set-sink-mute" if kind == "sinks" else "set-source-mute", name, "1" if muted else "0")


def set_default_sink(name):
    pa = find_bin("pactl")
    if pa:
        run(pa, "set-default-sink", name)
