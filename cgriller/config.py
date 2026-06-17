"""Configuration: user-editable defaults, CLI parsing, and the Settings object.

All runtime configuration is collected into a single Settings instance by
build_settings(), which is then passed explicitly through the app instead of
relying on module-level globals.
"""

import argparse
from dataclasses import dataclass


# ==============================================================================
# CONFIGURATION — Edit these settings or leave as None for auto-detect.
#                 Command-line flags override these values.
# ==============================================================================

DEVICE = "auto"             # "auto", "gravity", or "akorn"
ADAPTER = None              # BLE adapter e.g. "hci0", or None for default
DEVICE_IP = None            # Known device IP e.g. "192.168.86.113", or None
PORT = 8080                 # Web dashboard port
HOST = "0.0.0.0"            # Web dashboard bind address (0.0.0.0 = all interfaces; 127.0.0.1 = localhost only)
SCAN_TIMEOUT = 8.0          # BLE scan duration (seconds)
MAX_TEMP = 500              # Graph Y-axis max (°F)
DEBUG = False               # Print BLE packet data

# Note: the browser alarm tone and the chamber drift-alarm range are chosen in
# the dashboard UI (per-browser, persisted in localStorage), not on the command
# line.

# ==============================================================================

VERSION = "1.0.0"


# --- Device Profiles ---

DEVICE_PROFILES = {
    "gravity": {
        "name": "Gravity 980",
        "ble_match": "Gravity",
        "show_door": True,
        "show_fan": True,
        "cli_title": "GRAVITY 980 STATUS",
        "web_title": "Gravity 980 Monitor",
        "supports_commands": True,
        # Gravity manages its own fan internally and doesn't honor the fan
        # commands (id 0x06 fan-speed and id 0x07 auto/turbo are both silently
        # ignored, verified against live hardware). Show the fan state
        # read-only — no controls.
        "supports_fan_control": False,
        # Silence command id 0x05 on Gravity flashes "0000" on the device
        # panel rather than silencing — it appears to be a stop/break command,
        # not silence. Don't send it; we still squelch the browser tone
        # locally when the user dismisses an alarm.
        "supports_silence": False,
    },
    "akorn": {
        "name": "Auto Akorn",
        "ble_match": "Akorn",
        "show_door": False,
        "show_fan": True,
        "cli_title": "AUTO AKORN STATUS",
        "web_title": "Auto Akorn Monitor",
        "supports_commands": True,
        "supports_fan_control": True,
        "supports_silence": True,
    },
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="chargrillerd - Char-Griller BBQ Monitor. "
                    "Supports Gravity 980 and Auto Akorn.",
        epilog="Examples:\n"
               "  %(prog)s                    Auto-detect device via BLE\n"
               "  %(prog)s --ip 192.168.0.151 Connect to known IP directly\n"
               "  %(prog)s --device akorn     Force device type\n"
               "  %(prog)s --bluetooth        Monitor over Bluetooth only (no WiFi)\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}"
    )
    parser.add_argument(
        "--device", choices=["auto", "gravity", "akorn"],
        default=DEVICE,
        help="Device type (default: %(default)s — detect from BLE name)"
    )
    # Discovery modes are mutually exclusive: choose how to reach the device.
    # With none given, the default is an interactive BLE scan.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--ip", metavar="DEVICE_IP", default=DEVICE_IP,
        help="Connect directly to a known device IP (skip all discovery)"
    )
    mode.add_argument(
        "--wifi", action="store_true",
        help="Skip BLE and scan the local subnet for the device on WiFi instead"
    )
    mode.add_argument(
        "--bluetooth", action="store_true",
        help="Monitor entirely over Bluetooth LE — never connect to WiFi"
    )
    mode.add_argument(
        "--disconnect", action="store_true",
        help="Disconnect device from WiFi (send BLE disconnect command) and exit"
    )
    parser.add_argument(
        "--scan-timeout", type=float, default=SCAN_TIMEOUT, metavar="SECS",
        help="BLE scan duration in seconds (default: %(default)s)"
    )
    parser.add_argument(
        "--port", type=int, default=PORT, metavar="PORT",
        help="Web server port for status dashboard (default: %(default)s)"
    )
    parser.add_argument(
        "--host", default=HOST, metavar="ADDRESS",
        help="Web server bind address (default: %(default)s = all interfaces; "
             "use 127.0.0.1 to restrict the dashboard to this machine only)"
    )
    parser.add_argument(
        "--open", action="store_true",
        help="Open the dashboard in your default web browser at startup"
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
        help="Maximum temperature for graph Y-axis scaling (default: %(default)s°F). "
             "Readings above this are clamped in the graph to prevent spikes from ruining scale."
    )
    return parser.parse_args()


@dataclass
class Settings:
    """Resolved runtime configuration, built from CLI args + module defaults.

    Passed explicitly through the app in place of the old module globals.
    `device_profile` is the one mutable field: it starts from --device (or None
    for auto) and is filled in once the BLE name is seen.
    """
    device: str
    adapter: str | None
    ip: str | None
    scan_timeout: float
    disconnect: bool
    wifi: bool
    bluetooth: bool
    port: int
    host: str
    open_browser: bool
    debug: bool
    resume: str | None
    max_temp: int
    device_profile: dict | None = None
    device_pinned: bool = False  # True when --device forced the profile (never auto-overridden)

    def resolve_device_from_name(self, ble_name: str):
        """Preliminary device guess from a BLE advertised name (no-op if the
        profile was pinned via --device).

        Names like "BLEWIFI APP" (generic WiFi-provisioning mode) can't be told
        apart by string alone, so this is only a guess — resolve_device_from_model()
        corrects it once the device's notification protocol reveals the real model.
        """
        if self.device_pinned:
            return
        if "Akorn" in ble_name:
            self.device_profile = DEVICE_PROFILES["akorn"]
        elif "Gravity" in ble_name:
            self.device_profile = DEVICE_PROFILES["gravity"]
        else:
            self.device_profile = DEVICE_PROFILES["akorn"]  # default fallback

    def resolve_device_from_model(self, model: str | None):
        """Pin the device profile from a model key detected at the protocol
        layer (e.g. from the WiFi-info notification format). Authoritative —
        overrides any earlier name-based guess — but never overrides --device.
        """
        if self.device_pinned or model not in DEVICE_PROFILES:
            return
        self.device_profile = DEVICE_PROFILES[model]

    def resolve_device_from_packet_size(self, packet_size: int):
        """Identify the model from the TCP status packet stride. The Gravity 980
        sends 16-byte packets (offsets 0-15); the Auto Akorn sends 20-byte
        packets (bytes 16-19 add fan mode/speed). This is the only model signal
        available in WiFi/IP discovery, where no BLE name is ever seen — but it
        never overrides --device.
        """
        if self.device_pinned:
            return
        if packet_size == 16:
            self.device_profile = DEVICE_PROFILES["gravity"]
        elif packet_size == 20:
            self.device_profile = DEVICE_PROFILES["akorn"]

    def get_profile(self) -> dict:
        """Current device profile, defaulting to akorn."""
        return self.device_profile or DEVICE_PROFILES["akorn"]


def build_settings() -> Settings:
    """Parse CLI args, merge with module defaults, and resolve a Settings."""
    args = parse_args()

    device_profile = DEVICE_PROFILES.get(args.device) if args.device != "auto" else None

    return Settings(
        device=args.device,
        adapter=args.adapter,
        ip=args.ip,
        scan_timeout=args.scan_timeout,
        disconnect=args.disconnect,
        wifi=args.wifi,
        bluetooth=args.bluetooth,
        port=args.port,
        host=args.host,
        open_browser=args.open,
        debug=args.debug,
        resume=args.resume,
        max_temp=args.max_temp,
        device_profile=device_profile,
        device_pinned=device_profile is not None,
    )
