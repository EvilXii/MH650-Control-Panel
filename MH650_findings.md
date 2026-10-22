# Cooler Master MH650: complete findings from MasterPlus 1.9.6

Everything found while reverse engineering the MasterPlus 1.9.6 installer for the MH650 headset, for the purpose of Linux interoperability.

**How to read the confidence labels**

| Label | Meaning |
|---|---|
| **Verified** | Confirmed on your real hardware by output you pasted (`lsusb`, HID descriptors). |
| **High** | Read directly from disassembly or from a shipped file. |
| **Medium** | Inferred from code structure, symbol names or strings. |
| **Unknown** | Not determined. |

Important: this is **static analysis (disassembly + symbols), not a full decompile.** The Ghidra headless run was killed in the sandbox, so I disassembled the MH650 functions with Capstone using function addresses taken from the shipped PDB. The lighting commands have not yet been confirmed on the headset.

---

## 1. TL;DR

- The MH650 shows up as **two USB devices**: `2516:0129` (audio + media-key HID) and `2516:0111` (vendor HID control channel). **Verified.**
- Lighting and button/charger events run over **`2516:0111`, interface 0, HID report ID `0xFF`, 63 data bytes (64 with ID), interrupt IN `0x82` / OUT `0x02`**. **Verified** descriptor; command bytes are **High** from code, **not yet tested live**.
- Decoded commands: enable / release lighting, static color, lights off, async notification `FF 10 06 VV`.
- **Not HID (Medium):** EQ, bass, virtual 7.1, volume and mic mute are driver / Windows-audio features. On Linux they need PipeWire/ALSA, not a USB protocol.
- Not decoded: animated lighting effects, the response format, what each notification value means, the 19-LED per-LED mode, firmware/battery queries.

---

## 2. The package

| Item | Value |
|---|---|
| File | `Installer_MasterPlus_v1_9_6.zip` containing `Installer_MasterPlus_v1.9.6.exe` (366,524,536 bytes, dated 2025-03-25) |
| Installer type | Inno Setup 6.1.0, Unicode, 32-bit PE |
| Contents | 2,321 entries; ships debug symbols (`.pdb`) for most DLLs |
| Extracted | Only MH650/MH670 files, `CMUOT.dll/.pdb`, `hidapi.dll`, config and driver INFs (about 26 MB used, of 350 MB) |

### Tools used
`innoextract 1.9`, `7-Zip`, `llvm-pdbutil` (symbol addresses), `pefile` + `Capstone` (disassembly), Ghidra 11.3.2 (headless attempt, killed by sandbox memory limits). The REA repository (`morluto/rea`) was cloned and read, but **REA's MCP workflow was not run**; the analysis was done with the tools above directly.

### MH650-related files in the installer

| File | Purpose |
|---|---|
| `app/MH650CenterWid.dll` | Qt UI: main page (Lighting, Sound, Surround tiles) |
| `app/MH650LightWid.dll` | Qt UI: lighting page |
| `app/MH650SoundWid.dll` | Qt UI: EQ, bass, mic page |
| `app/MH650SurroundWid.dll` | Qt UI: 7.1 virtual speaker page |
| `app/json/MH650/component.json` | Lighting component description |
| `app/image/MH650/MH650.png` | Product image |
| `app/CMUOT.dll` + `CMUOT.pdb` | **The device SDK containing the real protocol code** |
| `app/hidapi.dll` | HID access library used by the SDK |
| `app/CfgFile.dll` | Profile/XML config (`IMH650ProfileCfg`, `MH650XmlCfg`) |
| `app/MPService.exe` | Background service |
| `app/cm_drv_installer/CM_MHGS/Win10/...` | Windows audio driver package (`CMTAC.sys`, APO DLLs, INF, catalog) |

The four `*Wid.dll` files are **UI only**; they call into `CMUOT::CMUOT_MH650_Base`.

---

## 3. USB identity

### 3.1 From the driver INF (`COOLERMASTER_MHGS.Inf`, High)
Vendor ID `0x2516` (Cooler Master). The MH650/MH670 family driver binds the audio interface (`MI_00`) of these PIDs: `0113, 011D, 0129, 012B, 01C7, 01EA`. The INF header calls it the "MH670 Driver". The `CMUAC` (C-Media 108B) INF lists other PIDs, including vendor `0x0D8C`, and is not specific to this headset.

`cm_driver.ini` maps driver groups to installed INF names: `108B = oem116.inf`, `MH6XX = oem257.inf`.

### 3.2 From your machine (Verified)

```
Bus 003 Device 046: ID 2516:0129 Cooler Master Co., Ltd. MH650
Bus 003 Device 047: ID 2516:0111 Cooler Master Co., Ltd. MH650
```

| USB ID | hidraw | HID interface | Role |
|---|---|---|---|
| `2516:0129` | `/dev/hidraw2` | interface 3 | Consumer-control / media keys (see 3.3) |
| `2516:0111` | `/dev/hidraw3` | interface 0 | **Vendor control channel**, report ID `0xFF` (see 3.4) |

`0111` is not in the driver INF list, which fits it being a separate HID-only device. `0111` reports `bcdUSB 1.10`, `bInterfaceClass 3 (HID)`, endpoints `0x82 EP2 IN` and `0x02 EP2 OUT`.

### 3.3 HID descriptor of `0129` interface 3 (Verified)

```
05 0c 09 01 a1 01 15 00 25 01 09 e9 09 ea 75 01 95 02 81 02 09 e2 09 00 81 06
05 0b 09 20 95 01 81 06 05 0c 09 00 95 03 81 02 26 ff 00 09 00 75 08 95 03 81 02
09 00 95 04 91 02 c0
```

| Direction | Contents |
|---|---|
| Input (32 bits, no report ID) | Volume Up (`0x0C:0xE9`), Volume Down (`0xEA`), Mute (`0xE2`), Telephony Hook Switch (`0x0B:0x20`), 3 padding bits, then 3 bytes of undefined-usage data |
| Output (4 bytes, no report ID) | Undefined usage; purpose unknown (possibly LED/state) |

This is a standard media-key interface. It has **no report ID and only 4 bytes**, so the 64-byte lighting packets do **not** belong here. Writing them gives `EPIPE` (Broken pipe), which is what you saw (output and feature variants). The kernel already turns its Volume/Mute inputs into key events.

### 3.4 HID descriptor of `0111` interface 0 (Verified)

```
05 01 09 00 a1 01 85 ff 15 00 25 ff 19 01 29 08 95 3f 75 08 81 02 19 01 29 08 91 02 c0
```
Generic Desktop page; **Report ID `0xFF`; Input 63 bytes; Output 63 bytes** (`95 3f 75 08` = 63 x 8 bits). With the ID byte this is 64 bytes, matching the buffers in the SDK.

---

## 4. Where the protocol lives

`CMUOT.dll` (about 6 MB, built with MSVC, 32-bit) contains two layers for this device:

| Class | Role |
|---|---|
| `CMUOT::MH650` | **Device layer**: opens HID, builds and sends 64-byte reports, reads/matches responses, runs threads |
| `CMUOT::CMUOT_MH650` (derives `CMUOT_MH650_Base`) | **Public SDK**: `get*/set*` methods used by the UI; many just forward to the device layer or to audio objects |

An almost identical set exists for the **MH670** (`CMUOT_MH670`), so the MH670 is likely very similar.

### 4.1 Device layer facts (High unless noted)

- Constructor `MH650(const std::string& path)` (RVA `0x1C8FB0`) calls **`hid_open_path` twice** (two HID handles) and `hid_set_nonblocking`. It allocates **two 64-byte (`0x40`) buffers**, calls `checkMH650EffectMute`, and uses `Sleep` delays of 2000 ms and 500 ms during startup.
- `hidapi.dll` imports in `CMUOT.dll`: `hid_init, hid_enumerate, hid_open_path, hid_set_nonblocking, hid_write, hid_read, hid_read_timeout, hid_get_input_report, hid_close`.
- Writes go through a helper at RVA `0x2795D0` taking `(handle, buffer, 0x40)`. It takes a mutex and **spawns a worker thread** (`CreateThread`, entry `0x10277620`). I did not isolate the final `hid_write` call inside that worker. **Medium.**
- Reads use a helper at RVA `0x277530` in `MH650RxUsbThread` (RVA `0x1CA700`).
- **Request/response matching** (`MH650ReadUsb`, RVA `0x1CACA0`): sends the 64-byte request, records a pending entry keyed by bytes `[1]`, `[2]`, `[3]` of the request plus a counter that wraps at 9999 (`0x270F`), waits on an event for **1500 ms** (`0x5DC`), then matches the response to the pending entry. Exact response layout was **not decoded**.
- **Async notifications** (`MH650RxUsbThread`): when a received 64-byte report has `buf[1] == 0x10` and `buf[2] == 0x06`, it calls `NotifyMH650(type = 6, value = buf[3])`, which invokes the callback registered through `registerNotify`.
- Other worker functions: `MH650RxUsbAudioThread` (`0x1CAF90`), `MH650RotateThread` (`0x1C97B0`, animates virtual-speaker rotation), `MH650MsgManager` (`0x1C9F00`, a `GetMessageW` message loop).
- Log/error strings: `MH650 USB Audio fail`, `MH650 USB fail`, `MH650 is not ready!!`, `USB Rx/Tx error`, `notify MH650 7.1 button press!!`, `notify effect mh650 charger`.

---

## 5. HID command set (64-byte reports, byte 0 = report ID `0xFF`)

All packets are zero-padded to 64 bytes. Offsets are byte indexes in the 64-byte report.

| Function (SDK name, RVA) | Bytes | Evidence | Confidence |
|---|---|---|---|
| Enable software lighting: `setLightingInit` (`0x1CA4B0`) | `FF 10 03 00 03 01` | Stores dword `0x000310FF` at `[0..3]` and word `0x0103` at `[4..5]` | High |
| Release lighting: `setLightingUninit` (`0x1CA520`) | `FF 10 03 00 03 00` | Same dword, word `0x0003` | High |
| Lights off: `setLightingTurnOff` (`0x1CA5F0`) | `FF 10 04 00 00 00 00` | Dword `0x000410FF`, word `0`, byte `[6]=0` | High |
| Static color: `setLightingPortColor` (`0x1CA580`) | `FF 10 04 00 RR GG BB FF` | `[0]=FF`, word `0x0410` at `[1..2]`, `[4..6]` = color bytes, `[7]=FF` | High |
| Async notification (device to host) | `FF 10 06 VV ...` | RX thread compare on `[1]==0x10`, `[2]==0x06`, value `[3]` | High (layout), Unknown (meaning of `VV`) |

Reading of the structure (Medium): byte 1 = command class `0x10` (lighting), byte 2 = sub-command (`03` = lighting-control mode, `04` = color, `06` = event), then payload.

`setLightingInit` also makes a virtual call (`[eax+0x10]`) after sending, purpose unknown.

---

## 6. Lighting model

From `component.json` (High):

- Component **All** (`compId 0`, property `delegation`): lighting delegate over ports `0` and `1`.
- Component **MH650-SIDE** (`compId 1`, type `ArgbPort`): **19 LEDs** (`lightingSegments: [19]`, `defaultLedQty: 19`).
- Effects exposed by MasterPlus:

| ID | Name | Parameters |
|---|---|---|
| 0 | Static Color | `dim`, `rgb` |
| 1 | Breathing | `dim`, `rgb`, `speed` |
| 2 | Color Cycle | `dim`, `speed` |
| 3 | Wave | `direction`, `rgb`, `speed` |
| 4 | Star | `dim`, `rgb`, `speed` |
| 5 | Marquee | `dim`, `rgb`, `speed` |
| 6 | Turn Off | none |
| 7 | Customization | 19 per-LED `rgb` values (`portId 1`) |

The MH670 JSON is identical except for names and a device-type key (`"14"` for MH650, `"15"` for MH670).

Only the **single-color and off** packets are decoded. How Breathing, Wave, Star, Marquee and the 19-LED Customization mode are sent to the firmware was **not decoded**. (The MasterPlus MH650 SDK also exposes `getEffectParameter`, `setEffectParameter`, `getCurrEffect`, so effects are probably handled by firmware with parameters.) The Linux tools in this project animate effects in software instead.

---

## 7. SDK surface (`CMUOT::CMUOT_MH650`)

RVAs are in `CMUOT.dll` (image base `0x10000000`).

| Group | Methods | Notes |
|---|---|---|
| Lifecycle | `device_pre_init 0x30C0F0`, `device_post_init 0x30C280`, `device_pre_exit 0x30C410`, `device_post_exit 0x30C5B0`, `device_exit 0x30C750`, `exit_device 0x305B80` | |
| Notifications | `registerNotify 0x30AFF0`, `unRegisterNotify 0x30B1D0` | callback `void (*)(int type, void* data)` |
| Lighting | `setLightingInit 0x30B3C0`, `setLightingUninit 0x30B560`, `setLightingTurnOff 0x30B700`, `setLightingPortColor 0x2F6EC0`, `setEffectParameter 0x309F00`, `getEffectParameter 0x30A220`, `getCurrEffect 0x30A4D0` | the `CMUOT_MH650` versions delegate to the `MH650` device layer |
| Speaker | `setSpeakerVol 0x309110`, `getSpeakerVol 0x3092C0`, `setSpeakerMute 0x309490`, `getSpeakerMute 0x309640` | |
| Microphone | `setMicVol 0x309800`, `getMicVol 0x3099B0`, `setMicMute 0x309B90`, `getMicMute 0x309D40`, `setMicBoost 0x308D80`, `getMicBoost 0x308F40`, `getButtonMicMute 0x30A6F0` | |
| EQ | `setEQEnable 0x307B90`, `getEQEnable 0x307D50`, `setEQData 0x3082B0`, `getEQData 0x308480` | data struct `mh650_eq` |
| Environment | `setEMEnable 0x307F20`, `getEMEnable 0x3080E0`, `setEMEnviroment 0x308640`, `getEMEnviroment 0x308800`, `setEMRoomSize 0x3089E0`, `getEMRoomSize 0x308BA0` | enums `MH650_ENVIROMENT`, `MH650_ROOM_SIZE` |
| Virtual 7.1 | `setVirtualVSS 0x3060B0`, `getVirtualVSS 0x306270`, `setVirtualSMVSS 0x306430`, `getVirtualSMVSS 0x3065F0`, `setVirtualAngle 0x3067B0`, `getVirtualAngle 0x306990`, `setVirtualDistance 0x306B50`, `getVirtualDistance 0x306D30`, `setVirtualRotate 0x3070B0`, `getVirtualRotate 0x3072E0`, `setVirtualClockWise 0x3074A0`, `getVirtualClockWise 0x307650`, `setVirtualMovementOuter 0x307810`, `setVirtualMoveClockWise 0x3079D0`, `resetVirtualAngleDistance 0x306F00`, `setVirtualData 0x305D30`, `getVirtualData 0x305EF0`, `set7dot1buttonOn 0x30A8D0` | data struct `mh650_virtual` |
| Default audio device | `setDefaultAudio 0x30AA80`, `getDefaultAudiio 0x30AC50`, `getIsDisableAudio 0x30AE10` | |

Device-layer (`CMUOT::MH650`) extras: `setMH650DeviceEQData 0x1C9B40`, `getMH650DeviceEQData 0x1C9C90`, `setMH650DeviceVirtualData 0x1C9630`, `getMH650DeviceVirtualData 0x1C96A0`, `resetMH650DeviceVirtualAngleDistance 0x1C9720`, `setMH650DeviceVirtualMovementOuter 0x1C98B0`, `setMH650DeviceVirtualMoveClockWise 0x1C9960`, `getMH650DeviceDefaultAudio 0x1CA370`, `checkMH650EffectMute 0x1CA650`, `NotifyMH650 0x1C9D00`.

### 7.1 What the UI exposes (from the widget DLLs, High)
- **Sound page:** EQ mode list (add / rename / delete modes), EQ band values, **Bass value slider** (`slotBassValChanged`), mic level and mic mute; a label "Controlled by MH650 controller" is shown when hardware controls apply.
- **Surround page:** "7.1 SURROUND SOUND PROFILES", "7.1 VIRTUAL SPEAKER PLACEMENT", per-speaker angle and distance, near/far, rotation clockwise/counter-clockwise, profile add/rename/delete, enable VSS.
- Profiles are stored through `IMH650ProfileCfg` in `CfgFile.dll`.

---

## 8. What is probably not HID (Medium)

Evidence:
- `setEQData` (device layer, `0x1C9B40`) does not build a 64-byte report. It writes into a driver-side object (`[edi+0x214]` / `[edi+0x24C]`) with SSE moves.
- `CMUOT.dll` contains Windows Core Audio classes: `IAudioEndpointVolume`, `AUIAudioEndpointVolumeCallback`, `AVAudioEQ`, `AVSysAudioCb`, `AVSysAudioMonitor`, `CAudioWare`, plus `GetDefaultAudioEndpoint`.
- The driver package ships audio effect DLLs: **`CMEffectAPO_CMTAC.dll`** (a Windows Audio Processing Object) and `CMCplExt_CMTAC.dll`, with `CMTAC.sys` (C-Media class driver). EQ and virtual surround are almost certainly processed there on Windows.
- A scan of the MH650 classes found no `DeviceIoControl`, only HID and threading/synchronization APIs. (That scan covered the MH650 functions only, up to 8 KB each, so it is not exhaustive.)

Conclusion for Linux: EQ, bass, room effect and virtual 7.1 are best done with **PipeWire** (built-in biquad EQ and an HRIR convolver). Volume and mic mute are ordinary audio controls.

---

## 9. Linux status

| Area | Status |
|---|---|
| Audio playback / mic | Expected to work via the normal USB-audio driver; not explicitly tested here |
| Volume / mute buttons | Standard media keys on `0129` iface 3 (kernel handles it) |
| Lighting (color / off / release) | Command bytes decoded; **not yet confirmed on your headset** |
| Button / charger events | Report layout known (`FF 10 06 VV`); value meanings unknown |
| Bass / EQ / 7.1 | Implemented in the GUI through PipeWire, not tested on hardware |
| Animated hardware effects, per-LED colors | Not decoded |
| Battery level, firmware version | Not found in this analysis |

### Deliverables in this project
- `mh650_linux.py`: command-line tool (`list`, `descriptor`, `color`, `off`, `release`, `listen`), talks to `/dev/hidrawN` directly.
- `mh650_gui.py`: PyQt6 control panel (Lighting, Sound, EQ/Bass, 7.1 Surround, Events/Log).
- `mh650_protocol.md`: earlier short protocol notes (superseded by this file).

### Commands to test lighting on your headset
```bash
sudo python3 mh650_linux.py --pid 0x0111 color FF0000
sudo python3 mh650_linux.py --pid 0x0111 off
sudo python3 mh650_linux.py --pid 0x0111 release
sudo python3 mh650_linux.py --pid 0x0111 listen   # then press buttons / plug charger
```
udev rule for non-root access:
```
KERNEL=="hidraw*", ATTRS{idVendor}=="2516", MODE="0660", TAG+="uaccess"
```

---

## 10. Corrections made during the analysis

1. I first assumed plain HID, then briefly concluded the MH650 did not use `hid_*` at all. That was a scan error: the constructor calls `hid_open_path`. HID is correct.
2. I first assumed the control channel was `0129` interface 3. Your descriptor showed it is a media-key interface; the real channel is `0111` interface 0.
3. Early "EQ/7.1 are Windows DSP" was an inference. It is supported by the evidence in section 8 but not proven.

---

## 11. Open questions and next steps

1. Confirm the lighting packets on `0111` (does the color change? does it need the enable packet first?).
2. Capture a `listen` log for each button, the mic-mute switch, volume wheel and charger to map `FF 10 06 VV`.
3. Decode the response format from `MH650ReadUsb` and the pending-request matching, which would reveal query commands (battery, firmware, mute state).
4. Decode animated effects: follow `setEffectParameter` / `getEffectParameter` (`0x309F00`, `0x30A220`) and the Customization (19-LED) path.
5. Find the final `hid_write` call inside the worker thread started by the helper at `0x2795D0`.
6. For certainty, compare against a usbmon / Wireshark capture of MasterPlus running in a Windows VM with USB passthrough.
7. Re-run a full Ghidra decompile on a machine with more RAM for readable pseudocode.

---

## 12. Reproducing the analysis

```bash
innoextract -l Installer_MasterPlus_v1.9.6.exe                      # list contents
innoextract -I app/CMUOT.dll -I app/CMUOT.pdb -I app/hidapi.dll ... # extract named files
llvm-pdbutil dump --publics CMUOT.pdb                                # symbol -> section:offset
# RVA = .text VirtualAddress + offset; disassemble with Capstone (x86-32) using pefile for bytes
```
Function addresses in this document are RVAs; add image base `0x10000000` for the addresses Ghidra/IDA show.

*Note on scope: this is interoperability research on a product you own. Do not redistribute Cooler Master's binaries; this document contains only findings and addresses.*
