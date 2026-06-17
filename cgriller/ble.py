"""BLE discovery and WiFi provisioning for Char-Griller controllers.

Low-level transport: takes explicit parameters (adapter, debug) rather than
depending on app configuration.
"""

import asyncio
import socket
import sys
import time

from .protocol import (
    CMD_ACTIVATE_WIFI,
    MSG_TYPE_WIFI_INFO,
    NOTIFY_CHAR_UUID,
    TCP_PORT,
    WRITE_CHAR_UUID,
    ScannedNetwork,
    WifiInfo,
)

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Error: 'bleak' package required. Install with: pip install bleak")
    sys.exit(1)


async def scan_for_devices(timeout: float = 8.0, adapter: str | None = None) -> list[tuple[str, str, int, str | None]]:
    """
    Scan for Char-Griller BLE devices (Akorn and/or Gravity).
    Returns list of (address, name, rssi, adapter) tuples.
    """
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


async def ble_activate_wifi(device_address: str, adapter: str | None = None, debug: bool = False) -> WifiInfo | None:
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


async def ble_disconnect_wifi(device_address: str, ip: str | None, adapter: str | None = None) -> bool:
    """
    Connect to device via BLE and send the WiFi disconnect command [0x05, 0x00, 0x00, 0x00].
    The device responds with a notification that crashes bleak's service cache — this is
    expected and harmless. We verify success by checking TCP becomes unreachable.

    `ip` is the device's last-known IP (from the cache), used only to verify the
    disconnect took effect; pass None to skip verification.
    """
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


async def ble_authenticate(client, debug: bool = False):
    """
    Send session-start handshake command.
    The last two bytes are nominally a PIN but the device does not validate them.
    Command format: [0x00, 0x19, 0x04, 0x00, 0xFF, 0x05, 0xFF, 0xFF]
    """
    cmd = bytes([0x00, 0x19, 0x04, 0x00, 0xFF, 0x05, 0xFF, 0xFF])
    if debug:
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


async def ble_provision_wifi(client, bssid: bytes, password: str, security_type: int = 1, debug: bool = False):
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
    if debug:
        print(f"  [BLE TX] {cmd.hex(' ')}")
    await client.write_gatt_char(WRITE_CHAR_UUID, cmd, response=False)
    await asyncio.sleep(2)


async def ble_full_provision(device_address: str, adapter: str | None = None, ssid: str | None = None, password: str | None = None, debug: bool = False) -> tuple[WifiInfo | None, str | None]:
    """
    Full WiFi provisioning flow over BLE.
    Uses a single notification subscription for the entire session to avoid
    BLE service discovery issues.

    Returns (wifi_info, detected_model) where detected_model is "gravity" or
    "akorn" inferred from the WiFi-info notification format (or None if no such
    notification arrived). This is authoritative for devices that advertise the
    generic "BLEWIFI APP" name, where the BLE name can't reveal the model.
    """
    wifi_info = None
    detected_model: str | None = None
    wifi_event = asyncio.Event()
    scan_results: list[ScannedNetwork] = []
    last_scan_time = [0.0]

    def handler(sender, data: bytes):
        nonlocal wifi_info, detected_model
        if debug:
            print(f"  [BLE RX {len(data)}B] {data.hex(' ')}")
        if len(data) > 0 and data[0] == MSG_TYPE_WIFI_INFO:
            info = WifiInfo.from_notification(data)
            if debug:
                print(f"    -> WiFi info (0x07): SSID='{info.ssid}' IP={info.ip}")
            if info.ip != "0.0.0.0":
                wifi_info = info
                detected_model = "gravity"  # type-0x07 is the Gravity 980 format
                wifi_event.set()
        elif data[0] == 0x00 and len(data) > 4 and data[1] == 0x20:
            # Akorn-style WiFi connected notification
            info = WifiInfo.from_status_notification(data)
            if debug:
                print(f"    -> WiFi info (0x20): SSID='{info.ssid}' IP={info.ip}")
            if info.ip != "0.0.0.0":
                wifi_info = info
                detected_model = "akorn"  # type-0x00/0x20 is the Akorn format
                wifi_event.set()
        elif data[0] == 0x02:
            if debug:
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
            await ble_authenticate(client, debug=debug)

            # Brief wait for auto-connect with stored creds
            print("Waiting for stored credentials...")
            try:
                await asyncio.wait_for(wifi_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            if wifi_info:
                return wifi_info, detected_model

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
                    return None, detected_model

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
                    return None, detected_model
                selected = max(matching, key=lambda n: n.signal)
                bssid = selected.bssid
                security_type = selected.security

            # Step 4: Provision credentials
            print(f"Provisioning credentials for '{selected.ssid}'...")
            await ble_provision_wifi(client, bssid, password, security_type, debug=debug)

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
        if debug:
            traceback.print_exc()

    return wifi_info, detected_model
