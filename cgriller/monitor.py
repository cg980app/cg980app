"""TCP monitoring loop, subnet discovery, and the live dashboard session."""

import asyncio
import concurrent.futures
import queue
import socket
import sys
import threading
import time
import webbrowser

from .ble import BleakClient, ble_activate_wifi, ble_authenticate
from .config import Settings
from .history import StatusHistory
from .protocol import (
    ALARM_FIRING,
    ALARM_SILENT,
    NOTIFY_CHAR_UUID,
    STATUS_PACKET_SIZE,
    TCP_PORT,
    WRITE_CHAR_UUID,
    DeviceStatus,
    ble_status_payload,
    tcp_cmd_to_ble,
)
from .web import start_web_server


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


# Candidate TCP status-packet sizes. 16-byte = older Gravity 980 firmware
# (offsets 0-15); 20-byte = Auto Akorn / newer Gravity (bytes 16-19 add fan
# mode/speed). See protocol.md "Device Status Format".
PACKET_SIZE_MIN = 16
PACKET_SIZE_EXT = 20


def _is_status_header(buf: bytes, i: int) -> bool:
    """True if a status packet plausibly begins at index `i`: the alarm byte
    (offset 4) is a known value and the reserved byte (offset 5) is zero. Used
    to find and validate packet boundaries in the TCP stream."""
    return (i + 6 <= len(buf)
            and buf[i + 4] in (ALARM_SILENT, ALARM_FIRING)
            and buf[i + 5] == 0x00)


def monitor_status(settings: Settings, ip: str, ble_device_address: str | None = None):
    """
    Connect to device TCP port 3333 and display live status.
    Also starts a web server on the configured port for a dashboard with
    real-time status and historical temperature graphs.
    Press Ctrl+C to exit.
    """
    current_ip = ip
    profile = settings.get_profile()
    history = StatusHistory(settings)
    if settings.resume:
        try:
            history.load_from_csv(settings.resume)
        except Exception as e:
            print(f"  Warning: could not load session: {e}")
    cmd_queue: queue.Queue = queue.Queue()
    server = start_web_server(history, settings.host, settings.port, cmd_queue)
    # 0.0.0.0 isn't a usable browser address — point the user at localhost.
    display_host = "localhost" if settings.host in ("0.0.0.0", "") else settings.host
    dashboard_url = f"http://{display_host}:{settings.port}"
    print(f"\n  Dashboard: {dashboard_url}")
    print(f"  Session log: {history.csv_path}")
    if settings.open_browser:
        webbrowser.open(dashboard_url)

    def tcp_monitor_loop() -> bool:
        """
        Run the TCP monitoring loop.
        Returns True if user pressed Ctrl+C (should exit),
        False if connection dropped (should reconnect).
        """
        nonlocal current_ip, profile
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
        packet_size = 0  # 0 = not yet measured; locks to 16 or 20 from the stream

        def drain_commands():
            """Flush any UI-queued commands to the device. Called at the top of
            every loop iteration — including after a recv timeout — so a command
            never waits on the next inbound packet to go out, only on the recv
            timeout (~2s) at worst."""
            while not cmd_queue.empty():
                try:
                    cmd = cmd_queue.get_nowait()
                    sock.send(cmd)
                    if settings.debug:
                        print(f"\n  [CMD TX] {cmd.hex(' ')}")
                except (socket.error, OSError, queue.Empty):
                    pass

        try:
            while True:
                drain_commands()
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

                while True:
                    # Re-align so index 0 is a valid packet boundary, dropping
                    # any partial/garbage prefix.
                    if not _is_status_header(buf, 0):
                        nxt = 1
                        while nxt + 6 <= len(buf) and not _is_status_header(buf, nxt):
                            nxt += 1
                        if nxt + 6 > len(buf):
                            buf = buf[-5:]  # no header yet; keep tail for resync
                            break
                        buf = buf[nxt:]
                        continue

                    # Lock the packet size once, by measuring the distance to the
                    # next header: 16 (older Gravity) vs 20 (Akorn / newer Gravity).
                    if packet_size == 0:
                        if _is_status_header(buf, PACKET_SIZE_MIN):
                            packet_size = PACKET_SIZE_MIN
                        elif _is_status_header(buf, PACKET_SIZE_EXT):
                            packet_size = PACKET_SIZE_EXT
                        elif len(buf) >= PACKET_SIZE_EXT + 6:
                            buf = buf[1:]  # index 0 was a false header; resync
                            continue
                        else:
                            break  # need a second packet to measure the stride
                        # The packet stride identifies the model when no BLE name
                        # was seen (--wifi/--ip): 16-byte = Gravity, 20-byte = Akorn.
                        settings.resolve_device_from_packet_size(packet_size)
                        profile = settings.get_profile()

                    if len(buf) < packet_size:
                        break

                    packet = buf[:packet_size]
                    buf = buf[packet_size:]

                    status = DeviceStatus.from_bytes(packet)
                    history.add(status)
                    output = status.format_display(profile)
                    output += f"\n\n  Last update: {time.strftime('%H:%M:%S')}"
                    output += "\n  Press Ctrl+C to exit"

                    # Move cursor up to overwrite previous output
                    if lines_printed > 0:
                        sys.stdout.write(f"\033[{lines_printed}A\033[J")
                    sys.stdout.write(output + "\n")
                    sys.stdout.flush()
                    lines_printed = output.count("\n") + 1

        except KeyboardInterrupt:
            return True
        finally:
            sock.close()

    # Main monitor loop with auto-reconnect
    try:
        while True:
            user_exit = tcp_monitor_loop()

            if user_exit:
                break

            # Connection lost — try to reconnect (the dashboard's own watchdog
            # raises the audible alarm for the lost connection).
            print("\nConnection lost. Attempting to reconnect...")
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
                wifi_info = asyncio.run(ble_activate_wifi(ble_device_address, adapter=settings.adapter, debug=settings.debug))
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
    except KeyboardInterrupt:
        pass
    finally:
        # Wake any open SSE clients so they exit cleanly, then tear down the
        # web server before closing the CSV — otherwise daemon threads can die
        # mid-write and clients get a broken pipe.
        history.request_shutdown()
        server.shutdown()
        server.server_close()
        history.close()
    print(f"\nSession log saved: {history.csv_path}")
    print("Disconnected.")


async def monitor_status_ble(settings: Settings, device_address: str, adapter: str | None = None):
    """Monitor the device entirely over BLE — no WiFi.

    Subscribes to the device's status-heartbeat notifications (sent ~1/sec) and
    serves the same dashboard as the TCP monitor. Control commands queued by the
    dashboard are written over BLE (prefixed with the 00 19 header). Auto-reconnects
    if the BLE link drops. The device model is resolved from --device or the BLE
    name first, then refined by the heartbeat payload length (16 = Gravity, 20 =
    Akorn) — same signal we use over TCP.
    """
    history = StatusHistory(settings)
    if settings.resume:
        try:
            history.load_from_csv(settings.resume)
        except Exception as e:
            print(f"  Warning: could not load session: {e}")
    cmd_queue: queue.Queue = queue.Queue()
    server = start_web_server(history, settings.host, settings.port, cmd_queue)
    display_host = "localhost" if settings.host in ("0.0.0.0", "") else settings.host
    dashboard_url = f"http://{display_host}:{settings.port}"
    print(f"\n  Dashboard: {dashboard_url}")
    print(f"  Session log: {history.csv_path}")
    if settings.open_browser:
        webbrowser.open(dashboard_url)

    lines_printed = [0]  # mutable: updated from the notification callback

    def handle_notification(sender, data: bytes):
        if settings.debug:
            print(f"\n  [BLE RX {len(data)}B] {data.hex(' ')}")
        payload = ble_status_payload(data)
        if payload is None:
            return  # WiFi-info / scan-result / ack notification — not a heartbeat
        # Same model signal we use over TCP: 16-byte payload = Gravity 980,
        # 20-byte = Auto Akorn (extra fan mode/speed bytes).
        settings.resolve_device_from_packet_size(len(payload))
        profile = settings.get_profile()
        try:
            status = DeviceStatus.from_bytes(payload)
        except ValueError:
            return
        history.add(status)
        output = status.format_display(profile)
        output += f"\n\n  Last update: {time.strftime('%H:%M:%S')}"
        output += "\n  Connected over BLE — Press Ctrl+C to exit"
        if lines_printed[0] > 0:
            sys.stdout.write(f"\033[{lines_printed[0]}A\033[J")
        sys.stdout.write(output + "\n")
        sys.stdout.flush()
        lines_printed[0] = output.count("\n") + 1

    try:
        while True:  # reconnect loop
            print(f"\nConnecting to {device_address} over BLE (adapter: {adapter or 'default'})...")
            try:
                async with BleakClient(device_address, timeout=15.0, adapter=adapter) as client:
                    await client.start_notify(NOTIFY_CHAR_UUID, handle_notification)
                    # Session-start handshake (PIN bytes are not validated).
                    await ble_authenticate(client, debug=settings.debug)
                    print("Connected! Streaming status over BLE (Ctrl+C to exit)...\n")
                    lines_printed[0] = 0
                    while client.is_connected:
                        # Flush any UI-queued commands out over BLE.
                        while not cmd_queue.empty():
                            try:
                                await client.write_gatt_char(
                                    WRITE_CHAR_UUID, tcp_cmd_to_ble(cmd_queue.get_nowait()),
                                    response=False,
                                )
                            except queue.Empty:
                                break
                            except Exception as e:
                                if settings.debug:
                                    print(f"\n  [BLE CMD error] {e}")
                        await asyncio.sleep(0.2)
                print("\nBLE connection lost.")
            except Exception as e:
                print(f"\nBLE error: {e}")
            # The dashboard's connection watchdog raises the audible alarm.
            print("Reconnecting in 3s...")
            await asyncio.sleep(3)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        history.request_shutdown()
        server.shutdown()
        server.server_close()
        history.close()
        print(f"\nSession log saved: {history.csv_path}")
        print("Disconnected.")


def scan_subnet_for_devices(timeout: float = 2.0) -> list[str]:
    """
    Scan the local subnet for devices with TCP port 3333 open that respond
    with valid status packets.
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
    lock = threading.Lock()

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
