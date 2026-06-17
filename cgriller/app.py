"""Application entrypoint: device discovery flow and process startup."""

import asyncio
import sys
import time

from .ble import ble_disconnect_wifi, ble_full_provision, scan_for_devices
from .config import Settings, build_settings
from .monitor import (
    monitor_status,
    monitor_status_ble,
    scan_subnet_for_devices,
    try_tcp_connection,
)
from .protocol import STATUS_PACKET_SIZE, TCP_PORT
from .storage import load_device_cache, save_device_cache


def _choose_device(devices) -> tuple[str, str, str | None]:
    """Print the discovered BLE devices and return the chosen
    (address, name, adapter). Auto-selects when only one is found."""
    print(f"\nFound {len(devices)} Char-Griller device(s):\n")
    for i, (addr, name, rssi, _adp) in enumerate(devices):
        print(f"  [{i + 1}] {name}  (RSSI: {rssi} dBm)")

    if len(devices) == 1:
        print(f"\nAuto-selecting: {devices[0][1]}")
        choice = 0
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
    addr, name, _rssi, adp = devices[choice]
    return addr, name, adp


async def main(settings: Settings):
    print("=" * 50)
    print("  chargrillerd — Command Line Monitor")
    print("=" * 50)
    print()

    # --disconnect mode: find device via BLE and send WiFi disconnect command
    if settings.disconnect:
        print("Scanning for device to disconnect...")
        devices = await scan_for_devices(timeout=settings.scan_timeout, adapter=settings.adapter)
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
        success = await ble_disconnect_wifi(device_addr, ip, adapter=device_adapter)
        if success:
            print("Device disconnected from WiFi successfully.")
        else:
            print("Disconnect may have failed (device still reachable on WiFi).")
        return None, None

    # Bluetooth-only mode (--bluetooth): monitor entirely over BLE, never WiFi.
    if settings.bluetooth:
        print("Scanning for devices via BLE (--bluetooth: no WiFi)...")
        devices = await scan_for_devices(timeout=settings.scan_timeout, adapter=settings.adapter)
        if not devices:
            print("\nNo devices found via BLE.")
            print("Make sure the device is powered on and in range.")
            return None, None
        device_addr, device_name, device_adapter = _choose_device(devices)
        settings.resolve_device_from_name(device_name)
        print(f"\nSelected: {device_name} (detected: {settings.get_profile()['name']})")
        await monitor_status_ble(settings, device_addr, adapter=device_adapter)
        return None, None

    # Step 1: Direct IP mode (--ip flag)
    if settings.ip:
        print(f"Connecting directly to {settings.ip}...")
        data = try_tcp_connection(settings.ip)
        if data and len(data) >= STATUS_PACKET_SIZE:
            # Load cached BLE address for reconnect capability
            cache = load_device_cache()
            ble_addr = cache["ble_address"] if cache else None
            if ble_addr:
                print(f"  (Using cached BLE address: {ble_addr[:20]}...)")
            return settings.ip, ble_addr
        else:
            print(f"Cannot reach device at {settings.ip}:{TCP_PORT}")
            return None, None

    # Step 1b: WiFi subnet scan mode (--wifi flag)
    if settings.wifi:
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

            monitor_status(settings, wifi_ip, None)
            return None, None

        print("No devices found on WiFi.")
        return None, None

    # Step 2: Scan for devices via BLE
    print("Step 2: Scanning for devices via BLE...")
    devices = await scan_for_devices(timeout=settings.scan_timeout, adapter=settings.adapter)

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
    device_addr, device_name, device_adapter = _choose_device(devices)
    settings.resolve_device_from_name(device_name)

    # Whether the BLE name itself identifies the model. Generic provisioning-mode
    # names like "BLEWIFI APP" don't — the model is only known after the device's
    # notification protocol reveals it during provisioning, so don't claim one yet.
    name_identifies_model = "Akorn" in device_name or "Gravity" in device_name

    # Step 4: Connect and provision WiFi
    # Tries stored credentials first. If that fails, scans for networks
    # and provisions credentials interactively.
    if name_identifies_model or settings.device_pinned:
        print(f"\nSelected: {device_name} (detected: {settings.get_profile()['name']})")
    else:
        print(f"\nSelected: {device_name} (model not yet known — detecting...)")
    profile = settings.get_profile()
    wifi_info, detected_model = await ble_full_provision(device_addr, adapter=device_adapter, debug=settings.debug)

    # The notification protocol format is an authoritative model signal — correct
    # the name-based guess (e.g. a Gravity 980 advertising the generic "BLEWIFI APP").
    settings.resolve_device_from_model(detected_model)
    if settings.get_profile() is not profile:
        profile = settings.get_profile()
        print(f"Detected model from device: {profile['name']}")

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


def run():
    """Process entrypoint: build settings, run discovery, then monitor."""
    settings = build_settings()
    try:
        result = asyncio.run(main(settings))
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
    monitor_status(settings, ip, ble_addr)
