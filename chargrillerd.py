#!/usr/bin/env python3
"""
chargrillerd - Char-Griller BBQ Monitor & Web Dashboard

Supports Char-Griller Gravity 980 and Auto Akorn. Connects via BLE to
bootstrap the device onto WiFi, then monitors live probe/fan/door/alarm
status over TCP port 3333. Serves a real-time web dashboard.

Based on cg980app (https://github.com/cg980app/cg980app).

Requirements:
    pip install bleak

Usage:
    python3 chargrillerd.py                  # BLE connect (auto-detect)
    python3 chargrillerd.py --wifi           # Find device already on WiFi
    python3 chargrillerd.py --ip 192.168.x.x # Direct IP connection
    python3 chargrillerd.py --device akorn   # Force device type
    python3 chargrillerd.py --platform mac   # Force macOS notifications

Flow:
    1. Scans BLE for devices matching "Akorn-*", "Gravity980-*", or "BLEWIFI APP"
    2. Lets you pick one if multiple found
    3. Sends WiFi activate command [0x06] — uses stored credentials
    4. If stored credentials fail: sends auth handshake, scans for
       WiFi networks, provisions credentials via BLE, then activates
    5. Connects to device TCP port 3333, streams live status
    6. Serves web dashboard at http://localhost:<port>
    7. Logs session to CSV at ~/.cgriller/logs/
    8. Sends desktop notifications (macOS or Linux, auto-detected)
    9. Auto-reconnects (TCP retry 30s, then BLE re-activation) on connection loss
    10. Detects device power loss within 5 seconds (no-data timeout)

Protocol (verified via live device testing + packet captures):
    - BLE Service UUID: 0xAAAA (0000aaaa-0000-1000-8000-00805f9b34fb)
    - Write Characteristic: 0xBBB0 (Write Without Response)
    - Notify Characteristic: 0xBBB1 (Notify)
    - Auth command: [0x00, 0x19, 0x04, 0x00, 0xFF, 0x05, 0xFF, 0xFF]
      (session-start handshake; last two bytes are not validated by device)
    - WiFi scan: [0x00, 0x00, 0x02, 0x00, 0x01, 0x02]
    - Provision: [0x01, 0x00, len_lo, len_hi, BSSID(6), security, pass_len, password]
    - Activate WiFi: [0x06, 0x00, 0x00, 0x00]
    - Disconnect WiFi: [0x05, 0x00, 0x00, 0x01]
    - TCP port 3333 streams 16-byte status packets ~1/sec, no auth required

Status Packet (16 bytes, big-endian):
    [0-1]   Probe 1 current temp (uint16, °F; 1000 = disconnected)
    [2-3]   Probe 1 set temp (uint16, °F; 4096 = not configured)
    [4]     Alarm (0x10 = silent, 0x00 = firing)
    [5]     Reserved (always 0x00)
    [6-7]   Probe 2 current temp
    [8-9]   Probe 2 set temp
    [10-11] Probe 3 current temp
    [12-13] Probe 3 set temp
    [14]    Fan (bit 4: 0x10 = on)
    [15]    Flags (bit 0: 0x01 = door open, bit 4: 0x10 = turbo)

Known Limitations:
    - The BLE heartbeat (type 0x00) has a 4-byte header before the 16-byte
      status. Bytes 1-3 (observed: 0x19, 0x10, 0x00) are not fully understood.
    - Temperature unit is Fahrenheit. No Celsius mode has been discovered.
    - The signal indicator byte in WiFi info (offset 2) has unknown units.
"""

import argparse
import asyncio
import csv
import http.server
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path


# ==============================================================================
# CONFIGURATION — Edit these settings or leave as None for auto-detect.
#                 Command-line flags override these values.
# ==============================================================================

DEVICE = None               # "gravity", "akorn", or None for auto-detect
PLATFORM = None             # "mac", "linux", or None for auto-detect
ADAPTER = None              # BLE adapter e.g. "hci0", or None for default
DEVICE_IP = None            # Known device IP e.g. "192.168.86.113", or None
PORT = 8080                 # Web dashboard port
NOTIFICATIONS = "desktop"   # "desktop", "ntfy", "both", or "none"
NTFY_URL = None             # ntfy server e.g. "https://ntfy.sh"
NTFY_TOPIC = None           # ntfy topic e.g. "my-grill"
ALARM_SOUND = None          # Path to alarm audio file, or None for system default
MAX_TEMP = 500              # Graph Y-axis max (°F)
CHAMBER_ALARM_RANGE = 25    # Chamber alarms if temp is this many °F over or under target
DEBUG = False               # Print BLE packet data

# ==============================================================================


def parse_args():
    parser = argparse.ArgumentParser(
        description="chargrillerd - Char-Griller BBQ Monitor. "
                    "Supports Gravity 980 and Auto Akorn.",
        epilog="Examples:\n"
               "  %(prog)s                    Auto-detect device via BLE\n"
               "  %(prog)s --ip 192.168.0.151 Connect to known IP directly\n"
               "  %(prog)s --device akorn     Force device type\n"
               "  %(prog)s --platform mac     Force macOS notifications\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--device", choices=["auto", "gravity", "akorn"],
        default=DEVICE or "auto",
        help="Device type (default: auto-detect from BLE name)"
    )
    parser.add_argument(
        "--platform", choices=["auto", "mac", "linux"],
        default=PLATFORM or "auto",
        help="Platform for notifications (default: auto-detect)"
    )
    parser.add_argument(
        "--wifi", action="store_true",
        help="Skip BLE and scan local subnet for device on WiFi instead"
    )
    parser.add_argument(
        "--ip", metavar="ADDRESS", default=DEVICE_IP,
        help="Connect directly to a known device IP (skip all discovery)"
    )
    parser.add_argument(
        "--scan-timeout", type=float, default=8.0, metavar="SECS",
        help="BLE scan duration in seconds (default: 8)"
    )
    parser.add_argument(
        "--disconnect", action="store_true",
        help="Disconnect device from WiFi (send BLE disconnect command) and exit"
    )
    parser.add_argument(
        "--no-disconnect", action="store_true",
        help=argparse.SUPPRESS  # deprecated, kept for backwards compat
    )
    parser.add_argument(
        "--port", type=int, default=PORT, metavar="PORT",
        help="Web server port for status dashboard (default: 8080)"
    )
    parser.add_argument(
        "--adapter", metavar="HCI", default=ADAPTER,
        help="Bluetooth adapter to use (e.g. hci0, hci1). Default: system default"
    )
    parser.add_argument(
        "--debug", action="store_true", default=DEBUG,
        help="Print all BLE packet data for debugging"
    )
    parser.add_argument(
        "--resume", metavar="FILE",
        help="Resume a previous session by loading its CSV log (path to .csv file)"
    )
    parser.add_argument(
        "--max-temp", type=int, default=MAX_TEMP, metavar="DEGREES",
        help="Maximum temperature for graph Y-axis scaling (default: 500°F). "
             "Readings above this are clamped in the graph to prevent spikes from ruining scale."
    )
    parser.add_argument(
        "--alarm-sound", metavar="FILE",
        default=ALARM_SOUND,
        help="Audio file for alarms (default: system sound based on platform)"
    )
    parser.add_argument(
        "--p1-low", metavar="°F",
        help="Probe 1 low alarm threshold (integer, or 'set' to use device set temp)"
    )
    parser.add_argument(
        "--p1-high", metavar="°F",
        help="Probe 1 high alarm threshold (integer, or 'set' to use device set temp)"
    )
    parser.add_argument(
        "--p2-low", metavar="°F",
        help="Probe 2 low alarm threshold (integer, or 'set' to use device set temp)"
    )
    parser.add_argument(
        "--p2-high", metavar="°F",
        help="Probe 2 high alarm threshold (integer, or 'set' to use device set temp)"
    )
    parser.add_argument(
        "--p3-low", metavar="°F",
        help="Probe 3 low alarm threshold (integer, or 'set' to use device set temp)"
    )
    parser.add_argument(
        "--p3-high", metavar="°F",
        help="Probe 3 high alarm threshold (integer, or 'set' to use device set temp)"
    )
    return parser.parse_args()


ARGS = parse_args()

# --- Platform Detection ---

def detect_platform():
    if ARGS.platform != "auto":
        return ARGS.platform
    return "mac" if sys.platform == "darwin" else "linux"

_PLATFORM = detect_platform()

if ARGS.alarm_sound is None:
    if _PLATFORM == "mac":
        ARGS.alarm_sound = "/System/Library/Sounds/Basso.aiff"
    else:
        ARGS.alarm_sound = "/usr/share/sounds/Yaru/stereo/dialog-error.oga"

# --- Device Profiles ---

DEVICE_PROFILES = {
    "gravity": {
        "name": "Gravity 980",
        "ble_match": "Gravity",
        "show_door": True,
        "show_fan": True,
        "cli_title": "GRAVITY 980 STATUS",
        "web_title": "Gravity 980 Monitor",
    },
    "akorn": {
        "name": "Auto Akorn",
        "ble_match": "Akorn",
        "show_door": False,
        "show_fan": True,
        "cli_title": "AUTO AKORN STATUS",
        "web_title": "Auto Akorn Monitor",
    },
}

# Will be set after BLE scan or from --device flag
DEVICE_PROFILE = DEVICE_PROFILES.get(ARGS.device) if ARGS.device != "auto" else None

def detect_device_from_name(ble_name: str):
    """Auto-detect device profile from BLE advertised name."""
    global DEVICE_PROFILE
    if DEVICE_PROFILE:
        return  # already set by --device flag
    if "Akorn" in ble_name:
        DEVICE_PROFILE = DEVICE_PROFILES["akorn"]
    elif "Gravity" in ble_name:
        DEVICE_PROFILE = DEVICE_PROFILES["gravity"]
    else:
        DEVICE_PROFILE = DEVICE_PROFILES["akorn"]  # default fallback

def get_profile():
    """Get current device profile, defaulting to akorn."""
    return DEVICE_PROFILE or DEVICE_PROFILES["akorn"]

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Error: 'bleak' package required. Install with: pip install bleak")
    sys.exit(1)


# --- BLE Protocol Constants ---

SERVICE_UUID = "0000aaaa-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000bbb0-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000bbb1-0000-1000-8000-00805f9b34fb"

CMD_ACTIVATE_WIFI = bytes([0x06, 0x00, 0x00, 0x00])
CMD_DISCONNECT_WIFI = bytes([0x05, 0x00, 0x00, 0x00])

MSG_TYPE_STATUS = 0x00
MSG_TYPE_WIFI_INFO = 0x07

# --- TCP Protocol Constants ---

TCP_PORT = 3333
STATUS_PACKET_SIZE = 20

# --- Sentinel Values ---

TEMP_PROBE_DISCONNECTED = 1000  # 0x03E8 in current temp = probe not connected
TEMP_NOT_CONFIGURED = 4096     # 0x1000 in set temp = no target set
ALARM_SILENT = 0x10
ALARM_FIRING = 0x00
FAN_ON_BIT = 0x10              # Byte 14, bit 4
DOOR_OPEN_BIT = 0x01           # Byte 15, bit 0
TURBO_BIT = 0x10               # Byte 15, bit 4

# --- Persistence & Notifications ---

CACHE_DIR = Path.home() / ".cgriller"
CACHE_FILE = CACHE_DIR / "device_cache.json"
LOG_DIR = CACHE_DIR / "logs"
SESSIONS_FILE = CACHE_DIR / "sessions.json"


def ensure_cache_dir():
    CACHE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)


def load_sessions_meta() -> dict:
    """Load session metadata (names, notes) from sessions.json."""
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_sessions_meta(data: dict):
    SESSIONS_FILE.write_text(json.dumps(data, indent=2))


def list_sessions() -> list[dict]:
    """List all session CSV files with metadata."""
    meta = load_sessions_meta()
    sessions = []
    for csv_file in sorted(LOG_DIR.glob("session_*.csv"), reverse=True):
        name = csv_file.stem
        stat = csv_file.stat()
        info = meta.get(name, {})
        sessions.append({
            "file": name,
            "path": str(csv_file),
            "label": info.get("label", ""),
            "size": stat.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
        })
    return sessions


def rename_session(file_stem: str, label: str):
    """Set a friendly label for a session."""
    meta = load_sessions_meta()
    if file_stem not in meta:
        meta[file_stem] = {}
    meta[file_stem]["label"] = label
    save_sessions_meta(meta)


def save_device_cache(ble_address: str, ip: str):
    ensure_cache_dir()
    data = {"ble_address": ble_address, "ip": ip, "timestamp": time.time()}
    CACHE_FILE.write_text(json.dumps(data))


def load_device_cache() -> dict | None:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def send_notification(title: str, message: str, critical: bool = False):
    """
    Send notifications based on NOTIFICATIONS setting.
    Supports desktop (mac/linux auto-detected), ntfy, both, or none.
    """
    mode = NOTIFICATIONS or "desktop"
    if mode in ("desktop", "both"):
        if _PLATFORM == "mac":
            _notify_mac(title, message, critical)
        else:
            _notify_linux(title, message, critical)
    if mode in ("ntfy", "both"):
        _notify_ntfy(title, message, critical)


def _notify_mac(title: str, message: str, critical: bool):
    if critical:
        def _combined():
            flag = Path("/tmp/.cgriller_alert_active")
            flag.touch()
            sound_thread = threading.Thread(target=lambda: _mac_sound_loop(flag), daemon=True)
            sound_thread.start()
            subprocess.run([
                "osascript", "-e",
                f'display alert "{title}" message "{message}" as critical'
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            flag.unlink(missing_ok=True)

        threading.Thread(target=_combined, daemon=True).start()
    else:
        try:
            subprocess.Popen([
                "osascript", "-e",
                f'display notification "{message}" with title "{title}" sound name "Glass"'
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def _mac_sound_loop(flag):
    while flag.exists():
        subprocess.run(["afplay", "-v", "2", ARGS.alarm_sound],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _notify_linux(title: str, message: str, critical: bool):
    if critical:
        def _combined():
            flag = Path("/tmp/.cgriller_alert_active")
            flag.touch()
            try:
                subprocess.Popen([
                    "notify-send", "--urgency=critical", title, message
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
            while flag.exists():
                try:
                    subprocess.run(
                        ["paplay", ARGS.alarm_sound],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=10
                    )
                except (OSError, subprocess.TimeoutExpired):
                    time.sleep(1)

        threading.Thread(target=_combined, daemon=True).start()
    else:
        try:
            subprocess.Popen([
                "notify-send", title, message
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def _notify_ntfy(title: str, message: str, critical: bool):
    """Send notification via ntfy (https://ntfy.sh or self-hosted)."""
    url = NTFY_URL
    topic = NTFY_TOPIC
    if not url or not topic:
        return
    try:
        full_url = f"{url.rstrip('/')}/{topic}"
        req = urllib.request.Request(full_url, data=message.encode())
        req.add_header("Title", title)
        if critical:
            req.add_header("Priority", "urgent")
            req.add_header("Tags", "fire")
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.URLError, OSError):
        pass


@dataclass
class ProbeReading:
    """A single probe's current and target temperature."""
    current: int | None  # None if probe disconnected
    set_temp: int | None  # None if not configured

    @property
    def connected(self) -> bool:
        return self.current is not None

    @property
    def has_target(self) -> bool:
        return self.set_temp is not None


@dataclass
class DeviceStatus:
    """Parsed 20-byte device status."""
    probe1: ProbeReading
    probe2: ProbeReading
    probe3: ProbeReading
    alarm_firing: bool
    fan_on: bool
    fan_turbo: bool
    door_open: bool
    fan_auto: bool
    fan_speed: int  # 0-100%

    @classmethod
    def from_bytes(cls, data: bytes) -> "DeviceStatus":
        """Parse a 20-byte status packet into structured data."""
        if len(data) < 16:
            raise ValueError(f"Expected at least 16 bytes, got {len(data)}")

        p1_cur = struct.unpack(">H", data[0:2])[0]
        p1_set = struct.unpack(">H", data[2:4])[0]
        alarm = data[4]
        # data[5] is reserved (always 0)
        p2_cur = struct.unpack(">H", data[6:8])[0]
        p2_set = struct.unpack(">H", data[8:10])[0]
        p3_cur = struct.unpack(">H", data[10:12])[0]
        p3_set = struct.unpack(">H", data[12:14])[0]
        fan_byte = data[14]
        status_byte = data[15]

        # Extended bytes (16-19) — fan mode/speed
        fan_auto = True
        fan_speed = 0
        if len(data) >= 18:
            fan_auto = (data[16] == 0x10)
            fan_speed = data[17]

        return cls(
            probe1=ProbeReading(
                current=p1_cur if p1_cur != TEMP_PROBE_DISCONNECTED else None,
                set_temp=p1_set if p1_set != TEMP_NOT_CONFIGURED else None,
            ),
            probe2=ProbeReading(
                current=p2_cur if p2_cur != TEMP_PROBE_DISCONNECTED else None,
                set_temp=p2_set if p2_set != TEMP_NOT_CONFIGURED else None,
            ),
            probe3=ProbeReading(
                current=p3_cur if p3_cur != TEMP_PROBE_DISCONNECTED else None,
                set_temp=p3_set if p3_set != TEMP_NOT_CONFIGURED else None,
            ),
            alarm_firing=(alarm == ALARM_FIRING),
            fan_on=bool(fan_byte & FAN_ON_BIT),
            fan_turbo=bool(status_byte & TURBO_BIT),
            door_open=bool(status_byte & DOOR_OPEN_BIT),
            fan_auto=fan_auto,
            fan_speed=fan_speed,
        )

    def format_display(self) -> str:
        """Format status for terminal display."""
        W = 42  # inner width between ║ bars
        lines = []
        lines.append("╔" + "═" * W + "╗")
        lines.append("║" + get_profile()["cli_title"].center(W) + "║")
        lines.append("╠" + "═" * W + "╣")

        for name, probe in [("Probe 1 (Chamber)", self.probe1), ("Probe 2 (Food)   ", self.probe2), ("Probe 3 (Food)   ", self.probe3)]:
            if probe.connected:
                target = f" → {probe.set_temp}°F" if probe.has_target else ""
                content = f"  {name}: {probe.current:>4}°F{target}"
            else:
                content = f"  {name}: -- disconnected --"
            lines.append("║" + content.ljust(W) + "║")

        lines.append("╠" + "═" * W + "╣")
        profile = get_profile()
        if profile["show_fan"]:
            if self.fan_auto:
                fan_str = "Auto"
            elif self.fan_speed == 0:
                fan_str = "Off"
            else:
                fan_str = f"{self.fan_speed}%"
            content = f"  Fan: {fan_str}"
            if profile["show_door"]:
                content += f"  |  Door: {'OPEN' if self.door_open else 'CLOSED'}"
            lines.append("║" + content.ljust(W) + "║")

        if self.alarm_firing:
            content = "  *** ALARM FIRING ***"
            lines.append("║" + content.ljust(W) + "║")

        lines.append("╚" + "═" * W + "╝")
        return "\n".join(lines)


@dataclass
class WifiInfo:
    """Parsed WiFi connection info from BLE notification."""
    ssid: str
    bssid: str
    ip: str
    netmask: str
    gateway: str

    @classmethod
    def from_status_notification(cls, data: bytes) -> "WifiInfo":
        """
        Parse a type-0x00/subtype-0x20 BLE notification (Akorn format).
        Format: [0x00][0x20][len_lo][len_hi][0x00][ssid_len][ssid...][bssid x6][ip x4][mask x4][gw x4]
        """
        ssid_len = data[5]
        offset = 6
        ssid = data[offset:offset + ssid_len].decode("utf-8", errors="replace")
        offset += ssid_len
        bssid = ":".join(f"{b:02X}" for b in data[offset:offset + 6])
        offset += 6
        ip = ".".join(str(b) for b in data[offset:offset + 4])
        offset += 4
        netmask = ".".join(str(b) for b in data[offset:offset + 4])
        offset += 4
        gateway = ".".join(str(b) for b in data[offset:offset + 4])
        return cls(ssid=ssid, bssid=bssid, ip=ip, netmask=netmask, gateway=gateway)

    @classmethod
    def from_notification(cls, data: bytes) -> "WifiInfo":
        """
        Parse a type-0x07 BLE notification payload (Gravity 980 format).
        Format: [type=0x07][status][signal][reserved x2][ssid_len][ssid...][bssid x6][ip x4][mask x4][gw x4]
        """
        if data[0] != MSG_TYPE_WIFI_INFO:
            raise ValueError(f"Expected message type 0x07, got 0x{data[0]:02x}")

        ssid_len = data[5]
        offset = 6
        ssid = data[offset:offset + ssid_len].decode("utf-8", errors="replace")
        offset += ssid_len

        bssid = ":".join(f"{b:02X}" for b in data[offset:offset + 6])
        offset += 6

        ip = ".".join(str(b) for b in data[offset:offset + 4])
        offset += 4

        netmask = ".".join(str(b) for b in data[offset:offset + 4])
        offset += 4

        gateway = ".".join(str(b) for b in data[offset:offset + 4])

        return cls(ssid=ssid, bssid=bssid, ip=ip, netmask=netmask, gateway=gateway)


# --- BLE Functions ---

async def scan_for_devices(timeout: float = 8.0) -> list[tuple[str, str, int]]:
    """
    Scan for Char-Griller BLE devices (Akorn and/or Gravity).
    Returns list of (address, name, rssi, adapter) tuples.
    """
    adapter = ARGS.adapter
    if adapter:
        print(f"Scanning for Char-Griller devices ({timeout}s, adapter: {adapter})...")
    else:
        print(f"Scanning for Char-Griller devices ({timeout}s)...")

    kwargs = {"timeout": timeout, "return_adv": True}
    if adapter:
        kwargs["adapter"] = adapter

    devices = await BleakScanner.discover(**kwargs)

    found_devices = []
    for addr, (device, adv_data) in devices.items():
        if device.name and ("Akorn" in device.name or "Gravity" in device.name or "BLEWIFI" in device.name):
            found_devices.append((device.address, device.name, adv_data.rssi, adapter))

    return found_devices


async def ble_activate_wifi(device_address: str, adapter: str | None = None) -> WifiInfo | None:
    """
    Connect to device via BLE, send WiFi activate command, return connection info.

    The command [0x06, 0x00, 0x00, 0x00] tells the device to connect to its
    previously stored WiFi credentials and report back its IP address.

    NOTE: If the device has never been provisioned with WiFi credentials, this
    command may fail or timeout. WiFi credential provisioning format is not
    documented in the captured logs.
    """
    wifi_info = None
    event = asyncio.Event()

    def notification_handler(sender, data: bytes):
        nonlocal wifi_info
        if len(data) > 0 and data[0] == MSG_TYPE_WIFI_INFO:
            wifi_info = WifiInfo.from_notification(data)
            event.set()

    print(f"Connecting to {device_address} via BLE...")
    try:
        async with BleakClient(device_address, timeout=15.0, adapter=adapter) as client:
            print("Connected. Subscribing to notifications...")
            await client.start_notify(NOTIFY_CHAR_UUID, notification_handler)

            print("Sending WiFi activate command [06 00 00 00]...")
            await client.write_gatt_char(WRITE_CHAR_UUID, CMD_ACTIVATE_WIFI, response=False)

            # Wait up to 10 seconds for WiFi info response
            try:
                await asyncio.wait_for(event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                print("Timeout waiting for WiFi info response.")
                print("The device may not have stored WiFi credentials.")

            await client.stop_notify(NOTIFY_CHAR_UUID)
    except Exception as e:
        print(f"BLE connection error: {e}")

    return wifi_info


async def ble_disconnect_wifi(device_address: str, adapter: str | None = None) -> bool:
    """
    Connect to device via BLE and send the WiFi disconnect command [0x05, 0x00, 0x00, 0x00].
    The device responds with a notification that crashes bleak's service cache — this is
    expected and harmless. We verify success by checking TCP becomes unreachable.
    """
    cache = load_device_cache()
    ip = cache["ip"] if cache else None

    if not ip:
        print("No cached device IP. Cannot verify disconnect.")
        return False

    def handler(sender, data: bytes):
        pass

    try:
        async with BleakClient(device_address, timeout=10.0, adapter=adapter) as client:
            await client.start_notify(NOTIFY_CHAR_UUID, handler)
            await asyncio.sleep(1)
            await client.write_gatt_char(WRITE_CHAR_UUID, bytes([0x05, 0x00, 0x00, 0x00]), response=False)
            await asyncio.sleep(2)
    except Exception:
        pass  # Expected — device response crashes bleak service cache

    # Verify WiFi is actually down
    await asyncio.sleep(3)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((ip, TCP_PORT))
        s.recv(16)
        s.close()
        return False
    except:
        s.close()
        return True


@dataclass
class ScannedNetwork:
    """A WiFi network found during BLE-initiated scan."""
    ssid: str
    bssid: bytes
    security: int
    signal: int
    flags: int

    @property
    def bssid_str(self) -> str:
        return ":".join(f"{b:02X}" for b in self.bssid)

    @property
    def security_str(self) -> str:
        return {0: "Open", 1: "WPA", 2: "WPA2", 3: "WPA/WPA2", 4: "WPA3"}.get(self.security, f"Type {self.security}")


async def ble_authenticate(client):
    """
    Send session-start handshake command.
    The last two bytes are nominally a PIN but the device does not validate them.
    Command format: [0x00, 0x19, 0x04, 0x00, 0xFF, 0x05, 0xFF, 0xFF]
    """
    cmd = bytes([0x00, 0x19, 0x04, 0x00, 0xFF, 0x05, 0xFF, 0xFF])
    if ARGS.debug:
        print(f"  [BLE TX] {cmd.hex(' ')}")
    await client.write_gatt_char(WRITE_CHAR_UUID, cmd, response=False)
    await asyncio.sleep(1)


async def ble_request_scan(client) -> list[ScannedNetwork]:
    """
    Request a WiFi network scan via BLE.
    Command: [0x00, 0x00, 0x02, 0x00, 0x01, 0x02]
    Device responds with multiple notifications containing scan results.
    Each scan result notification: [0x00, 0x10, len_lo, len_hi, ssid_len, SSID..., BSSID(6), security, signal, flags]
    """
    networks: list[ScannedNetwork] = []
    scan_done = asyncio.Event()
    last_notification_time = [time.time()]

    def scan_handler(sender, data: bytes):
        last_notification_time[0] = time.time()
        if len(data) < 5:
            return
        # Scan results have type=0x00, sub-type=0x10
        if data[0] == 0x00 and data[1] == 0x10:
            payload_len = data[2] | (data[3] << 8)
            if len(data) < 4 + payload_len:
                return
            ssid_len = data[4]
            offset = 5
            ssid = data[offset:offset + ssid_len].decode("utf-8", errors="replace")
            offset += ssid_len
            if offset + 9 <= len(data):
                bssid = data[offset:offset + 6]
                security = data[offset + 6]
                signal = data[offset + 7]
                flags = data[offset + 8]
                networks.append(ScannedNetwork(
                    ssid=ssid, bssid=bssid, security=security,
                    signal=signal, flags=flags
                ))

    await client.start_notify(NOTIFY_CHAR_UUID, scan_handler)

    cmd = bytes([0x00, 0x00, 0x02, 0x00, 0x01, 0x02])
    await client.write_gatt_char(WRITE_CHAR_UUID, cmd, response=False)

    # Wait for scan results (they arrive in a burst ~3-4 seconds after command)
    await asyncio.sleep(5)
    # Wait a bit more if results are still arriving
    while time.time() - last_notification_time[0] < 1.5:
        await asyncio.sleep(0.5)

    await client.stop_notify(NOTIFY_CHAR_UUID)
    return networks


async def ble_provision_wifi(client, bssid: bytes, password: str, security_type: int = 1):
    """
    Send WiFi credentials to the device.
    Command format: [0x01, 0x00, payload_len_lo, payload_len_hi, BSSID(6), security(1), pass_len(1), password...]
    Note: security_type for provisioning is always 0x01 for WPA/WPA2 networks,
    regardless of what the scan reports (scan uses different encoding).
    """
    pass_bytes = password.encode("utf-8")
    payload = bssid + bytes([0x01, len(pass_bytes)]) + pass_bytes
    payload_len = len(payload)
    cmd = bytes([0x01, 0x00, payload_len & 0xFF, (payload_len >> 8) & 0xFF]) + payload
    if ARGS.debug:
        print(f"  [BLE TX] {cmd.hex(' ')}")
    await client.write_gatt_char(WRITE_CHAR_UUID, cmd, response=False)
    await asyncio.sleep(2)


async def ble_full_provision(device_address: str, adapter: str | None = None, ssid: str | None = None, password: str | None = None) -> WifiInfo | None:
    """
    Full WiFi provisioning flow over BLE.
    Uses a single notification subscription for the entire session to avoid
    BLE service discovery issues.
    """
    wifi_info = None
    wifi_event = asyncio.Event()
    scan_results: list[ScannedNetwork] = []
    last_scan_time = [0.0]

    def handler(sender, data: bytes):
        nonlocal wifi_info
        if ARGS.debug:
            print(f"  [BLE RX {len(data)}B] {data.hex(' ')}")
        if len(data) > 0 and data[0] == MSG_TYPE_WIFI_INFO:
            info = WifiInfo.from_notification(data)
            if ARGS.debug:
                print(f"    -> WiFi info (0x07): SSID='{info.ssid}' IP={info.ip}")
            if info.ip != "0.0.0.0":
                wifi_info = info
                wifi_event.set()
        elif data[0] == 0x00 and len(data) > 4 and data[1] == 0x20:
            # Akorn-style WiFi connected notification
            info = WifiInfo.from_status_notification(data)
            if ARGS.debug:
                print(f"    -> WiFi info (0x20): SSID='{info.ssid}' IP={info.ip}")
            if info.ip != "0.0.0.0":
                wifi_info = info
                wifi_event.set()
        elif data[0] == 0x02:
            if ARGS.debug:
                print(f"    -> Provision response: {data.hex(' ')}")
        elif data[0] == 0x00 and len(data) > 4 and data[1] == 0x10:
            payload_len = data[2] | (data[3] << 8)
            if len(data) >= 4 + payload_len:
                ssid_len = data[4]
                offset = 5
                net_ssid = data[offset:offset + ssid_len].decode("utf-8", errors="replace")
                offset += ssid_len
                if offset + 9 <= len(data):
                    bssid = data[offset:offset + 6]
                    security = data[offset + 6]
                    signal = data[offset + 7]
                    flags = data[offset + 8]
                    scan_results.append(ScannedNetwork(
                        ssid=net_ssid, bssid=bssid, security=security,
                        signal=signal, flags=flags
                    ))
                    last_scan_time[0] = time.time()

    print(f"Connecting to {device_address} via BLE (adapter: {adapter or 'default'})...")
    try:
        async with BleakClient(device_address, timeout=15.0, adapter=adapter) as client:
            await client.start_notify(NOTIFY_CHAR_UUID, handler)
            await asyncio.sleep(1)

            # Step 1: Auth handshake — if device has stored credentials,
            # it will auto-connect and send WiFi info without needing activate.
            print("Sending auth handshake...")
            await ble_authenticate(client)

            # Brief wait for auto-connect with stored creds
            print("Waiting for stored credentials...")
            try:
                await asyncio.wait_for(wifi_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            if wifi_info:
                return wifi_info

            # Step 2: No stored credentials — scan and provision
            # Do NOT send activate (0x06) here — it poisons the device state.
            print("No stored credentials. Scanning for WiFi networks...")
            if not ssid and not password:
                scan_results.clear()
                await client.write_gatt_char(WRITE_CHAR_UUID, bytes([0x00, 0x00, 0x02, 0x00, 0x01, 0x02]), response=False)
                await asyncio.sleep(6)
                while time.time() - last_scan_time[0] < 1.5 and last_scan_time[0] > 0:
                    await asyncio.sleep(0.5)

                networks = list(scan_results)
                if not networks:
                    # Retry scan once after a delay
                    print("No networks found. Retrying...")
                    await asyncio.sleep(3)
                    scan_results.clear()
                    last_scan_time[0] = 0.0
                    await client.write_gatt_char(WRITE_CHAR_UUID, bytes([0x00, 0x00, 0x02, 0x00, 0x01, 0x02]), response=False)
                    await asyncio.sleep(6)
                    while time.time() - last_scan_time[0] < 1.5 and last_scan_time[0] > 0:
                        await asyncio.sleep(0.5)
                    networks = list(scan_results)

                if not networks:
                    print("No networks found.")
                    return None

                seen: dict[str, ScannedNetwork] = {}
                for n in networks:
                    if n.ssid not in seen or n.signal > seen[n.ssid].signal:
                        seen[n.ssid] = n
                unique_networks = sorted(seen.values(), key=lambda n: n.signal, reverse=True)

                print(f"\nFound {len(unique_networks)} network(s):\n")
                for i, n in enumerate(unique_networks):
                    print(f"  [{i + 1}] {n.ssid:<30s} ({n.security_str}, signal: {n.signal})")

                while True:
                    raw = input(f"\nSelect network [1-{len(unique_networks)}]: ").strip()
                    try:
                        idx = int(raw) - 1
                        if 0 <= idx < len(unique_networks):
                            break
                    except ValueError:
                        pass
                    print("Invalid selection.")

                selected = unique_networks[idx]
                password = input(f"Password for '{selected.ssid}': ").strip()
                bssid = selected.bssid
                security_type = selected.security
            else:
                print(f"\nScanning for '{ssid}'...")
                scan_results.clear()
                await client.write_gatt_char(WRITE_CHAR_UUID, bytes([0x00, 0x00, 0x02, 0x00, 0x01, 0x02]), response=False)
                await asyncio.sleep(6)
                networks = list(scan_results)
                matching = [n for n in networks if n.ssid == ssid]
                if not matching:
                    print(f"Network '{ssid}' not found in scan results.")
                    return None
                selected = max(matching, key=lambda n: n.signal)
                bssid = selected.bssid
                security_type = selected.security

            # Step 4: Provision credentials
            print(f"Provisioning credentials for '{selected.ssid}'...")
            await ble_provision_wifi(client, bssid, password, security_type)

            # Step 5: Activate with new credentials
            print("Activating WiFi...")
            wifi_event.clear()
            wifi_info = None
            await client.write_gatt_char(WRITE_CHAR_UUID, CMD_ACTIVATE_WIFI, response=False)
            try:
                await asyncio.wait_for(wifi_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                print("Timeout waiting for WiFi connection.")

    except Exception as e:
        import traceback
        print(f"BLE error: {e}")
        if ARGS.debug:
            traceback.print_exc()

    return wifi_info


# --- History & Web Server ---

class StatusHistory:
    """Thread-safe storage of historical status readings with CSV persistence."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entries: list[dict] = []
        self.events: list[dict] = []
        self.current: DeviceStatus | None = None
        self.previous: DeviceStatus | None = None
        self.start_time = time.time()
        self.notified_targets: set[str] = set()
        # User-configurable alarm bounds (set via command line)
        # Values are int (fixed threshold) or "set" (use device's set temp at runtime)
        def _parse_bound(v):
            if v is None:
                return None
            if v.lower() == "set":
                return "set"
            return int(v)

        self.alarm_bounds: dict[str, dict] = {
            "probe1": {"low": _parse_bound(ARGS.p1_low), "high": _parse_bound(ARGS.p1_high)},
            "probe2": {"low": _parse_bound(ARGS.p2_low), "high": _parse_bound(ARGS.p2_high)},
            "probe3": {"low": _parse_bound(ARGS.p3_low), "high": _parse_bound(ARGS.p3_high)},
        }
        self.bounds_triggered: set[str] = set()

        # CSV log file
        ensure_cache_dir()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.session_name = f"session_{timestamp}"
        self.csv_path = LOG_DIR / f"{self.session_name}.csv"
        self.csv_file = open(self.csv_path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([
            "elapsed_sec", "timestamp",
            "probe1_cur", "probe1_set", "probe2_cur", "probe2_set",
            "probe3_cur", "probe3_set",
            "fan", "turbo", "door", "alarm",
            "fan_auto", "fan_speed"
        ])

    def add(self, status: DeviceStatus):
        elapsed = round(time.time() - self.start_time, 1)
        entry = {
            "t": elapsed,
            "p1": status.probe1.current,
            "p1_set": status.probe1.set_temp,
            "p2": status.probe2.current,
            "p2_set": status.probe2.set_temp,
            "p3": status.probe3.current,
            "p3_set": status.probe3.set_temp,
            "fan": status.fan_on,
            "turbo": status.fan_turbo,
            "door": status.door_open,
            "alarm": status.alarm_firing,
            "fan_auto": status.fan_auto,
            "fan_speed": status.fan_speed,
        }

        with self.lock:
            self.previous = self.current
            self.current = status
            self.entries.append(entry)

        # CSV persistence
        self.csv_writer.writerow([
            elapsed, time.strftime("%H:%M:%S"),
            status.probe1.current, status.probe1.set_temp,
            status.probe2.current, status.probe2.set_temp,
            status.probe3.current, status.probe3.set_temp,
            int(status.fan_on), int(status.fan_turbo),
            int(status.door_open), int(status.alarm_firing),
            int(status.fan_auto), status.fan_speed,
        ])
        self.csv_file.flush()

        # Detect events and send notifications
        self._check_events(status, elapsed)

    def _check_events(self, status: DeviceStatus, elapsed: float):
        prev = self.previous
        if prev is None:
            return

        if status.door_open and not prev.door_open:
            self._add_event(elapsed, "door", "Door opened")
        elif not status.door_open and prev.door_open:
            self._add_event(elapsed, "door", "Door closed")

        if status.alarm_firing and not prev.alarm_firing:
            self._add_event(elapsed, "alarm", "Alarm firing")
            send_notification(get_profile()["name"], "Alarm is firing!", critical=True)
        elif not status.alarm_firing and prev.alarm_firing:
            self._add_event(elapsed, "alarm", "Alarm dismissed")

        if status.fan_auto != prev.fan_auto or status.fan_speed != prev.fan_speed:
            if status.fan_auto:
                self._add_event(elapsed, "fan", "Fan auto")
            elif status.fan_speed == 0:
                self._add_event(elapsed, "fan", "Fan off")
            else:
                self._add_event(elapsed, "fan", f"Fan {status.fan_speed}%")

        for name, probe in [("Probe 1", status.probe1), ("Probe 2", status.probe2), ("Probe 3", status.probe3)]:
            if probe.connected and probe.has_target and probe.current is not None:
                key = f"{name}_{probe.set_temp}"
                if probe.current >= probe.set_temp and key not in self.notified_targets:
                    self.notified_targets.add(key)
                    self._add_event(elapsed, "target", f"{name} reached {probe.set_temp}°F")
                    send_notification(get_profile()["name"], f"{name} reached target ({probe.set_temp}°F)!", critical=True)

        # Check user-configurable alarm bounds
        for probe_key, probe in [("probe1", status.probe1), ("probe2", status.probe2), ("probe3", status.probe3)]:
            if not probe.connected or probe.current is None:
                continue
            bounds = self.alarm_bounds[probe_key]
            # Resolve "set" to the probe's current set temp
            low = bounds["low"]
            if low == "set":
                low = probe.set_temp
            high = bounds["high"]
            if high == "set":
                high = probe.set_temp

            if low is not None and probe.current < low:
                trigger_key = f"{probe_key}_low"
                if trigger_key not in self.bounds_triggered:
                    self.bounds_triggered.add(trigger_key)
                    label = f"{probe_key.replace('probe', 'Probe ')} below {low}°F ({probe.current}°F)"
                    self._add_event(elapsed, "alarm", label)
                    send_notification(get_profile()["name"], label, critical=True)
            elif low is not None and f"{probe_key}_low" in self.bounds_triggered:
                self.bounds_triggered.discard(f"{probe_key}_low")

            if high is not None and probe.current > high:
                trigger_key = f"{probe_key}_high"
                if trigger_key not in self.bounds_triggered:
                    self.bounds_triggered.add(trigger_key)
                    label = f"{probe_key.replace('probe', 'Probe ')} above {high}°F ({probe.current}°F)"
                    self._add_event(elapsed, "alarm", label)
                    send_notification(get_profile()["name"], label, critical=True)
            elif high is not None and f"{probe_key}_high" in self.bounds_triggered:
                self.bounds_triggered.discard(f"{probe_key}_high")

    def _add_event(self, elapsed: float, category: str, label: str):
        with self.lock:
            self.events.append({"t": elapsed, "cat": category, "label": label})

    def get_json(self) -> str:
        with self.lock:
            return json.dumps(self.entries)

    def get_events_json(self) -> str:
        with self.lock:
            return json.dumps(self.events)

    def get_current_json(self) -> str:
        with self.lock:
            if not self.current:
                return "{}"
            s = self.current
            stats = {}
            for key, label in [("p1", "probe1"), ("p2", "probe2"), ("p3", "probe3")]:
                vals = [e[key] for e in self.entries if e[key] is not None]
                if vals:
                    stats[label] = {"min": min(vals), "max": max(vals), "avg": round(sum(vals) / len(vals), 1)}
            return json.dumps({
                "probe1": {"current": s.probe1.current, "set": s.probe1.set_temp, "connected": s.probe1.connected},
                "probe2": {"current": s.probe2.current, "set": s.probe2.set_temp, "connected": s.probe2.connected},
                "probe3": {"current": s.probe3.current, "set": s.probe3.set_temp, "connected": s.probe3.connected},
                "fan": s.fan_on,
                "fan_auto": s.fan_auto,
                "fan_speed": s.fan_speed,
                "turbo": s.fan_turbo,
                "door": s.door_open,
                "alarm": s.alarm_firing,
                "device": get_profile(),
                "timestamp": time.strftime("%H:%M:%S"),
                "stats": stats,
                "session_minutes": round((time.time() - self.start_time) / 60, 1),
                "max_temp": ARGS.max_temp,
                "chamber_alarm_range": CHAMBER_ALARM_RANGE,
                "bounds": {
                    k: {
                        "low": v["low"] if v["low"] != "set" else (getattr(s, k).set_temp if hasattr(s, k) else None),
                        "high": v["high"] if v["high"] != "set" else (getattr(s, k).set_temp if hasattr(s, k) else None),
                        "low_is_set": v["low"] == "set",
                        "high_is_set": v["high"] == "set",
                    } for k, v in self.alarm_bounds.items()
                },
            })

    def close(self):
        self.csv_file.close()

    def load_from_csv(self, csv_path: str):
        """Load historical entries from a previous session CSV file and reopen it for appending."""
        with open(csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                def parse_int(v):
                    if v == "" or v == "None":
                        return None
                    return int(v)
                entry = {
                    "t": float(row["elapsed_sec"]),
                    "p1": parse_int(row["probe1_cur"]),
                    "p1_set": parse_int(row["probe1_set"]),
                    "p2": parse_int(row["probe2_cur"]),
                    "p2_set": parse_int(row["probe2_set"]),
                    "p3": parse_int(row["probe3_cur"]),
                    "p3_set": parse_int(row["probe3_set"]),
                    "fan": row["fan"] == "1",
                    "turbo": row["turbo"] == "1",
                    "door": row["door"] == "1",
                    "alarm": row["alarm"] == "1",
                }
                with self.lock:
                    self.entries.append(entry)
        # Adjust start_time so new entries continue from where the old session left off
        if self.entries:
            last_t = self.entries[-1]["t"]
            self.start_time = time.time() - last_t
            print(f"  Resumed {len(self.entries)} readings ({last_t:.0f}s of history)")

        # Reopen the same file for appending (no header, continues the log)
        self.csv_file.close()
        self.csv_path = Path(csv_path)
        self.csv_file = open(csv_path, "a", newline="")
        self.csv_writer = csv.writer(self.csv_file)


DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Char-Griller Monitor</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }
  h1 { text-align: center; color: #ff6b35; margin-bottom: 5px; }
  .subtitle { text-align: center; color: #888; margin-bottom: 20px; font-size: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; max-width: 900px; margin: 0 auto 30px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; max-width: 900px; margin: 0 auto 30px; }
  .card { background: #16213e; border-radius: 10px; padding: 15px; text-align: center; border: 1px solid #0f3460; }
  .card.alarm { border-color: #e74c3c; animation: pulse 1s infinite; }
  @keyframes pulse { 50% { border-color: #ff0; } }
  .card h3 { margin: 0 0 8px; color: #aaa; font-size: 13px; text-transform: uppercase; }
  .temp { font-size: 36px; font-weight: bold; color: #ff6b35; }
  .temp.cool { color: #4ecdc4; }
  .set { font-size: 14px; color: #888; margin-top: 5px; }
  .badge { display: inline-block; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
  .badge.on { background: #27ae60; color: #fff; }
  .badge.off { background: #555; color: #aaa; }
  .badge.alert { background: #e74c3c; color: #fff; }
  .chart-container { max-width: 900px; margin: 0 auto; background: #16213e; border-radius: 10px; padding: 20px; border: 1px solid #0f3460; }
  .chart-controls { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  .chart-controls button { background: #0f3460; color: #eee; border: 1px solid #1a4a8a; border-radius: 4px; padding: 4px 10px; cursor: pointer; font-size: 12px; }
  .chart-controls button:hover { background: #1a4a8a; }
  .chart-controls button.active { background: #ff6b35; border-color: #ff6b35; }
  .chart-controls span { color: #888; font-size: 12px; }
  canvas { width: 100% !important; height: 300px !important; }
  .disconnected { text-align: center; color: #666; font-size: 24px; }
  .conn-lost { display: none; background: #e74c3c; color: #fff; text-align: center; padding: 12px; font-weight: bold; border-radius: 8px; max-width: 900px; margin: 0 auto 15px; animation: pulse 1.5s infinite; }
  .alarm-row { display: flex; gap: 6px; margin-top: 8px; justify-content: center; align-items: center; }
  .alarm-input { width: 60px; background: #0f3460; border: 1px solid #1a4a8a; color: #eee; padding: 4px 6px; border-radius: 4px; font-size: 12px; text-align: center; }
  .alarm-input::placeholder { color: #555; }
  .alarm-label { font-size: 11px; color: #666; }
  .alarm-set-btn { background: #0f3460; border: 1px solid #1a4a8a; color: #ff6b35; padding: 4px 8px; border-radius: 4px; font-size: 11px; cursor: pointer; }
  .alarm-set-btn:hover { background: #1a4a8a; }
  .settings-bar { display: flex; gap: 8px; align-items: center; justify-content: center; max-width: 900px; margin: 0 auto 15px; font-size: 12px; color: #888; }
  .settings-label { color: #666; }
  .fan-control { display: flex; gap: 6px; align-items: center; justify-content: center; margin-top: 8px; }
  .fan-select { background: #0f3460; border: 1px solid #1a4a8a; color: #eee; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
  .alarm-banner { display: none; background: #e74c3c; color: #fff; text-align: center; padding: 14px; font-weight: bold; font-size: 18px; border-radius: 8px; max-width: 900px; margin: 0 auto 15px; animation: pulse 0.5s infinite; cursor: pointer; }
</style>
</head>
<body>
<h1 id="deviceTitle">Char-Griller Monitor</h1>
<div class="subtitle" id="timestamp">Connecting...</div>
<div class="conn-lost" id="conn-status">CONNECTION LOST — Server is not responding</div>
<div class="alarm-banner" id="alarm-banner" onclick="dismissAlarm()">ALARM — Click to dismiss</div>
<div class="settings-bar" id="sessionBar">
  <span class="settings-label">Session:</span>
  <span id="sessionName" style="color:#ff6b35;cursor:pointer" onclick="renameCurrentSession()" title="Click to rename"></span>
  <button class="alarm-set-btn" onclick="renameCurrentSession()">Rename</button>
  <button class="alarm-set-btn" onclick="showSessions()">History</button>
</div>
<div id="sessionList" style="display:none;max-width:900px;margin:0 auto 15px;background:#16213e;border-radius:8px;padding:12px;border:1px solid #0f3460">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <span style="color:#aaa;font-size:13px;font-weight:bold">Past Sessions</span>
    <button class="alarm-set-btn" onclick="document.getElementById('sessionList').style.display='none'">Close</button>
  </div>
  <div id="sessionListItems" style="max-height:200px;overflow-y:auto"></div>
</div>
<div class="settings-bar" id="settingsBar">
  <span class="settings-label">Alarm:</span>
  <select class="fan-select" onchange="setTone(this.value)" id="toneSelect">
    <option value="beep">Beep</option>
    <option value="siren">Siren</option>
    <option value="chime">Chime</option>
    <option value="urgent">Urgent</option>
  </select>
  <button class="alarm-set-btn" onclick="testTone()">Test</button>
  <button class="alarm-set-btn" id="muteBtn" onclick="toggleMute()">Mute</button>
</div>
<div class="grid" id="cards"></div>
<div class="grid2" id="controls"></div>
<div class="chart-container">
  <div class="chart-controls">
    <button onclick="setZoom('all')" id="z-all" class="active">All</button>
    <button onclick="setZoom(5)" id="z-5">5m</button>
    <button onclick="setZoom(15)" id="z-15">15m</button>
    <button onclick="setZoom(30)" id="z-30">30m</button>
    <button onclick="setZoom(60)" id="z-60">1h</button>
    <button onclick="setZoom(120)" id="z-120">2h</button>
    <span>|</span>
    <button onclick="panChart(-1)">&larr;</button>
    <button onclick="panChart(1)">&rarr;</button>
    <button onclick="panChart(0)">Latest</button>
    <span id="chart-range"></span>
  </div>
  <canvas id="chart"></canvas>
</div>

<script>
let history = [];
let chart = null;
let zoomMinutes = 'all';  // 'all' or number of minutes to show
let panOffset = 0;        // seconds offset from latest (0 = latest visible)

function setZoom(mins) {
  zoomMinutes = mins;
  panOffset = 0;
  document.querySelectorAll('.chart-controls button[id^="z-"]').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('z-' + mins);
  if (btn) btn.classList.add('active');
  drawChart();
}

function panChart(dir) {
  if (zoomMinutes === 'all') return;
  const step = zoomMinutes * 60 * 0.25;  // pan 25% of visible window
  if (dir === 0) { panOffset = 0; }
  else { panOffset += dir * step; }
  if (panOffset < 0) panOffset = 0;
  drawChart();
}

function initChart() {
  const ctx = document.getElementById('chart').getContext('2d');
  chart = {ctx, canvas: document.getElementById('chart')};
  drawChart();
}

function drawChart() {
  if (!chart || history.length < 2) return;
  const canvas = chart.canvas;
  const ctx = chart.ctx;
  const W = canvas.width = canvas.offsetWidth * 2;
  const H = canvas.height = canvas.offsetHeight * 2;
  ctx.scale(1,1);

  const pad = {top: 30, right: 20, bottom: 40, left: 50};
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  ctx.clearRect(0, 0, W, H);

  // Determine visible time window
  const totalEnd = history[history.length-1].t;
  const totalStart = history[0].t;
  let visEnd, visStart;
  if (zoomMinutes === 'all') {
    visStart = totalStart;
    visEnd = totalEnd;
  } else {
    const windowSec = zoomMinutes * 60;
    visEnd = totalEnd - panOffset;
    visStart = visEnd - windowSec;
    if (visStart < totalStart) { visStart = totalStart; visEnd = visStart + windowSec; }
  }
  const timeRange = visEnd - visStart || 1;
  const t0 = visStart;

  // Update range display
  const rangeEl = document.getElementById('chart-range');
  if (rangeEl) {
    const fmt = (s) => Math.floor(s/60)+'m'+Math.round(s%60)+'s';
    rangeEl.textContent = fmt(visStart)+' – '+fmt(visEnd);
  }

  // Filter history to visible window for temp range calc
  const visible = history.filter(e => e.t >= visStart && e.t <= visEnd);
  if (visible.length < 1) return;

  // Find temp range from visible data only
  let minT = Infinity, maxT = -Infinity;
  const maxTempCap = window._maxTemp || 500;
  const series = ['p1','p2','p3','p1_set','p2_set','p3_set'];
  for (const e of visible) {
    for (const k of series) {
      if (e[k] !== null && e[k] <= maxTempCap) { minT = Math.min(minT, e[k]); maxT = Math.max(maxT, e[k]); }
    }
  }
  if (minT === Infinity) return;
  minT = Math.max(0, minT - 20);
  maxT = Math.min(maxT + 20, maxTempCap);
  const tRange = maxT - minT || 1;

  // Grid
  ctx.strokeStyle = '#2a2a4a';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 5; i++) {
    const y = pad.top + (plotH * i / 5);
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left+plotW, y); ctx.stroke();
    ctx.fillStyle = '#666'; ctx.font = '20px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText(Math.round(maxT - (tRange * i / 5)) + '°', pad.left - 8, y + 6);
  }

  // Time axis
  ctx.fillStyle = '#666'; ctx.textAlign = 'center';
  const mins = Math.floor(timeRange / 60);
  const step = Math.max(1, Math.floor(mins / 6));
  for (let m = 0; m <= mins; m += step) {
    const x = pad.left + (m * 60 / timeRange) * plotW;
    ctx.fillText(m + 'm', x, H - 10);
  }

  // Draw series
  const colors = {p1: '#ff6b35', p2: '#4ecdc4', p3: '#ffe66d', p1_set: '#ff6b3577', p2_set: '#4ecdc477', p3_set: '#ffe66d77'};
  const dashes = {p1: [], p2: [], p3: [], p1_set: [8,4], p2_set: [8,4], p3_set: [8,4]};

  for (const key of series) {
    const pts = history.filter(e => e[key] !== null);
    if (pts.length < 2) continue;
    ctx.beginPath();
    ctx.strokeStyle = colors[key];
    ctx.lineWidth = key.includes('set') ? 2 : 3;
    ctx.setLineDash(dashes[key]);
    let first = true;
    for (const e of pts) {
      const val = Math.min(e[key], maxTempCap);
      const x = pad.left + ((e.t - t0) / timeRange) * plotW;
      const y = pad.top + ((maxT - val) / tRange) * plotH;
      if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Legend
  ctx.font = '18px sans-serif';
  let lx = pad.left;
  const labels = {p1:'Probe 1', p2:'Probe 2', p3:'Probe 3'};
  for (const k of ['p1','p2','p3']) {
    if (history.some(e => e[k] !== null)) {
      ctx.fillStyle = colors[k]; ctx.fillRect(lx, 6, 20, 12);
      ctx.fillStyle = '#ccc'; ctx.textAlign = 'left'; ctx.fillText(labels[k], lx+25, 17);
      lx += 100;
    }
  }

  // Event annotations (vertical lines with labels)
  if (window._events && window._events.length) {
    const evColors = {door:'#e67e22', alarm:'#e74c3c', fan:'#3498db', target:'#2ecc71'};
    ctx.font = '14px sans-serif';
    for (const ev of window._events) {
      const x = pad.left + ((ev.t - t0) / timeRange) * plotW;
      if (x < pad.left || x > pad.left + plotW) continue;
      ctx.strokeStyle = evColors[ev.cat] || '#888';
      ctx.lineWidth = 1;
      ctx.setLineDash([4,4]);
      ctx.beginPath(); ctx.moveTo(x, pad.top); ctx.lineTo(x, pad.top + plotH); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = evColors[ev.cat] || '#888';
      ctx.textAlign = 'left';
      ctx.save();
      ctx.translate(x + 3, pad.top + 10);
      ctx.rotate(Math.PI/2);
      ctx.fillText(ev.label, 0, 0);
      ctx.restore();
    }
  }
}

// --- Session Management ---
let currentSessionFile = '';
let currentSessionLabel = '';

function loadSessionInfo() {
  fetch('/api/sessions').then(r => r.json()).then(data => {
    currentSessionFile = data.current;
    currentSessionLabel = data.current_label;
    const el = document.getElementById('sessionName');
    el.textContent = currentSessionLabel || currentSessionFile;
  });
}

function renameCurrentSession() {
  const name = prompt('Session name:', currentSessionLabel || '');
  if (name === null) return;
  fetch('/api/session/rename', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({file: currentSessionFile, label: name})
  }).then(() => {
    currentSessionLabel = name;
    document.getElementById('sessionName').textContent = name || currentSessionFile;
  });
}

function showSessions() {
  const panel = document.getElementById('sessionList');
  if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
  fetch('/api/sessions').then(r => r.json()).then(data => {
    const el = document.getElementById('sessionListItems');
    if (!data.sessions.length) { el.innerHTML = '<div style=\"color:#666;font-size:12px\">No sessions yet</div>'; }
    else {
      el.innerHTML = data.sessions.map(s => {
        const isCurrent = s.file === data.current;
        const label = s.label || s.file;
        const sizeKB = Math.round(s.size / 1024);
        return '<div style=\"display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #0f3460;font-size:12px\">'
          + '<a href=\"/session/'+s.file+'\" target=\"_blank\" style=\"color:'+(isCurrent?'#ff6b35':'#2aa198')+';text-decoration:none\">'+(isCurrent?'>> ':'')+label+'</a>'
          + '<span style=\"color:#666\">'+s.modified+' ('+sizeKB+'KB)</span>'
          + '</div>';
      }).join('');
    }
    panel.style.display = '';
  });
}

loadSessionInfo();

// --- Grill Command API ---
function sendCommand(target, value) {
  fetch('/api/command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({target: target, value: value})
  });
}

function silenceGrill() {
  sendCommand('silence', 0);
}

function sendGrillTemp(probe, el) {
  const val = el.value.trim();
  if (val === '') return;
  const num = parseInt(val);
  if (!isNaN(num) && num > 0 && num < 1000) {
    sendCommand(probe, num);
  }
}

function setFan(val) {
  if (val === 'auto') {
    sendCommand('fan', 4096);  // 0x1000 = auto
  } else {
    sendCommand('fan', parseInt(val));
  }
}


// --- Browser Alarm System ---
let alarmFiring = false;
let alarmInterval = null;
let alarmCtx = null;
let alarmMuted = localStorage.getItem('cgrillerMuted') === 'true';
let alarmTone = localStorage.getItem('cgrillerTone') || 'beep';

function getCtx() {
  if (!alarmCtx) alarmCtx = new (window.AudioContext || window.webkitAudioContext)();
  return alarmCtx;
}

const TONES = {
  beep: function() {
    const ctx = getCtx(); const now = ctx.currentTime;
    for (let i = 0; i < 3; i++) {
      const osc = ctx.createOscillator(); const g = ctx.createGain();
      osc.frequency.value = 880; g.gain.value = 0.3;
      osc.connect(g); g.connect(ctx.destination);
      osc.start(now + i * 0.25); osc.stop(now + i * 0.25 + 0.15);
    }
  },
  siren: function() {
    const ctx = getCtx(); const now = ctx.currentTime;
    const osc = ctx.createOscillator(); const g = ctx.createGain();
    osc.type = 'sawtooth'; g.gain.value = 0.2;
    osc.frequency.setValueAtTime(600, now);
    osc.frequency.linearRampToValueAtTime(1200, now + 0.3);
    osc.frequency.linearRampToValueAtTime(600, now + 0.6);
    osc.connect(g); g.connect(ctx.destination);
    osc.start(now); osc.stop(now + 0.6);
  },
  chime: function() {
    const ctx = getCtx(); const now = ctx.currentTime;
    [523, 659, 784, 1047].forEach(function(freq, i) {
      const osc = ctx.createOscillator(); const g = ctx.createGain();
      osc.type = 'sine'; osc.frequency.value = freq;
      g.gain.setValueAtTime(0.25, now + i * 0.2);
      g.gain.exponentialRampToValueAtTime(0.01, now + i * 0.2 + 0.4);
      osc.connect(g); g.connect(ctx.destination);
      osc.start(now + i * 0.2); osc.stop(now + i * 0.2 + 0.4);
    });
  },
  urgent: function() {
    const ctx = getCtx(); const now = ctx.currentTime;
    for (let i = 0; i < 5; i++) {
      const osc = ctx.createOscillator(); const g = ctx.createGain();
      osc.frequency.value = 1000; g.gain.value = 0.35;
      osc.connect(g); g.connect(ctx.destination);
      osc.start(now + i * 0.15); osc.stop(now + i * 0.15 + 0.08);
    }
  }
};

function playAlarmTone() {
  if (alarmMuted) return;
  (TONES[alarmTone] || TONES.beep)();
}

function testTone() {
  (TONES[alarmTone] || TONES.beep)();
}

function setTone(val) {
  alarmTone = val;
  localStorage.setItem('cgrillerTone', val);
}

function toggleMute() {
  alarmMuted = !alarmMuted;
  localStorage.setItem('cgrillerMuted', alarmMuted);
  document.getElementById('muteBtn').textContent = alarmMuted ? 'Unmute' : 'Mute';
}

function startAlarm(msg) {
  if (alarmFiring) return;
  alarmFiring = true;
  document.getElementById('alarm-banner').textContent = msg + ' — Click to snooze 5 min';
  document.getElementById('alarm-banner').style.display = 'block';
  playAlarmTone();
  alarmInterval = setInterval(playAlarmTone, 2000);
}

function dismissAlarm() {
  alarmFiring = false;
  if (alarmInterval) { clearInterval(alarmInterval); alarmInterval = null; }
  document.getElementById('alarm-banner').style.display = 'none';
  silenceGrill();
}

// Track which probes have fired — won't re-fire until temp drops below target
let alarmFiredProbes = new Set();

function checkAlarms(data) {
  if (alarmFiring) return;
  const probes = [
    {key: 'probe1', name: 'Chamber', d: data.probe1},
    {key: 'probe2', name: 'Probe 2', d: data.probe2},
    {key: 'probe3', name: 'Probe 3', d: data.probe3}
  ];
  for (const p of probes) {
    if (!p.d.connected || p.d.set === null) continue;
    const range = data.chamber_alarm_range || 25;
    if (p.key === 'probe1') {
      // Chamber: alarm if temp drifts too far from target (over or under)
      const overKey = p.key + '_over';
      const underKey = p.key + '_under';
      if (p.d.current >= p.d.set + range) {
        if (!alarmFiredProbes.has(overKey)) {
          alarmFiredProbes.add(overKey);
          startAlarm(p.name + ' is ' + (p.d.current - p.d.set) + ' over target (' + p.d.current + '/' + p.d.set + ')');
          return;
        }
      } else { alarmFiredProbes.delete(overKey); }
      if (p.d.current <= p.d.set - range) {
        if (!alarmFiredProbes.has(underKey)) {
          alarmFiredProbes.add(underKey);
          startAlarm(p.name + ' is ' + (p.d.set - p.d.current) + ' under target (' + p.d.current + '/' + p.d.set + ')');
          return;
        }
      } else { alarmFiredProbes.delete(underKey); }
    } else {
      // Food probes: alarm when target reached
      if (p.d.current >= p.d.set) {
        if (!alarmFiredProbes.has(p.key)) {
          alarmFiredProbes.add(p.key);
          startAlarm(p.name + ' reached target (' + p.d.current + '/' + p.d.set + ')');
          return;
        }
      } else {
        alarmFiredProbes.delete(p.key);
      }
    }
  }
}

function updateUI(data) {
  document.getElementById('timestamp').textContent = 'Last update: ' + data.timestamp + ' | Session: ' + data.session_minutes + ' min';
  let html = '';

  const probes = [{name:'Probe 1 (Chamber)', key:'probe1', d:data.probe1}, {name:'Probe 2 (Food)', key:'probe2', d:data.probe2}, {name:'Probe 3 (Food)', key:'probe3', d:data.probe3}];
  for (const p of probes) {
    if (p.d.connected) {
      const setHtml = p.d.set !== null ? '<div class=\"set\">Target: '+p.d.set+'°F</div>' : '';
      let alarmHtml = '';
      if (p.key === 'probe1') {
        // Chamber: always sends to grill, no app/grill toggle
        alarmHtml = '<div class=\"alarm-row\">'
          + '<span class=\"alarm-label\">Set temp:</span>'
          + '<input class=\"alarm-input\" id=\"alarm-'+p.key+'\" type=\"number\" min=\"1\" max=\"999\" placeholder=\"°F\" value=\"'+(p.d.set||'')+'\" '
          + 'onfocus=\"this.select()\" '
          + 'onblur=\"sendGrillTemp(\\''+p.key+'\\', this)\" '
          + 'onkeydown=\"if(event.key===\\'Enter\\')this.blur()\">'
          + '</div>';
      } else {
        // Food probes: send target to grill, alarm fires via grill's alarm byte
        alarmHtml = '<div class=\"alarm-row\">'
          + '<span class=\"alarm-label\">Set temp:</span>'
          + '<input class=\"alarm-input\" id=\"alarm-'+p.key+'\" type=\"number\" min=\"1\" max=\"999\" placeholder=\"°F\" value=\"'+(p.d.set||'')+'\" '
          + 'onfocus=\"this.select()\" '
          + 'onblur=\"sendGrillTemp(\\''+p.key+'\\', this)\" '
          + 'onkeydown=\"if(event.key===\\'Enter\\')this.blur()\">'
          + '</div>';
      }
      html += '<div class=\"card\"><h3>'+p.name+'</h3><div class=\"temp\">'+p.d.current+'°F</div>'+setHtml+alarmHtml+'</div>';
    } else {
      html += '<div class=\"card\"><h3>'+p.name+'</h3><div class=\"disconnected\">—</div><div class=\"set\">Not connected</div></div>';
    }
  }

  let ctrlHtml = '';
  const dev = data.device || {};
  if (dev.show_fan !== false) {
    let fanLabel;
    if (data.fan_auto) { fanLabel = 'Auto'; }
    else if (data.fan_speed === 0) { fanLabel = 'Off'; }
    else { fanLabel = data.fan_speed + '%'; }
    let fanClass = (data.fan_auto || data.fan_speed > 0) ? 'on' : 'off';
    let fanVal = data.fan_auto ? 'auto' : String(data.fan_speed);
    ctrlHtml += '<div class=\"card\"><h3>Fan</h3><span class=\"badge '+fanClass+'\">'+fanLabel+'</span>'
      + '<div class=\"fan-control\"><select class=\"fan-select\" onchange=\"setFan(this.value)\">'
      + '<option value=\"auto\"'+(fanVal==='auto'?' selected':'')+'>Auto</option>'
      + '<option value=\"0\"'+(fanVal==='0'?' selected':'')+'>Off</option>'
      + '<option value=\"5\"'+(fanVal==='5'?' selected':'')+'>5%</option>'
      + '<option value=\"20\"'+(fanVal==='20'?' selected':'')+'>20%</option>'
      + '<option value=\"40\"'+(fanVal==='40'?' selected':'')+'>40%</option>'
      + '<option value=\"60\"'+(fanVal==='60'?' selected':'')+'>60%</option>'
      + '<option value=\"80\"'+(fanVal==='80'?' selected':'')+'>80%</option>'
      + '<option value=\"100\"'+(fanVal==='100'?' selected':'')+'>100%</option>'
      + '</select></div></div>';
  }
  if (dev.show_door !== false) {
    ctrlHtml += '<div class=\"card\"><h3>Door</h3><span class=\"badge '+(data.door?'alert':'off')+'\">'+(data.door?'OPEN':'Closed')+'</span></div>';
  }

  if (dev.web_title) {
    document.getElementById('deviceTitle').textContent = dev.web_title;
    document.title = dev.web_title;
  }

  // Alarm
  if (data.alarm) {
    html += '<div class=\"card alarm\"><h3>Alarm</h3><span class=\"badge alert\">FIRING</span></div>';
  }

  // Don't rebuild while user is interacting with inputs or dropdowns
  const focused = document.activeElement;
  const isEditing = focused && (focused.classList.contains('alarm-input') || focused.classList.contains('fan-select'));
  if (!isEditing) {
    document.getElementById('cards').innerHTML = html;
    document.getElementById('controls').innerHTML = ctrlHtml;
  }

  // Check browser alarms
  checkAlarms(data);
}

async function poll() {
  try {
    const [cur, hist, events] = await Promise.all([
      fetch('/api/status').then(r => r.json()),
      fetch('/api/history').then(r => r.json()),
      fetch('/api/events').then(r => r.json())
    ]);
    history = hist;
    window._events = events;
    window._maxTemp = cur.max_temp || 500;
    updateUI(cur);
    drawChart();
    document.getElementById('conn-status').style.display = 'none';
    window._failCount = 0;
  } catch(e) {
    window._failCount = (window._failCount || 0) + 1;
    if (window._failCount >= 2) {
      document.getElementById('conn-status').style.display = 'block';
    }
  }
  setTimeout(poll, 1000);
}

initChart();
poll();
window.addEventListener('resize', drawChart);
// Init settings bar state
document.getElementById('toneSelect').value = alarmTone;
document.getElementById('muteBtn').textContent = alarmMuted ? 'Unmute' : 'Mute';
</script>
<div style="text-align:center;padding:20px 0 10px;font-size:11px;color:#555">
  <a href="https://www.youtube.com/@RobsBackyardBBQ" target="_blank" rel="noopener" style="color:#666;text-decoration:none">Find my Char-Griller videos on Rob's Backyard BBQ (YouTube)</a>
  <div style="margin-top:6px"><a href="https://github.com/kprojects/chargrillerd" target="_blank" rel="noopener" style="color:#444;text-decoration:none">Fork me on GitHub</a> &middot; MIT License</div>
</div>
</body>
</html>"""


SESSION_VIEW_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>__SESSION_LABEL__ - Session Replay</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }
  h1 { text-align: center; color: #ff6b35; margin-bottom: 5px; font-size: 24px; }
  .subtitle { text-align: center; color: #888; margin-bottom: 20px; font-size: 14px; }
  .chart-container { max-width: 900px; margin: 0 auto; background: #16213e; border-radius: 10px; padding: 20px; border: 1px solid #0f3460; }
  .chart-controls { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  .chart-controls button { background: #0f3460; color: #eee; border: 1px solid #1a4a8a; border-radius: 4px; padding: 4px 10px; cursor: pointer; font-size: 12px; }
  .chart-controls button:hover { background: #1a4a8a; }
  .chart-controls button.active { background: #ff6b35; border-color: #ff6b35; }
  .chart-controls span { color: #888; font-size: 12px; }
  canvas { width: 100% !important; height: 400px !important; }
  .stats { max-width: 900px; margin: 20px auto; display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
  .stat-card { background: #16213e; border-radius: 8px; padding: 12px; border: 1px solid #0f3460; text-align: center; }
  .stat-card h3 { color: #aaa; font-size: 11px; text-transform: uppercase; margin: 0 0 6px; }
  .stat-val { color: #ff6b35; font-size: 20px; font-weight: bold; }
  .stat-detail { color: #666; font-size: 11px; margin-top: 4px; }
</style>
</head>
<body>
<h1>__SESSION_LABEL__</h1>
<div class="subtitle">Session replay (read-only) &middot; <a href="/" style="color:#2aa198">Back to live</a></div>

<div class="stats" id="stats"></div>

<div class="chart-container">
  <div class="chart-controls">
    <button onclick="setZoom('all')" id="z-all" class="active">All</button>
    <button onclick="setZoom(5)" id="z-5">5m</button>
    <button onclick="setZoom(15)" id="z-15">15m</button>
    <button onclick="setZoom(30)" id="z-30">30m</button>
    <button onclick="setZoom(60)" id="z-60">1h</button>
    <button onclick="setZoom(120)" id="z-120">2h</button>
    <span>|</span>
    <button onclick="panChart(-1)">&larr;</button>
    <button onclick="panChart(1)">&rarr;</button>
    <button onclick="panChart(0)">Latest</button>
    <span id="chart-range"></span>
  </div>
  <canvas id="chart"></canvas>
</div>

<script>
let history = [];
let zoomMinutes = 'all';
let panOffset = 0;

function setZoom(mins) {
  zoomMinutes = mins;
  panOffset = 0;
  document.querySelectorAll('.chart-controls button[id^="z-"]').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('z-' + mins);
  if (btn) btn.classList.add('active');
  drawChart();
}

function panChart(dir) {
  if (zoomMinutes === 'all') return;
  const step = zoomMinutes * 60 * 0.25;
  if (dir === 0) { panOffset = 0; }
  else { panOffset += dir * step; }
  if (panOffset < 0) panOffset = 0;
  drawChart();
}

function drawChart() {
  if (!history.length) return;
  const canvas = document.getElementById('chart');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * dpr;
  canvas.height = canvas.clientHeight * dpr;
  ctx.scale(dpr, dpr);
  const W = canvas.clientWidth;
  const H = canvas.clientHeight;
  ctx.clearRect(0, 0, W, H);

  const maxT = history[history.length - 1].t;
  let tMin = 0, tMax = maxT;
  if (zoomMinutes !== 'all') {
    const window_s = zoomMinutes * 60;
    tMax = maxT - panOffset;
    tMin = tMax - window_s;
    if (tMin < 0) { tMin = 0; tMax = Math.min(window_s, maxT); }
  }

  const rangeEl = document.getElementById('chart-range');
  function fmt(s) { const m = Math.floor(s/60); return m < 60 ? m+'m'+Math.floor(s%60)+'s' : Math.floor(m/60)+'h'+m%60+'m'; }
  rangeEl.textContent = fmt(tMin) + ' - ' + fmt(tMax);

  const pad = {top: 30, bottom: 30, left: 40, right: 10};
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  const visible = history.filter(e => e.t >= tMin && e.t <= tMax);
  if (!visible.length) return;

  let yMax = 0;
  visible.forEach(e => {
    if (e.p1 !== null && e.p1 > yMax) yMax = e.p1;
    if (e.p2 !== null && e.p2 > yMax) yMax = e.p2;
    if (e.p3 !== null && e.p3 > yMax) yMax = e.p3;
    if (e.p1_set !== null && e.p1_set > yMax) yMax = e.p1_set;
  });
  yMax = Math.ceil(yMax * 1.15 / 10) * 10;
  if (yMax < 50) yMax = 50;

  function x(t) { return pad.left + (t - tMin) / (tMax - tMin) * plotW; }
  function y(v) { return pad.top + plotH - (v / yMax) * plotH; }

  // Grid
  ctx.strokeStyle = '#1a3a5c'; ctx.lineWidth = 0.5;
  for (let i = 0; i <= 5; i++) {
    const yy = pad.top + plotH * i / 5;
    ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(W - pad.right, yy); ctx.stroke();
    ctx.fillStyle = '#666'; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText(Math.round(yMax * (5 - i) / 5), pad.left - 5, yy + 3);
  }

  // Target line (dashed)
  const lastSet = visible[visible.length-1].p1_set;
  if (lastSet) {
    ctx.strokeStyle = '#ff6b35'; ctx.lineWidth = 1; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(pad.left, y(lastSet)); ctx.lineTo(W-pad.right, y(lastSet)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // Draw lines
  const series = [{key:'p1',color:'#ff6b35'},{key:'p2',color:'#3498db'},{key:'p3',color:'#f1c40f'}];
  series.forEach(s => {
    ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.beginPath();
    let started = false;
    visible.forEach(e => {
      if (e[s.key] !== null) {
        const px = x(e.t), py = y(e[s.key]);
        if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
      }
    });
    if (started) ctx.stroke();
  });

  // Legend
  ctx.font = '11px sans-serif';
  let lx = pad.left + 5;
  series.forEach(s => {
    ctx.fillStyle = s.color;
    ctx.fillRect(lx, pad.top - 18, 10, 10);
    ctx.fillText(s.key === 'p1' ? 'Probe 1' : s.key === 'p2' ? 'Probe 2' : 'Probe 3', lx + 14, pad.top - 9);
    lx += 80;
  });
}

fetch('/api/session/data/__SESSION_NAME__').then(r => r.json()).then(data => {
  history = data;
  drawChart();

  // Stats
  const duration = data.length ? data[data.length-1].t : 0;
  const durationStr = duration > 3600 ? Math.floor(duration/3600)+'h '+Math.floor(duration%3600/60)+'m' : Math.floor(duration/60)+'m';
  const p1vals = data.filter(e => e.p1 !== null).map(e => e.p1);
  const p3vals = data.filter(e => e.p3 !== null).map(e => e.p3);
  let statsHtml = '<div class="stat-card"><h3>Duration</h3><div class="stat-val">'+durationStr+'</div><div class="stat-detail">'+data.length+' readings</div></div>';
  if (p1vals.length) {
    statsHtml += '<div class="stat-card"><h3>Chamber</h3><div class="stat-val">'+Math.round(p1vals.reduce((a,b)=>a+b)/p1vals.length)+'&deg;F avg</div><div class="stat-detail">'+Math.min(...p1vals)+'&deg; - '+Math.max(...p1vals)+'&deg;</div></div>';
  }
  if (p3vals.length) {
    statsHtml += '<div class="stat-card"><h3>Probe 3</h3><div class="stat-val">'+Math.round(p3vals.reduce((a,b)=>a+b)/p3vals.length)+'&deg;F avg</div><div class="stat-detail">'+Math.min(...p3vals)+'&deg; - '+Math.max(...p3vals)+'&deg;</div></div>';
  }
  document.getElementById('stats').innerHTML = statsHtml;
});

window.addEventListener('resize', drawChart);
</script>
<div style="text-align:center;padding:20px 0 10px;font-size:11px;color:#555">
  <a href="https://www.youtube.com/@RobsBackyardBBQ" target="_blank" rel="noopener" style="color:#666;text-decoration:none">Find my Char-Griller videos on Rob's Backyard BBQ (YouTube)</a>
  <div style="margin-top:6px"><a href="https://github.com/kprojects/chargrillerd" target="_blank" rel="noopener" style="color:#444;text-decoration:none">Fork me on GitHub</a> &middot; MIT License</div>
</div>
</body>
</html>"""


import queue

# Shared command queue — web server puts commands, TCP loop sends them
_cmd_queue = queue.Queue()

# TCP command format (no 0019 header for TCP)
def build_tcp_cmd(probe_id: int, value: int) -> bytes:
    return bytes([0x04, 0x00, 0xFF, probe_id, (value >> 8) & 0xFF, value & 0xFF])

CMD_IDS = {
    "probe1": 0x00,  # chamber target
    "probe2": 0x02,  # probe 2 target
    "probe3": 0x03,  # probe 3 target
    "silence": 0x05, # silence alarm (value 0x0000)
    "fan": 0x06,     # fan speed (0x1000 = auto)
    # NOTE: timer (0x01) and power (0x04) use a different command format
    # (m.b() full-packet builder, not simple m.d()). Do not use with this
    # simple command structure — it corrupts grill state.
}


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the status dashboard."""

    history: StatusHistory = None  # set by start_web_server

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            if self.path == "/api/command":
                target = body.get("target", "")
                value = body.get("value")
                if target in CMD_IDS and value is not None:
                    cmd = build_tcp_cmd(CMD_IDS[target], int(value))
                    _cmd_queue.put(cmd)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True, "sent": cmd.hex(" ")}).encode())
                else:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": "Invalid target or value"}).encode())
            elif self.path == "/api/session/rename":
                file_stem = body.get("file", "")
                label = body.get("label", "")
                if file_stem:
                    rename_session(file_stem, label)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True}).encode())
                else:
                    self.send_response(400)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.end_headers()

    def do_GET(self):
        try:
            if self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(DASHBOARD_HTML.encode())
            elif self.path == "/api/status":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(self.history.get_current_json().encode())
            elif self.path == "/api/history":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(self.history.get_json().encode())
            elif self.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(self.history.get_events_json().encode())
            elif self.path == "/api/sessions":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                current = self.history.session_name
                meta = load_sessions_meta()
                current_label = meta.get(current, {}).get("label", "")
                self.wfile.write(json.dumps({
                    "current": current,
                    "current_label": current_label,
                    "sessions": list_sessions()
                }).encode())
            elif self.path.startswith("/api/session/data/"):
                name = self.path.split("/")[-1]
                csv_path = LOG_DIR / f"{name}.csv"
                if not csv_path.exists():
                    self.send_response(404)
                    self.end_headers()
                    return
                entries = []
                with open(csv_path, "r") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        def pint(v):
                            return int(v) if v and v != "None" else None
                        entries.append({
                            "t": float(row["elapsed_sec"]),
                            "p1": pint(row["probe1_cur"]),
                            "p1_set": pint(row["probe1_set"]),
                            "p2": pint(row["probe2_cur"]),
                            "p2_set": pint(row["probe2_set"]),
                            "p3": pint(row["probe3_cur"]),
                            "p3_set": pint(row["probe3_set"]),
                        })
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(entries).encode())
            elif self.path.startswith("/session/"):
                name = self.path.split("/")[-1]
                csv_path = LOG_DIR / f"{name}.csv"
                if not csv_path.exists():
                    self.send_response(404)
                    self.end_headers()
                    return
                meta = load_sessions_meta()
                label = meta.get(name, {}).get("label", name)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(SESSION_VIEW_HTML.replace("__SESSION_NAME__", name).replace("__SESSION_LABEL__", label).encode())
            else:
                self.send_response(404)
                self.end_headers()
        except BrokenPipeError:
            pass

    def log_message(self, format, *args):
        pass  # suppress request logs


def start_web_server(history: StatusHistory, port: int) -> http.server.HTTPServer:
    DashboardHandler.history = history
    server = http.server.HTTPServer(("0.0.0.0", port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# --- TCP Functions ---

def try_tcp_connection(ip: str, timeout: float = 3.0) -> bytes | None:
    """
    Attempt TCP connection to device port 3333.
    Returns initial status bytes if successful, None otherwise.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, TCP_PORT))
        data = sock.recv(1024)
        sock.close()
        return data
    except (socket.timeout, socket.error, OSError):
        try:
            sock.close()
        except:
            pass
        return None


def monitor_status(ip: str, ble_device_address: str | None = None):
    """
    Connect to device TCP port 3333 and display live status.
    Also starts a web server on the configured port for a dashboard with
    real-time status and historical temperature graphs.
    Press Ctrl+C to exit.
    """
    current_ip = ip
    history = StatusHistory()
    if ARGS.resume:
        try:
            history.load_from_csv(ARGS.resume)
        except Exception as e:
            print(f"  Warning: could not load session: {e}")
    web_server = start_web_server(history, ARGS.port)
    print(f"\n  Dashboard: http://localhost:{ARGS.port}")
    print(f"  Session log: {history.csv_path}")

    def tcp_monitor_loop() -> bool:
        """
        Run the TCP monitoring loop.
        Returns True if user pressed Ctrl+C (should exit),
        False if connection dropped (should reconnect).
        """
        nonlocal current_ip
        print(f"\nConnecting to {current_ip}:{TCP_PORT}...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)

        try:
            sock.connect((current_ip, TCP_PORT))
        except (socket.timeout, socket.error, OSError) as e:
            print(f"Failed to connect: {e}")
            return False

        print("Connected! Streaming status (Ctrl+C to exit)...\n")

        buf = b""
        lines_printed = 0
        last_data_time = time.time()
        try:
            while True:
                try:
                    chunk = sock.recv(256)
                    if not chunk:
                        print("\nConnection closed by device.")
                        return False
                    buf += chunk
                    last_data_time = time.time()
                except socket.timeout:
                    if time.time() - last_data_time > 5:
                        print("\nDevice not responding (no data for 5s).")
                        return False
                    continue
                except (socket.error, OSError):
                    print("\nConnection lost.")
                    return False

                # Send any queued commands
                while not _cmd_queue.empty():
                    try:
                        cmd = _cmd_queue.get_nowait()
                        sock.send(cmd)
                        if ARGS.debug:
                            print(f"\n  [CMD TX] {cmd.hex(' ')}")
                    except (socket.error, OSError, queue.Empty):
                        pass

                while len(buf) >= STATUS_PACKET_SIZE:
                    if buf[4] in (ALARM_SILENT, ALARM_FIRING) and buf[5] == 0x00:
                        packet = buf[:STATUS_PACKET_SIZE]
                        buf = buf[STATUS_PACKET_SIZE:]

                        status = DeviceStatus.from_bytes(packet)
                        history.add(status)
                        output = status.format_display()
                        output += f"\n\n  Last update: {time.strftime('%H:%M:%S')}"
                        output += "\n  Press Ctrl+C to exit"

                        # Move cursor up to overwrite previous output
                        if lines_printed > 0:
                            sys.stdout.write(f"\033[{lines_printed}A\033[J")
                        sys.stdout.write(output + "\n")
                        sys.stdout.flush()
                        lines_printed = output.count("\n") + 1
                    else:
                        buf = buf[1:]

        except KeyboardInterrupt:
            return True
        finally:
            sock.close()

    # Main monitor loop with auto-reconnect
    while True:
        user_exit = tcp_monitor_loop()

        if user_exit:
            break

        # Connection lost — alert and try to reconnect
        print("\nConnection lost. Attempting to reconnect...")
        send_notification(get_profile()["name"], "Connection to device lost!", critical=True)
        reconnected = False
        for attempt in range(15):
            time.sleep(2)
            data = try_tcp_connection(current_ip, timeout=2.0)
            if data and len(data) >= STATUS_PACKET_SIZE:
                print(f"  Reconnected to {current_ip}:{TCP_PORT}")
                reconnected = True
                break
            sys.stdout.write(f"  Retry {attempt + 1}/15...\r")
            sys.stdout.flush()

        if reconnected:
            continue

        if not ble_device_address:
            print("\nCould not reconnect. No BLE address available.")
            break

        # TCP reconnect failed — try BLE re-activation
        print("\n  TCP reconnect failed. Attempting BLE re-activation...")
        for attempt in range(3):
            print(f"  BLE attempt {attempt + 1}/3...")
            wifi_info = asyncio.run(ble_activate_wifi(ble_device_address))
            if wifi_info and wifi_info.ip != "0.0.0.0":
                current_ip = wifi_info.ip
                print(f"  Reconnected! Device at {current_ip}")
                time.sleep(2)
                reconnected = True
                break
            time.sleep(3)

        if not reconnected:
            print("Failed to reconnect after all attempts.")
            break

    # Disconnect device from WiFi on exit (only if we connected via BLE)
    history.close()
    print(f"\nSession log saved: {history.csv_path}")
    print("Disconnected.")


# --- Network Scan ---

def scan_subnet_for_devices(timeout: float = 2.0) -> list[str]:
    """
    Scan the local subnet for devices with TCP port 3333 open that respond
    with valid Gravity status packets.
    Uses the machine's default interface IP to determine the /24 subnet,
    then probes all addresses concurrently.
    Returns list of responding IPs.
    """
    # Determine local IP and subnet
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        return []

    subnet_prefix = ".".join(local_ip.split(".")[:3])
    print(f"Scanning for Char-Griller devices on port {TCP_PORT}...")

    found_ips: list[str] = []
    lock = __import__("threading").Lock()

    def check_host(ip: str):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            if sock.connect_ex((ip, TCP_PORT)) == 0:
                # Verify it sends valid status data
                try:
                    data = sock.recv(64)
                    if (len(data) >= STATUS_PACKET_SIZE
                            and data[4] in (ALARM_SILENT, ALARM_FIRING)
                            and data[5] == 0x00):
                        with lock:
                            found_ips.append(ip)
                except socket.timeout:
                    pass
        except (socket.error, OSError):
            pass
        finally:
            sock.close()

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = []
        for i in range(1, 255):
            ip = f"{subnet_prefix}.{i}"
            if ip == local_ip:
                continue
            futures.append(executor.submit(check_host, ip))
        concurrent.futures.wait(futures, timeout=timeout + 2)

    found_ips.sort()
    return found_ips


# --- Main Flow ---

async def main():
    print("=" * 50)
    print("  chargrillerd — Command Line Monitor")
    print("=" * 50)
    print()

    # --disconnect mode: find device via BLE and send WiFi disconnect command
    if ARGS.disconnect:
        print("Scanning for device to disconnect...")
        devices = await scan_for_devices(timeout=ARGS.scan_timeout)
        if not devices:
            print("No device found via BLE. It may already be disconnected.")
            return None, None
        device_addr = devices[0][0]
        device_adapter = devices[0][3]
        print(f"Found: {devices[0][1]}")

        # Check if device is on WiFi first
        cache = load_device_cache()
        ip = cache["ip"] if cache else None
        was_on_wifi = False
        if ip:
            data = try_tcp_connection(ip, timeout=2.0)
            was_on_wifi = data is not None and len(data) >= STATUS_PACKET_SIZE
        print(f"WiFi status before: {'connected' if was_on_wifi else 'not connected'}")

        if not was_on_wifi:
            print("Device is not on WiFi. Nothing to disconnect.")
            return None, None

        print("Sending WiFi disconnect command...")
        success = await ble_disconnect_wifi(device_addr, adapter=device_adapter)
        if success:
            print("Device disconnected from WiFi successfully.")
        else:
            print("Disconnect may have failed (device still reachable on WiFi).")
        return None, None

    # Step 1: Direct IP mode (--ip flag)
    if ARGS.ip:
        print(f"Connecting directly to {ARGS.ip}...")
        data = try_tcp_connection(ARGS.ip)
        if data and len(data) >= STATUS_PACKET_SIZE:
            # Load cached BLE address for reconnect capability
            cache = load_device_cache()
            ble_addr = cache["ble_address"] if cache else None
            if ble_addr:
                print(f"  (Using cached BLE address: {ble_addr[:20]}...)")
            return ARGS.ip, ble_addr
        else:
            print(f"Cannot reach device at {ARGS.ip}:{TCP_PORT}")
            return None, None

    # Step 1b: WiFi subnet scan mode (--wifi flag)
    if ARGS.wifi:
        print("Step 1: Scanning WiFi subnet for devices (--wifi flag)...")
        wifi_ips = scan_subnet_for_devices()

        if wifi_ips:
            if len(wifi_ips) == 1:
                wifi_ip = wifi_ips[0]
                print(f"\nFound device at {wifi_ip}:{TCP_PORT}")
            else:
                print(f"\nFound {len(wifi_ips)} device(s) on WiFi:\n")
                for i, ip in enumerate(wifi_ips):
                    print(f"  [{i + 1}] {ip}")
                while True:
                    raw = input(f"\nSelect device [1-{len(wifi_ips)}]: ").strip()
                    try:
                        idx = int(raw) - 1
                        if 0 <= idx < len(wifi_ips):
                            wifi_ip = wifi_ips[idx]
                            break
                    except ValueError:
                        pass
                    print("Invalid selection.")

            monitor_status(wifi_ip, None)
            return None, None

        print("No devices found on WiFi.")
        return None, None

    # Step 2: Scan for Gravity devices via BLE
    print("Step 2: Scanning for devices via BLE...")
    devices = await scan_for_devices(timeout=ARGS.scan_timeout)

    if not devices:
        print("\nNo devices found via BLE or WiFi.")
        print("Make sure the device is powered on and in range.")
        ip = input("\nEnter device IP manually (or Enter to exit): ").strip()
        if not ip:
            return None, None
        data = try_tcp_connection(ip)
        if data and len(data) >= STATUS_PACKET_SIZE:
            return ip, None
        else:
            print(f"Cannot reach device at {ip}:{TCP_PORT}")
        return None, None

    # Step 3: Let user choose device
    print(f"\nFound {len(devices)} Char-Griller device(s):\n")
    for i, (addr, name, rssi, _adp) in enumerate(devices):
        print(f"  [{i + 1}] {name}  (RSSI: {rssi} dBm)")

    if len(devices) == 1:
        choice = 0
        print(f"\nAuto-selecting: {devices[0][1]}")
    else:
        while True:
            raw = input(f"\nSelect device [1-{len(devices)}]: ").strip()
            try:
                choice = int(raw) - 1
                if 0 <= choice < len(devices):
                    break
            except ValueError:
                pass
            print("Invalid selection.")

    device_addr, device_name, _, device_adapter = devices[choice]
    detect_device_from_name(device_name)
    profile = get_profile()

    # Step 4: Connect and provision WiFi
    # Tries stored credentials first. If that fails, scans for networks
    # and provisions credentials interactively.
    print(f"\nSelected: {device_name} (detected: {profile['name']})")
    wifi_info = await ble_full_provision(device_addr, adapter=device_adapter)

    if wifi_info is None:
        print("\nFailed to connect device to WiFi.")
        ip = input("\nEnter device IP manually (or Enter to exit): ").strip()
        if not ip:
            return None, None
    else:
        ip = wifi_info.ip
        print(f"\nDevice connected to WiFi successfully!")
        print(f"  SSID:    {wifi_info.ssid}")
        print(f"  IP:      {wifi_info.ip}")
        print(f"  Gateway: {wifi_info.gateway}")
        save_device_cache(device_addr, ip)

    # Step 6: Give device time to join WiFi and start TCP listener.
    print(f"\nWaiting for TCP service at {ip}:{TCP_PORT}...")
    for attempt in range(15):
        time.sleep(2)
        data = try_tcp_connection(ip, timeout=2.0)
        if data and len(data) >= STATUS_PACKET_SIZE:
            print(f"Device ready!")
            break
        sys.stdout.write(f"  Attempt {attempt + 1}/15...\r")
        sys.stdout.flush()
    else:
        print(f"\nCould not connect to {ip}:{TCP_PORT} after 30 seconds.")
        print("The device may have failed to join WiFi.")
        return None, None

    return ip, device_addr


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting.")
        sys.exit(0)

    if result is None:
        sys.exit(0)

    ip, ble_addr = result
    if ip is None:
        sys.exit(0)

    # Monitoring runs outside asyncio.run() so we can use asyncio freely
    # for BLE reconnect/disconnect inside monitor_status.
    monitor_status(ip, ble_addr)
