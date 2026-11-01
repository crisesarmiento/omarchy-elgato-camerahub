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

import json
import re
import shutil
import subprocess
import sys
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
    return devices


def status_payload(
    usb_root: Path = USB_ROOT,
    video_root: Path = VIDEO_CLASS,
    table_path: Path = TABLE_PATH,
    dev_root: Path = Path("/dev"),
) -> dict:
    table = load_table(table_path)
    tool = find_tool()
    return {
        "ok": True,
        "backend": "v4l2-ctl" if tool else "none",
        "devices": scan_devices(table, tool, usb_root, video_root, dev_root),
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


def main(argv: list[str]) -> int:
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
    return fail("Unknown command. Use status or set.")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
