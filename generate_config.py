#!/usr/bin/env python3
"""Interactive config generator for led-backlight-mqtt.

Scans for local V4L2 video capture devices, collects WLED device IPs,
MQTT broker settings, and global defaults, then writes a validated
config.yaml ready for use with the led-backlight service.

No extra dependencies beyond the stdlib + pyyaml (already in requirements.txt).
cv2 is used for device probing when available but is not required.
"""

from __future__ import annotations

import glob
import ipaddress
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit(
        "pyyaml is required. Install it with: pip install pyyaml\n"
        "(or install the package: pip install -e .)"
    )

# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"

# Disable color if stdout is not a tty
if not sys.stdout.isatty():
    _BOLD = _DIM = _CYAN = _GREEN = _YELLOW = _RED = _RESET = ""


def _header(text: str) -> None:
    print(f"\n{_BOLD}{_CYAN}{'─' * 60}{_RESET}")
    print(f"{_BOLD}{_CYAN}  {text}{_RESET}")
    print(f"{_BOLD}{_CYAN}{'─' * 60}{_RESET}")


def _info(text: str) -> None:
    print(f"  {_DIM}{text}{_RESET}")


def _warn(text: str) -> None:
    print(f"  {_YELLOW}! {text}{_RESET}")


def _success(text: str) -> None:
    print(f"  {_GREEN}✓ {text}{_RESET}")


def _error(text: str) -> None:
    print(f"  {_RED}✗ {text}{_RESET}")


def _prompt(label: str, default: str | None = None, allow_empty: bool = False) -> str:
    """Show a prompt and return stripped user input.

    Loops until non-empty input is given (or allow_empty is True).
    """
    default_hint = f" [{default}]" if default is not None else ""
    while True:
        try:
            raw = input(f"  {_BOLD}{label}{default_hint}:{_RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if raw == "" and default is not None:
            return default
        if raw == "" and allow_empty:
            return ""
        if raw != "":
            return raw
        _warn("This field is required — please enter a value.")


def _prompt_int(label: str, default: int, min_val: int = 1, max_val: int = 2**31) -> int:
    """Prompt for an integer within [min_val, max_val]."""
    while True:
        raw = _prompt(label, str(default))
        try:
            val = int(raw)
        except ValueError:
            _warn(f"Must be an integer (got '{raw}').")
            continue
        if val < min_val or val > max_val:
            _warn(f"Must be between {min_val} and {max_val}.")
            continue
        return val


def _prompt_float(label: str, default: float, min_val: float = 0.0) -> float:
    """Prompt for a float >= min_val."""
    while True:
        raw = _prompt(label, str(default))
        try:
            val = float(raw)
        except ValueError:
            _warn(f"Must be a number (got '{raw}').")
            continue
        if val <= min_val:
            _warn(f"Must be greater than {min_val}.")
            continue
        return val


def _prompt_choice(label: str, choices: list[str], default: str | None = None) -> str:
    """Prompt to pick from a fixed list of choices."""
    choices_str = "/".join(
        f"{_BOLD}{c}{_RESET}" if c == default else c for c in choices
    )
    while True:
        raw = _prompt(f"{label} ({choices_str})", default).lower()
        if raw in choices:
            return raw
        _warn(f"Please enter one of: {', '.join(choices)}")


def _prompt_yes_no(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = _prompt(f"{label} ({hint})", allow_empty=True)
    if raw == "":
        return default
    return raw.lower() in ("y", "yes")


def _prompt_ip(label: str) -> str:
    """Prompt for a valid IPv4 address."""
    while True:
        raw = _prompt(label)
        try:
            ipaddress.IPv4Address(raw)
            return raw
        except ValueError:
            _warn(f"'{raw}' is not a valid IPv4 address (e.g. 192.168.1.50).")


def _prompt_id(label: str, default: str, existing: set[str]) -> str:
    """Prompt for a unique string ID (alphanumeric + underscores)."""
    id_re = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
    while True:
        raw = _prompt(label, default)
        if not id_re.match(raw):
            _warn("ID must start with a letter and contain only letters, digits, and underscores.")
            continue
        if raw in existing:
            _warn(f"ID '{raw}' is already in use. Please choose a different one.")
            continue
        return raw


# ---------------------------------------------------------------------------
# Device probing
# ---------------------------------------------------------------------------

def _probe_with_v4l2ctl(device: str) -> dict[str, Any] | None:
    """Try to get device info via v4l2-ctl. Returns a partial info dict or None."""
    if not shutil.which("v4l2-ctl"):
        return None
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--device", device, "--get-fmt-video"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return None
        info: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if "Width/Height" in line:
                m = re.search(r"(\d+)/(\d+)", line)
                if m:
                    info["width"] = int(m.group(1))
                    info["height"] = int(m.group(2))
            if "Pixel Format" in line:
                m = re.search(r"'([A-Z0-9]{4})'", line)
                if m:
                    info["fourcc"] = m.group(1)
        return info if info else None
    except Exception:
        return None


def _probe_with_cv2(device: str) -> dict[str, Any] | None:
    """Try to open the device via cv2 and read its properties."""
    try:
        import cv2  # type: ignore

        idx = int(device.replace("/dev/video", ""))
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        info = {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": cap.get(cv2.CAP_PROP_FPS),
        }
        cap.release()
        return info
    except Exception:
        return None


def _is_capture_device(device: str) -> bool:
    """Return True if the device node appears to be a real video capture device.

    V4L2 often creates companion nodes (e.g. /dev/video1 alongside /dev/video0)
    that are metadata or output nodes rather than capture sources.  We use
    v4l2-ctl --all to check for 'Video Capture' capability when available,
    falling back to a simple readable-file test.
    """
    if shutil.which("v4l2-ctl"):
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device, "--all"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            output = result.stdout + result.stderr
            # Accept device only if it advertises Video Capture capability
            if "Video Capture" in output:
                return True
            # If v4l2-ctl ran but no capture capability found, skip it
            if result.returncode == 0:
                return False
        except Exception:
            pass

    # Fallback: try to open the device file for reading
    try:
        with open(device, "rb"):
            pass
        return True
    except OSError:
        return False


def scan_video_devices() -> list[dict[str, Any]]:
    """Scan /dev/video* and return a list of detected capture devices."""
    paths = sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "0", p[-3:])))
    devices = []
    for path in paths:
        if not _is_capture_device(path):
            continue
        info: dict[str, Any] = {"device": path}
        # Try v4l2-ctl first, then cv2
        v4l_info = _probe_with_v4l2ctl(path)
        cv2_info = _probe_with_cv2(path)
        merged = {}
        if cv2_info:
            merged.update(cv2_info)
        if v4l_info:
            merged.update(v4l_info)  # v4l2-ctl takes precedence
        info.update(merged)
        devices.append(info)
    return devices


# ---------------------------------------------------------------------------
# Section collectors
# ---------------------------------------------------------------------------

def collect_global_defaults() -> dict[str, Any]:
    _header("Global Defaults")
    _info("These apply to all inputs/outputs unless overridden individually.")
    print()

    max_fps = _prompt_float("Max FPS per input", default=30.0)
    print()
    _info("Analysis resolution — frames are downscaled to this size before edge sampling.")
    _info("320×180 retains plenty of color detail and is fast on the Pi.")
    while True:
        res_raw = _prompt("Analysis resolution (WxH)", default="320x180")
        m = re.fullmatch(r"(\d+)[xX×](\d+)", res_raw)
        if m:
            analysis_resolution = [int(m.group(1)), int(m.group(2))]
            break
        _warn("Enter as WxH, e.g. 320x180")

    print()
    _info("Border strip depth as a fraction of the shorter frame dimension.")
    _info("0.08 = 8% — e.g. ~14 px on a 180 px tall analysis frame.")
    while True:
        bp_raw = _prompt("Border percentage (0.01–0.50)", default="0.08")
        try:
            border_pct = float(bp_raw)
            if 0.01 <= border_pct <= 0.5:
                break
        except ValueError:
            pass
        _warn("Must be a number between 0.01 and 0.50.")

    return {
        "max_fps": max_fps,
        "analysis_resolution": analysis_resolution,
        "border_pct": border_pct,
    }


def collect_mqtt() -> dict[str, Any]:
    _header("MQTT Broker")
    _info("Used for Home Assistant auto-discovery and control.")
    print()

    host = _prompt("Broker host / IP")
    port = _prompt_int("Port", default=1883, min_val=1, max_val=65535)
    print()
    username = _prompt("Username (leave blank to skip)", allow_empty=True)
    if username:
        _info("Tip: store your password in the MQTT_PASSWORD env var and leave the field blank.")
        _info("The config will then contain  password: ${MQTT_PASSWORD}")
        password_raw = _prompt("Password (leave blank to use ${MQTT_PASSWORD})", allow_empty=True)
        password = password_raw if password_raw else "${MQTT_PASSWORD}"
    else:
        username = None
        password = None

    print()
    discovery_prefix = _prompt("HA discovery prefix", default="homeassistant")
    client_id = _prompt("MQTT client ID", default="led-backlight")

    result: dict[str, Any] = {
        "host": host,
        "port": port,
        "discovery_prefix": discovery_prefix,
        "client_id": client_id,
    }
    if username is not None:
        result["username"] = username
    if password is not None:
        result["password"] = password
    return result


_QUADRANT_KEYS = ["top_left", "top_right", "bottom_left", "bottom_right"]


def collect_capture_devices(detected: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]]
]:
    """Walk through detected devices and prompt for configuration.

    Returns (capture_devices, inputs).
    """
    _header("Capture Devices")

    capture_devices: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    existing_device_ids: set[str] = set()
    existing_input_ids: set[str] = set()

    if not detected:
        _warn("No video devices found at /dev/video*.")
        _warn("You may be running on a non-Linux system or no capture cards are attached.")
        if not _prompt_yes_no("Add a capture device manually?", default=False):
            return [], []

        # Manual entry path — keep same flow as detected but skip device display
        detected = [{}]  # one empty stub so the loop runs once

    for idx, dev_info in enumerate(detected):
        path = dev_info.get("device", "")
        if path:
            print(f"\n  Device {idx + 1}: {_BOLD}{path}{_RESET}")
            if "width" in dev_info and "height" in dev_info:
                res = f"{dev_info['width']}×{dev_info['height']}"
                fps = f"  @{dev_info.get('fps', '?')} fps" if "fps" in dev_info else ""
                _info(f"Reported: {res}{fps}  fourcc={dev_info.get('fourcc', '?')}")
        else:
            print(f"\n  Manual capture device {idx + 1}")

        if not _prompt_yes_no("Configure this device?", default=True):
            continue

        print()
        # --- ID ---
        suggested_id = f"card_{idx}"
        dev_id = _prompt_id("Device ID", default=suggested_id, existing=existing_device_ids)
        existing_device_ids.add(dev_id)

        # --- device path ---
        if path:
            device_val: str | int = path
        else:
            device_val = _prompt("V4L2 device path or index", default="/dev/video0")
            if device_val.isdigit():
                device_val = int(device_val)

        # --- FOURCC ---
        suggested_fourcc = dev_info.get("fourcc", "MJPG")
        fourcc = _prompt("FOURCC (MJPG recommended; YUYV for raw)", default=suggested_fourcc).upper()

        # --- Resolution ---
        suggested_w = dev_info.get("width", 1920)
        suggested_h = dev_info.get("height", 1080)
        width = _prompt_int("Width", default=suggested_w, min_val=1, max_val=7680)
        height = _prompt_int("Height", default=suggested_h, min_val=1, max_val=4320)

        # --- FPS ---
        suggested_fps = float(dev_info.get("fps", 30.0))
        fps = _prompt_float("FPS", default=suggested_fps)

        # --- Mode ---
        print()
        _info("single   — the full frame is one video source")
        _info("quadrant — the frame contains 4 sub-images in a 2×2 grid")
        mode = _prompt_choice("Mode", choices=["single", "quadrant"], default="single")

        capture_devices.append({
            "id": dev_id,
            "device": device_val,
            "fourcc": fourcc,
            "width": width,
            "height": height,
            "fps": fps,
            "mode": mode,
        })

        # --- Auto-generate inputs ---
        print()
        if mode == "single":
            suggested_input_id = dev_id
            input_id = _prompt_id(
                "Input ID for this device",
                default=suggested_input_id,
                existing=existing_input_ids,
            )
            existing_input_ids.add(input_id)
            inputs.append({
                "id": input_id,
                "source_device": dev_id,
                "quadrant": None,
            })
            _success(f"Input '{input_id}' → {dev_id}")
        else:
            _info("Generating four quadrant inputs…")
            for q in _QUADRANT_KEYS:
                suggested_input_id = f"{dev_id}_{q}"
                input_id = _prompt_id(
                    f"Input ID for {q}",
                    default=suggested_input_id,
                    existing=existing_input_ids,
                )
                existing_input_ids.add(input_id)
                inputs.append({
                    "id": input_id,
                    "source_device": dev_id,
                    "quadrant": q,
                })
                _success(f"Input '{input_id}' → {dev_id} [{q}]")

        # Ask if there are more devices to add manually
        if idx == len(detected) - 1:
            if _prompt_yes_no("Add another capture device?", default=False):
                detected.append({})  # extend the loop

    return capture_devices, inputs


def collect_wled_outputs(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _header("WLED Outputs")
    _info("Enter each WLED device (LED strip controller) you want to configure.")

    if not inputs:
        _warn("No inputs defined — skipping WLED output configuration.")
        _warn("You can add wled_outputs manually to config.yaml later.")
        return []

    wled_outputs: list[dict[str, Any]] = []
    existing_ids: set[str] = set()

    while True:
        n = len(wled_outputs) + 1
        print(f"\n  WLED device {n}")

        host = _prompt_ip("IP address")

        # LED count
        print()
        _info("LED count — leave blank to auto-fetch from the WLED device at startup.")
        led_raw = _prompt("LED count (blank = auto)", allow_empty=True)
        led_count = int(led_raw) if led_raw.isdigit() and int(led_raw) > 0 else None

        brightness = _prompt_int("Default brightness (0–255)", default=200, min_val=0, max_val=255)

        # Input selection
        print()
        _info("Available inputs:")
        for i, inp in enumerate(inputs):
            print(f"    {i + 1}. {inp['id']}")
        while True:
            sel_raw = _prompt("Assign to input (number or ID)", default=inputs[0]["id"])
            # Accept numeric index
            if sel_raw.isdigit():
                sel_idx = int(sel_raw) - 1
                if 0 <= sel_idx < len(inputs):
                    input_id = inputs[sel_idx]["id"]
                    break
                _warn(f"Enter a number between 1 and {len(inputs)}.")
            else:
                if any(inp["id"] == sel_raw for inp in inputs):
                    input_id = sel_raw
                    break
                _warn(f"Unknown input ID '{sel_raw}'.")

        # Output ID
        suggested_id = f"tv{n}"
        out_id = _prompt_id("Output ID", default=suggested_id, existing=existing_ids)
        existing_ids.add(out_id)

        entry: dict[str, Any] = {
            "id": out_id,
            "host": host,
            "led_count": led_count,
            "brightness": brightness,
            "input": input_id,
            "border_pct": None,
        }
        wled_outputs.append(entry)
        _success(f"WLED output '{out_id}' ({host}) → input '{input_id}'")

        if not _prompt_yes_no("\nAdd another WLED device?", default=False):
            break

    return wled_outputs


# ---------------------------------------------------------------------------
# YAML serialisation helpers
# ---------------------------------------------------------------------------

class _NullRepresenter:
    """Ensure None → yaml null (tilde)."""


def _build_yaml_dict(
    globals_cfg: dict[str, Any],
    mqtt_cfg: dict[str, Any],
    capture_devices: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    wled_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "global": globals_cfg,
        "mqtt": mqtt_cfg,
        "capture_devices": capture_devices,
        "inputs": inputs,
        "wled_outputs": wled_outputs,
    }


def _represent_none(dumper: yaml.Dumper, _: None) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:null", "~")


def _dump_yaml(data: dict[str, Any]) -> str:
    dumper = yaml.Dumper
    dumper.add_representer(type(None), _represent_none)
    return yaml.dump(
        data,
        Dumper=dumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print(f"{_BOLD}led-backlight-mqtt — Config Generator{_RESET}")
    print("Generates a config.yaml for the led-backlight service.")
    print("Press Ctrl+C at any time to abort without saving.")

    # 1. Scan for video devices
    _header("Scanning for Video Devices")
    print("  Scanning /dev/video* …", end="", flush=True)
    detected = scan_video_devices()
    print(f" found {len(detected)} device(s).")

    if detected:
        print()
        for d in detected:
            path = d["device"]
            parts = [path]
            if "width" in d and "height" in d:
                parts.append(f"{d['width']}×{d['height']}")
            if "fps" in d:
                parts.append(f"@{d['fps']:.0f} fps")
            if "fourcc" in d:
                parts.append(f"fourcc={d['fourcc']}")
            _success("  ".join(parts))
    else:
        _warn("No /dev/video* devices found.")

    # 2 + 3. Capture devices + inputs
    capture_devices, inputs = collect_capture_devices(detected)

    # 4. WLED outputs
    wled_outputs = collect_wled_outputs(inputs)

    # 5. MQTT
    mqtt_cfg = collect_mqtt()

    # 6. Global defaults
    globals_cfg = collect_global_defaults()

    # 7. Write config.yaml
    _header("Writing config.yaml")
    out_path = Path("config.yaml")
    if out_path.exists():
        _warn(f"'{out_path}' already exists.")
        if not _prompt_yes_no("Overwrite?", default=False):
            print("  Aborted — config.yaml was not changed.")
            sys.exit(0)

    config_data = _build_yaml_dict(globals_cfg, mqtt_cfg, capture_devices, inputs, wled_outputs)
    yaml_text = _dump_yaml(config_data)

    # Prepend a header comment
    header_comment = (
        "# ─────────────────────────────────────────────────────────────────────────────\n"
        "# led-backlight-mqtt — generated configuration\n"
        "#\n"
        "# Generated by generate_config.py\n"
        "# Edit this file as needed, then start the service:\n"
        "#   led-backlight --config config.yaml\n"
        "#\n"
        "# Sensitive values can use ${ENV_VAR} substitution, e.g.:\n"
        "#   password: ${MQTT_PASSWORD}\n"
        "# ─────────────────────────────────────────────────────────────────────────────\n\n"
    )
    out_path.write_text(header_comment + yaml_text, encoding="utf-8")

    _success(f"Config written to {out_path.resolve()}")
    print()
    _info("Next steps:")
    _info("  1. Review config.yaml and adjust any values if needed.")
    _info("  2. Set any environment variables referenced in the config (e.g. MQTT_PASSWORD).")
    _info("  3. Run: led-backlight --config config.yaml")
    print()


if __name__ == "__main__":
    main()
