#!/usr/bin/env python3
"""Detect Elgato USB devices and drive standard V4L2 controls.

Reads sysfs and, when v4l2-ctl is on PATH, lists or sets a fixed set of
UVC controls. Does not open hidraw, write sysfs, or send vendor reports.

status prints one JSON object:

    {"ok": true, "backend": "v4l2-ctl"|"none", "devices": [ ... ]}

Each device has product, name, family, role, usb, video, controls, note.
usb is the sysfs device name (for example "1-2"), not a filesystem path.
video is "/dev/videoN" or "".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

VENDOR = "0fd9"
USB_ROOT = Path("/sys/bus/usb/devices")
VIDEO_CLASS = Path("/sys/class/video4linux")
TABLE_PATH = Path(__file__).resolve().parent / "devices.json"
TOOL_TIMEOUT = 5

NODE_RE = re.compile(r"^/dev/video[0-9]+$")
VALUE_RE = re.compile(r"^-?[0-9]+$")
PRODUCT_RE = re.compile(r"^[0-9a-f]{4}$")
VIDEO_NAME_RE = re.compile(r"^video[0-9]+$")
CTRL_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_]+)\s+0x[0-9a-fA-F]+\s+\((?P<type>[^)]+)\)\s*:(?P<rest>.*)$"
)
MENU_RE = re.compile(r"^\s+(?P<value>-?\d+):\s+(?P<label>.*?)\s*$")
VALUE_FIELD_RE = re.compile(r"value=(-?\d+)(?:\s+\(([^)]*)\))?")
RANGE_FIELD_RE = re.compile(r"\b(min|max|step)=(-?\d+)")
FLAGS_RE = re.compile(r"\bflags=(\S+)")

NOTE_CAPTURE = "Picture and EDID controls are not driven from Linux yet."
NOTE_NO_NODE = "USB device seen, no V4L2 node yet."
NOTE_NO_TOOL = "Install v4l-utils to change controls."
NOTE_READ_FAILED = "Could not read controls."
NOTE_WIFI = "On Wi-Fi. Connecting."
NOTE_WIFI_UP = "Connected over Wi-Fi. Video is on /dev/video9 (EpocCam)."
LOOPBACK = Path("/dev/video9")
VIDEO_UUID = bytes.fromhex("d8156a751e2ecd844c4afc60f8fd48b1")
STATE_DIR = Path.home() / ".local/state/elgato-camerahub"
STATE_PATH = STATE_DIR / "wifi.json"
WIFI_SERVICE = "_egstreamwrap._tcp"
IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")

# Order is the order status emits. Names are the v4l2-ctl control ids.
WHITELIST = (
    "exposure_auto",
    "exposure_absolute",
    "exposure_auto_priority",
    "gain",
    "white_balance_temperature_auto",
    "white_balance_temperature",
    "focus_auto",
    "focus_absolute",
    "zoom_absolute",
    "pan_absolute",
    "tilt_absolute",
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
    "backlight_compensation",
    "power_line_frequency",
    "hue",
)

LABELS = {
    "exposure_auto": "Auto exposure",
    "exposure_absolute": "Exposure",
    "exposure_auto_priority": "Exposure priority",
    "gain": "Gain",
    "white_balance_temperature_auto": "Auto white balance",
    "white_balance_temperature": "White balance",
    "focus_auto": "Auto focus",
    "focus_absolute": "Focus",
    "zoom_absolute": "Zoom",
    "pan_absolute": "Pan",
    "tilt_absolute": "Tilt",
    "brightness": "Brightness",
    "contrast": "Contrast",
    "saturation": "Saturation",
    "sharpness": "Sharpness",
    "backlight_compensation": "Backlight",
    "power_line_frequency": "Anti-flicker",
    "hue": "Hue",
}


class HelperError(Exception):
    """A failure that should become {"ok": false} with a short sentence."""


def emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def fail(message: str) -> int:
    emit({"ok": False, "error": message})
    return 1


def load_table(path: Path = TABLE_PATH) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HelperError("Could not read the device table.") from exc
    products = raw.get("products") if isinstance(raw, dict) else None
    vendor = str(raw.get("vendor", "")).lower() if isinstance(raw, dict) else ""
    if vendor != VENDOR or not isinstance(products, list) or not products:
        raise HelperError("Could not read the device table.")
    table: dict[str, dict] = {}
    for row in products:
        if not isinstance(row, dict):
            raise HelperError("Could not read the device table.")
        product = str(row.get("product", "")).lower()
        name = row.get("name")
        family = row.get("family")
        role = row.get("role")
        if (
            not PRODUCT_RE.fullmatch(product)
            or product in table
            or not isinstance(name, str)
            or not name
            or not isinstance(family, str)
            or not family
            or role not in ("webcam", "capture")
        ):
            raise HelperError("Could not read the device table.")
        table[product] = {
            "product": product,
            "name": name,
            "family": family,
            "role": role,
        }
    return table


def read_sysfs(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return ""


def video_nodes(video_root: Path, dev_root: Path = Path("/dev")) -> list[dict]:
    if not video_root.is_dir():
        return []
    nodes = []
    try:
        entries = list(video_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not VIDEO_NAME_RE.fullmatch(entry.name):
            continue
        dev = str(dev_root / entry.name)
        if not Path(dev).exists():
            continue
        try:
            real = entry.resolve(strict=True)
        except OSError:
            continue
        index_text = read_sysfs(entry / "index")
        index = int(index_text) if index_text.isdigit() else None
        nodes.append(
            {
                "dev": dev,
                "real": real,
                "index": index,
                "label": read_sysfs(entry / "name"),
                "number": int(entry.name[5:]),
            }
        )
    return nodes


def node_belongs(node_real: Path, usb_real: Path) -> bool:
    """True when the V4L2 node sits on this USB device, not a descendant device."""
    try:
        relative = node_real.relative_to(usb_real)
    except ValueError:
        return False
    if not relative.parts:
        return False
    head = relative.parts[0]
    return head == "video4linux" or head.startswith(usb_real.name + ":")


def pick_video(candidates: list[dict]) -> str:
    if not candidates:
        return ""

    def rank(node: dict) -> tuple:
        metadata = "Metadata" in node["label"]
        index = node["index"]
        return (
            1 if metadata else 0,
            0 if index == 0 else 1,
            index if index is not None else 10_000,
            node["number"],
        )

    return min(candidates, key=rank)["dev"]


def find_tool() -> str | None:
    return shutil.which("v4l2-ctl")


def run_tool(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TOOL_TIMEOUT,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def control_kind(key: str, ctype: str, minv: int | None, maxv: int | None, paren: str | None) -> str:
    auto = key.endswith("_auto") or key == "exposure_auto_priority"
    zero_one = ctype == "bool" or (minv == 0 and maxv == 1)
    if auto and zero_one and ctype in ("bool", "menu", "intmenu"):
        return "bool"
    # v4l2-ctl prints the current menu item as "value=N (Label)".
    if paren is not None or ctype in ("menu", "intmenu"):
        return "menu"
    return "int"


def parse_control_line(line: str) -> dict | None:
    match = CTRL_RE.match(line)
    if match is None:
        return None
    key = match.group("name")
    if key not in LABELS:
        return None
    rest = match.group("rest")
    ctype = match.group("type").strip()
    ranges: dict[str, int] = {}
    for field, raw in RANGE_FIELD_RE.findall(rest):
        ranges[field] = int(raw)
    value_match = VALUE_FIELD_RE.search(rest)
    value = int(value_match.group(1)) if value_match else None
    paren = value_match.group(2).strip() if value_match and value_match.group(2) else None
    if value_match and value_match.group(2) is not None and paren == "":
        paren = None
    flags_match = FLAGS_RE.search(rest)
    flags = flags_match.group(1) if flags_match else ""
    inactive = "inactive" in flags.split(",")
    kind = control_kind(key, ctype, ranges.get("min"), ranges.get("max"), paren)
    minv = ranges.get("min")
    maxv = ranges.get("max")
    step = ranges.get("step")
    if kind == "bool":
        minv = 0 if minv is None else minv
        maxv = 1 if maxv is None else maxv
        step = 1 if step is None else step
    elif kind == "menu" and step is None:
        step = 1
    options = []
    if paren and value is not None:
        options.append({"value": value, "label": paren})
    return {
        "key": key,
        "label": LABELS[key],
        "kind": kind,
        "min": minv,
        "max": maxv,
        "step": step,
        "value": value,
        "writable": value is not None and not inactive,
        "options": options,
    }


def parse_controls(text: str) -> list[dict]:
    found: dict[str, dict] = {}
    current: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        menu = MENU_RE.match(line)
        if menu and current:
            option = {
                "value": int(menu.group("value")),
                "label": menu.group("label").strip(),
            }
            options = found[current]["options"]
            if option["label"] and not any(item["value"] == option["value"] for item in options):
                options.append(option)
            continue
        parsed = parse_control_line(line)
        current = None
        if parsed is None:
            continue
        found[parsed["key"]] = parsed
        current = parsed["key"]
    ordered = []
    for key in WHITELIST:
        if key not in found:
            continue
        found[key]["options"].sort(key=lambda item: item["value"])
        ordered.append(found[key])
    return ordered


def read_controls(tool: str, node: str) -> tuple[list[dict], str]:
    proc = run_tool([tool, "-d", node, "--list-ctrls"])
    if proc is None or proc.returncode != 0:
        text = proc.stdout if proc is not None else ""
        controls = parse_controls(text or "")
        if controls:
            return controls, ""
        return [], NOTE_READ_FAILED
    return parse_controls(proc.stdout or ""), ""


def scan_devices(
    table: dict[str, dict],
    tool: str | None,
    usb_root: Path = USB_ROOT,
    video_root: Path = VIDEO_CLASS,
    dev_root: Path = Path("/dev"),
) -> list[dict]:
    try:
        usb_entries = sorted(usb_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise HelperError("Could not scan USB devices.") from exc
    nodes = video_nodes(video_root, dev_root)
    devices = []
    for entry in usb_entries:
        vendor = read_sysfs(entry / "idVendor").lower()
        if vendor != VENDOR:
            continue
        product = read_sysfs(entry / "idProduct").lower().zfill(4)
        known = table.get(product)
        if known is None:
            continue
        try:
            usb_real = entry.resolve(strict=True)
        except OSError:
            continue
        matched = [node for node in nodes if node_belongs(node["real"], usb_real)]
        video = pick_video(matched)
        device = {
            "product": known["product"],
            "name": known["name"],
            "family": known["family"],
            "role": known["role"],
            "usb": entry.name,
            "video": video,
            "controls": [],
            "note": "",
        }
        if known["role"] == "capture":
            device["note"] = NOTE_CAPTURE
        elif not video:
            device["note"] = NOTE_NO_NODE
        elif not tool:
            device["note"] = NOTE_NO_TOOL
        else:
            controls, note = read_controls(tool, video)
            device["controls"] = controls
            device["note"] = note
        devices.append(device)
    devices.sort(key=lambda item: (item["usb"], item["product"]))
    devices.extend(scan_wifi())
    return devices


def parse_txt(field: str) -> dict[str, str]:
    found = {}
    for item in re.findall(r'"([^"]*)"', field):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key and len(key) <= 8 and len(value) <= 80:
            found[key] = value
    return found


def scan_wifi() -> list[dict]:
    """EpocCam / Camera Hub iPhone app. Bonjour type observed in Camera Hub 2.3.0."""
    tool = shutil.which("avahi-browse")
    if not tool:
        return []
    try:
        proc = subprocess.run(
            [tool, "-rpt", WIFI_SERVICE],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0 and not proc.stdout:
        return []
    by_id: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        if not line.startswith("="):
            continue
        parts = line.split(";")
        if len(parts) < 9:
            continue
        proto, name, address, port_text = parts[2], parts[3], parts[7], parts[8]
        if proto != "IPv4" or IPV4_RE.fullmatch(address) is None:
            continue
        if not port_text.isdigit():
            continue
        txt = parse_txt(parts[9] if len(parts) > 9 else "")
        label = txt.get("I") or name.replace("\\032", " ").replace("-", " ")
        label = " ".join(label.split())
        if not label or len(label) > 80:
            continue
        device_id = txt.get("D") or name
        if not re.fullmatch(r"[A-Za-z0-9._ -]{1,64}", device_id):
            device_id = name[:64]
        # Prefer the first IPv4 hit. A second interface should not duplicate it.
        by_id.setdefault(
            device_id,
            {
                "product": "",
                "name": label,
                "family": "epoccam",
                "role": "wifi",
                "usb": "",
                "video": "",
                "host": address,
                "controls": [],
                "note": NOTE_WIFI,
            },
        )
    return list(by_id.values())


def login_token(nonce: int) -> int:
    """CAtomHandshakeServer::Translate. HMAC-MD5 of the decimal nonce, then bswap of the two halves xored."""
    text = str(nonce).encode("ascii")
    key = bytearray(64)
    key[0:4] = b"B_%5"
    key[5] = 0x66
    key[7:10] = b"oKt"
    inner = bytearray(byte ^ 0x36 for byte in key)
    inner[4] = 0x52
    inner[6] = 0x61
    outer = bytearray(byte ^ 0x5C for byte in key)
    outer[4] = 0x38
    outer[6] = 0x0B
    digest = hashlib.md5(bytes(outer) + hashlib.md5(bytes(inner) + text).digest()).digest()
    mixed = int.from_bytes(digest[0:8], "little") ^ int.from_bytes(digest[8:16], "little")
    return int.from_bytes(mixed.to_bytes(8, "little"), "big")


def atom(tag: int, payload: bytes) -> bytes:
    return struct.pack("<II", tag, len(payload)) + payload


def read_frame(sock: socket.socket) -> tuple[bytes, bytes]:
    header = b""
    while len(header) < 8:
        chunk = sock.recv(8 - len(header))
        if not chunk:
            raise HelperError("The phone closed the connection.")
        header += chunk
    magic = header[:4]
    length = struct.unpack_from("<I", header, 4)[0]
    if length > 1_000_000:
        raise HelperError("The phone sent a frame that is too large.")
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise HelperError("The phone closed the connection.")
        body += chunk
    return magic, body


def parse_hello(body: bytes) -> tuple[int, int]:
    if len(body) < 40:
        raise HelperError("The phone hello was too short.")
    version_tag, version_len = struct.unpack_from("<II", body, 0)
    version = struct.unpack_from("<I", body, 8)[0]
    nonce = struct.unpack_from("<q", body, 32)[0]
    if version_tag != 0x44000008 or version_len != 4:
        raise HelperError("The phone hello was not a login challenge.")
    return version, nonce


def login(sock: socket.socket) -> None:
    magic, body = read_frame(sock)
    if magic != struct.pack("<I", 0x48656C6F):
        raise HelperError("The phone did not send a login challenge.")
    version, nonce = parse_hello(body)
    parts = [
        atom(0x44000008, struct.pack("<I", version)),
        atom(0x4400000A, struct.pack("<Q", login_token(nonce))),
        atom(0x44000008, struct.pack("<I", 0)),
    ]
    sock.sendall(struct.pack("<II", 0x4C6F6769, sum(len(part) for part in parts)) + b"".join(parts))
    reply, _payload = read_frame(sock)
    if reply != b"cleW":
        raise HelperError("The phone rejected the login.")


def write_state(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(STATE_PATH)


def read_state() -> dict | None:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return payload


def ensure_holder() -> None:
    if read_state() is not None:
        return
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "hold"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def annotate_wifi(devices: list[dict]) -> None:
    if not any(device.get("role") == "wifi" for device in devices):
        return
    ensure_holder()
    state = read_state()
    live = bool(state and state.get("connected") and state.get("host"))
    for device in devices:
        if device.get("role") != "wifi":
            continue
        if live and device.get("host") == state.get("host"):
            device["note"] = NOTE_WIFI_UP
            device["connected"] = True
        else:
            device["connected"] = False


def hold() -> int:
    write_state({"pid": os.getpid(), "connected": False, "host": ""})
    if LOOPBACK.exists() and shutil.which("ffmpeg"):
        while True:
            try:
                stream_video()
            except Exception:
                time.sleep(2)
            time.sleep(2)
    while True:
        phones = scan_wifi()
        if not phones:
            time.sleep(2)
            continue
        phone = phones[0]
        host = phone["host"]
        try:
            port = phone_port(host)
            sock = socket.create_connection((host, port), timeout=4)
            sock.settimeout(8)
            login(sock)
            write_state({"pid": os.getpid(), "connected": True, "host": host})
            sock.settimeout(30)
            while True:
                try:
                    read_frame(sock)
                except socket.timeout:
                    continue
        except (OSError, HelperError, struct.error):
            write_state({"pid": os.getpid(), "connected": False, "host": host})
            time.sleep(2)


def phone_port(host: str) -> int:
    tool = shutil.which("avahi-browse")
    if not tool:
        raise HelperError("avahi-browse is not available.")
    proc = subprocess.run(
        [tool, "-rpt", WIFI_SERVICE],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )
    for line in proc.stdout.splitlines():
        parts = line.split(";")
        if len(parts) < 9 or not line.startswith("=") or parts[2] != "IPv4":
            continue
        if parts[7] == host and parts[8].isdigit():
            return int(parts[8])
    raise HelperError("The phone is no longer advertising.")


def status_payload(
    usb_root: Path = USB_ROOT,
    video_root: Path = VIDEO_CLASS,
    table_path: Path = TABLE_PATH,
    dev_root: Path = Path("/dev"),
) -> dict:
    table = load_table(table_path)
    tool = find_tool()
    devices = scan_devices(table, tool, usb_root, video_root, dev_root)
    annotate_wifi(devices)
    return {
        "ok": True,
        "backend": "v4l2-ctl" if tool else "none",
        "devices": devices,
    }


def webcam_for_node(payload: dict, node: str) -> dict | None:
    for device in payload.get("devices", []):
        if device.get("role") == "webcam" and device.get("video") == node:
            return device
    return None


def do_set(node: str, key: str, value: str) -> int:
    if NODE_RE.fullmatch(node) is None:
        return fail("That is not a video device node.")
    if key not in LABELS:
        return fail("That control is not supported.")
    if VALUE_RE.fullmatch(value) is None:
        return fail("The value must be a whole number.")
    tool = find_tool()
    if not tool:
        return fail(NOTE_NO_TOOL)
    try:
        current = status_payload()
        if webcam_for_node(current, node) is None:
            return fail("That video node is not an Elgato webcam.")
        proc = run_tool([tool, "-d", node, "--set-ctrl", f"{key}={value}"])
        if proc is None or proc.returncode != 0:
            return fail(f"Could not set {LABELS[key]} on {node}.")
        emit(status_payload())
    except HelperError as exc:
        return fail(str(exc))
    except Exception:
        return fail(f"Could not set {LABELS[key]} on {node}.")
    return 0


def wire(name: str) -> bytes:
    return name[::-1].encode("ascii")


def uuids_in(buf: bytes) -> list[bytes]:
    found: list[bytes] = []

    def walk(blob: bytes) -> None:
        off = 0
        while off + 8 <= len(blob):
            tag, length = struct.unpack_from("<II", blob, off)
            if length > len(blob) - off - 8:
                return
            payload = blob[off + 8 : off + 8 + length]
            if tag == 0x61554944 and len(payload) >= 24:
                inner_tag, inner_len = struct.unpack_from("<II", payload, 0)
                if inner_tag == 0x44000013 and inner_len == 16:
                    found.append(payload[8:24])
            else:
                walk(payload)
            off += 8 + length

    walk(buf)
    return found


def video_uuid(table: bytes) -> bytes | None:
    if VIDEO_UUID in table:
        return VIDEO_UUID
    for raw in uuids_in(table):
        if int.from_bytes(raw[:8], "little") & 0x70003 == 0x20000:
            return raw
    return None


def subscription(channel: bytes, dest: bytes) -> bytes:
    # One tTup, matching the phone's own hsDn. The hash is the unfinalized
    # MD5 context after those 32 bytes, which is what Camera Hub serializes.
    block = channel + dest + bytes(32)
    numbered = lambda value: atom(0x44000007, struct.pack("<I", value))
    raw_byte = lambda value: atom(0x44000003, bytes([value]))
    arya = atom(
        0x41726179,
        atom(0x44000008, struct.pack("<I", 64)) + b"".join(raw_byte(b) for b in block),
    )
    inner = atom(
        0x68486173,
        b"".join(
            numbered(value)
            for value in (32, 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476)
        )
        + arya,
    )
    pair = atom(0x44000013, channel) + atom(
        0x44000013, bytes.fromhex("01000000000000000000000000000000")
    )
    node = atom(
        0x74547570,
        atom(0x7454536B, atom(0x74534B79, pair) + atom(0x61554944, atom(0x44000013, dest)))
        + atom(0x7476456D, b""),
    )
    outer = atom(0x68486173, inner + numbered(0))
    return atom(0x6D445473, outer + atom(0x74547270, node))


def span_request(session: bytes, endpoint: bytes) -> bytes:
    message = uuid.uuid4().bytes
    span = uuid.uuid4().bytes
    body = atom(
        0x734D5271,
        atom(
            0x73774D67,
            atom(0x44000013, message)
            + atom(0x61554944, atom(0x44000013, session))
            + atom(0x61554944, atom(0x44000013, endpoint)),
        )
        + atom(0x68634944, atom(0x44000009, struct.pack("<Q", 0x60000)))
        + atom(0x61554944, atom(0x44000013, span)),
    )
    return (
        body
        + atom(0x44000008, struct.pack("<I", 1))
        + atom(0x4C586475, atom(0x4400000A, struct.pack("<Q", 33333)))
    )


def stop_holder() -> None:
    state = read_state()
    if not state:
        return
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    time.sleep(0.8)


def stream_video() -> int:
    if not LOOPBACK.exists():
        return fail(
            "Missing /dev/video9. Run: sudo modprobe v4l2loopback video_nr=9 "
            "card_label=EpocCam exclusive_caps=1 && sudo chmod 666 /dev/video9"
        )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return fail("ffmpeg is not available.")
    stop_holder()
    phones = scan_wifi()
    if not phones:
        return fail("The iPhone is not advertising.")
    host = phones[0]["host"]
    sock = socket.create_connection((host, phone_port(host)), timeout=4)
    sock.settimeout(8)
    login(sock)
    write_state({"pid": os.getpid(), "connected": True, "host": host})
    session = None
    channel = None
    decoder = subprocess.Popen(
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-flags",
            "low_delay",
            "-f",
            "h264",
            "-i",
            "pipe:0",
            "-an",
            "-pix_fmt",
            "bgra",
            "-s",
            "640x480",
            "-f",
            "v4l2",
            str(LOOPBACK),
        ],
        stdin=subprocess.PIPE,
    )
    try:
        while True:
            magic, body = read_frame(sock)
            if magic == wire("tBas"):
                sock.sendall(struct.pack("<II", 0x73537051, 0))
            elif magic == wire("tBaN"):
                sock.sendall(struct.pack("<II", 0x7442614E, len(body)) + body)
            elif magic == wire("tSyn") and len(body) >= 24:
                ticks = struct.unpack_from("<Q", body, 16)[0]
                payload = atom(0x4C587473, atom(0x4400000A, struct.pack("<Q", ticks + 50)))
                sock.sendall(struct.pack("<II", 0x7453796E, len(payload)) + payload)
            elif magic == wire("STyS") and session is None:
                ids = uuids_in(body)
                if len(ids) < 7:
                    return fail("The phone session did not list a stream.")
                session = ids[0]
                request = span_request(session, ids[6])
                sock.sendall(struct.pack("<II", 0x73537051, len(request)) + request)
            elif magic == wire("MtDS"):
                sock.sendall(struct.pack("<II", 0x4D74444B, 0))
            elif magic == wire("STbS"):
                channel = video_uuid(body)
                if channel is None:
                    return fail("The phone did not list a 640x480 stream.")
                sock.sendall(struct.pack("<II", 0x5354624B, 0))
            elif magic == wire("hsWU"):
                if channel is None:
                    return fail("The phone asked for a subscription before listing streams.")
                payload = subscription(channel, uuid.uuid4().bytes)
                sock.sendall(struct.pack("<II", 0x68735755, len(payload)) + payload)
                sock.settimeout(30)
            elif magic == wire("aDat") and decoder.stdin is not None:
                decoder.stdin.write(body)
                decoder.stdin.flush()
    except (OSError, HelperError, struct.error):
        return fail("The phone closed the video stream.")
    finally:
        if decoder.stdin is not None:
            decoder.stdin.close()
        decoder.terminate()
        sock.close()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "hold":
        return hold()
    if len(argv) == 2 and argv[1] == "video":
        try:
            return stream_video()
        except HelperError as exc:
            return fail(str(exc))
        except Exception:
            return fail("Could not start the iPhone camera.")
    if len(argv) == 2 and argv[1] == "status":
        try:
            emit(status_payload())
        except HelperError as exc:
            return fail(str(exc))
        except Exception:
            return fail("Could not read camera status.")
        return 0
    if len(argv) == 5 and argv[1] == "set":
        return do_set(argv[2], argv[3], argv[4])
    if len(argv) >= 2 and argv[1] == "status":
        return fail("Usage: camerahub.py status")
    if len(argv) >= 2 and argv[1] == "set":
        return fail("Usage: camerahub.py set /dev/videoN key value.")
    return fail("Unknown command. Use status, set, or video.")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
