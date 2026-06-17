"""chargrillerd - Char-Griller BBQ Monitor & Web Dashboard

Supports Char-Griller Gravity 980 and Auto Akorn. Connects via BLE to
bootstrap the device onto WiFi, then monitors live probe/fan/door/alarm
status over TCP port 3333. Serves a real-time web dashboard.

Homepage: https://github.com/cg980app/cg980app

Requirements:
    pip install bleak

Usage:
    python3 chargrillerd.py                  # BLE connect (auto-detect)
    python3 chargrillerd.py --wifi           # Find device already on WiFi
    python3 chargrillerd.py --ip 192.168.x.x # Direct IP connection
    python3 chargrillerd.py --bluetooth      # Monitor over Bluetooth only (no WiFi)
    python3 chargrillerd.py --device akorn   # Force device type

Flow:
    1. Scans BLE for devices matching "Akorn-*", "Gravity980-*", or "BLEWIFI APP"
    2. Lets you pick one if multiple found
    3. Sends WiFi activate command [0x06] — uses stored credentials
    4. If stored credentials fail: sends auth handshake, scans for
       WiFi networks, provisions credentials via BLE, then activates
    5. Connects to device TCP port 3333, streams live status
    6. Serves web dashboard at http://localhost:<port>
    7. Logs session to CSV at ~/.cgriller/logs/
    8. Raises every alarm in the browser dashboard (sound + banner); no
       server-side desktop/ntfy notifications
    9. Auto-reconnects (TCP retry 30s, then BLE re-activation) on connection loss
    10. Detects device power loss within 5 seconds (no-data timeout)

Package layout:
    config        - CONFIG defaults, CLI parsing, Settings dataclass, device profiles
    protocol      - wire constants, packet/notification parsing, command framing
    storage       - cache dir, device cache, session metadata
    history       - StatusHistory (readings, events, CSV, SSE signal)
    ble           - BLE discovery and WiFi provisioning
    web           - HTTP dashboard + SSE stream + command POSTs (static/ holds the frontend)
    monitor       - TCP + BLE monitor loops, subnet discovery, dashboard session
    app           - device discovery flow + run() entrypoint

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
"""
