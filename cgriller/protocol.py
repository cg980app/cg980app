"""Wire protocol: constants, packet/notification parsing, and command framing.

Pure data layer — no configuration or I/O. DeviceStatus.format_display() takes a
device profile dict rather than reaching for global config.
"""

import struct
from dataclasses import dataclass


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
STATUS_PACKET_SIZE = 16

# --- Sentinel Values ---

TEMP_PROBE_DISCONNECTED = 1000  # 0x03E8 in current temp = probe not connected
TEMP_NOT_CONFIGURED = 4096     # 0x1000 in set temp = no target set
ALARM_SILENT = 0x10
ALARM_FIRING = 0x00
FAN_ON_BIT = 0x10              # Byte 14, bit 4
DOOR_OPEN_BIT = 0x01           # Byte 15, bit 0
TURBO_BIT = 0x10               # Byte 15, bit 4


# --- TCP command framing ---

# TCP control commands are 4 bytes: FF [id] [hi] [lo]. Verified against the
# official Android client (com.jdkj.grill.c.a.c.m strips the first 4 bytes —
# 00 19 04 00 — from the 8-byte BLE-form command before writing to the socket).
# Older docs showed a 6-byte form starting with 04 00; that is incorrect for
# the Gravity 980, which silently ignores it. The Akorn happens to be more
# permissive and accepts both, but the 4-byte form is what we ship.
def build_tcp_cmd(probe_id: int, value: int) -> bytes:
    return bytes([0xFF, probe_id, (value >> 8) & 0xFF, value & 0xFF])


# BLE control commands are the same 4-byte TCP command prefixed with the
# 00 19 04 00 header (00 19 = framing magic, 04 00 = payload length=4),
# written to WRITE_CHAR_UUID. See protocol.md "Command Format".
BLE_CMD_HEADER = bytes([0x00, 0x19, 0x04, 0x00])


def tcp_cmd_to_ble(cmd: bytes) -> bytes:
    """Wrap a 4-byte TCP command as its 8-byte BLE equivalent."""
    return BLE_CMD_HEADER + cmd


def ble_status_payload(data: bytes) -> bytes | None:
    """Extract the status payload from a BLE status-heartbeat notification of
    the form ``[00 19 <len> 00 <payload>]`` (sent ~1/sec). The payload is the
    same Device Status Format used over TCP. Returns None if `data` is not a
    status heartbeat (e.g. a WiFi-info or scan-result notification)."""
    if len(data) < 4 or data[0] != 0x00 or data[1] != 0x19:
        return None
    payload = data[4:4 + data[2]]
    return payload if len(payload) >= 16 else None


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

FAN_AUTO = 0x1000  # fan-speed sentinel for "auto"

# Acceptable value ranges per command. Probe 1 is the chamber and lives in the
# normal smoking range; probes 2/3 are food probes — the device rejects
# targets below 50°F.
CMD_RANGES = {
    "probe1": (200, 700),
    "probe2": (50, 350),
    "probe3": (50, 350),
    "fan":    (0, 100),  # plus the FAN_AUTO sentinel
    "silence": (0, 0),
}


def validate_cmd_value(target: str, value: int) -> str | None:
    """Return None if (target, value) is acceptable, else a human-readable error.
    Bounds-check guards against accidental UI corruption (e.g. -500°F target)."""
    if target not in CMD_RANGES:
        return f"Unknown command target: {target}"
    if target == "fan" and value == FAN_AUTO:
        return None
    lo, hi = CMD_RANGES[target]
    if not lo <= value <= hi:
        if target == "fan":
            return f"Fan must be {lo}-{hi} (or {FAN_AUTO} for auto), got {value}"
        if target == "silence":
            return f"Silence value must be 0, got {value}"
        return f"{target} value must be {lo}-{hi}°F, got {value}"
    return None


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

        # Extended bytes (16-19) — fan mode/speed. Only the Auto Akorn (20-byte
        # packet) reports these; the Gravity 980 (16-byte) carries fan state only
        # in the fan_on bit above, so it is never "auto" and has no speed.
        fan_auto = False
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

    def format_display(self, profile: dict) -> str:
        """Format status for terminal display, using the given device profile."""
        W = 42  # inner width between ║ bars
        lines = []
        lines.append("╔" + "═" * W + "╗")
        lines.append("║" + profile["cli_title"].center(W) + "║")
        lines.append("╠" + "═" * W + "╣")

        for name, probe in [("Probe 1 (Chamber)", self.probe1), ("Probe 2 (Food)   ", self.probe2), ("Probe 3 (Food)   ", self.probe3)]:
            if probe.connected:
                target = f" → {probe.set_temp}°F" if probe.has_target else ""
                content = f"  {name}: {probe.current:>4}°F{target}"
            else:
                content = f"  {name}: -- disconnected --"
            lines.append("║" + content.ljust(W) + "║")

        lines.append("╠" + "═" * W + "╣")
        parts = []
        if profile["show_fan"]:
            if self.fan_auto:
                fan_str = "Auto"
            elif self.fan_speed > 0:
                fan_str = f"{self.fan_speed}%"
            elif self.fan_turbo:
                fan_str = "Turbo"
            elif self.fan_on:
                fan_str = "On"
            else:
                fan_str = "Off"
            parts.append(f"Fan: {fan_str}")
        if profile["show_door"]:
            parts.append(f"Door: {'OPEN' if self.door_open else 'CLOSED'}")
        if parts:
            content = "  " + "  |  ".join(parts)
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
