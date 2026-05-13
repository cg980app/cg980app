# Gravity Device — BLE WiFi Bootstrap & Status Protocol

## Overview

The Gravity device is a temperature controller (grill/smoker controller) with three temperature probes, a fan, a door sensor, and an alarm. It uses Bluetooth Low Energy (BLE) as a bootstrap mechanism to bring the device onto a WiFi network. Once on WiFi, it exposes a TCP service on **port 3333** that streams real-time device status.

The BLE interaction is brief: the client connects, sends a command to activate WiFi using stored credentials, receives the device's network information (IP address, SSID, etc.), and then disconnects BLE. All subsequent status monitoring occurs over WiFi.

**Device Hardware**: Opulinks BLE+WiFi SoC (OPL-series chip)  
**BLE Advertised Name**: `Gravity980-XX:XX` (last 4 of MAC, e.g., `Gravity980-12:34`)  
**BLE GATT Device Name**: `BLEWIFI APP`  
**MAC Address (observed)**: `88:4A:18:XX:XX:XX` (Opulinks OUI)

---

## BLE Connection Parameters

| Parameter | Value |
|-----------|-------|
| BLE Type | Bluetooth Low Energy (BLE 4.x+) |
| Connection Interval (negotiated) | 45 ms (0x0024 × 1.25ms) |
| Supervision Timeout | 5000 ms (0x01F4 × 10ms) |
| MTU (negotiated) | 247 bytes (server-side limit) |
| Client MTU Request | 517 bytes |
| LE Data Length | 132 bytes (0x0084) |

The client should request an MTU exchange after service discovery. The device will respond with its maximum MTU of **247 bytes**.

---

## GATT Service Structure

### Custom WiFi Control Service

| Field | Value |
|-------|-------|
| Service UUID (16-bit) | `0xAAAA` |
| Full UUID | `0000aaaa-0000-1000-8000-00805f9b34fb` |

> **Note**: On some platforms (e.g., macOS/iOS CoreBluetooth), only the custom service is visible. The standard GAP and GATT services may be hidden by the OS.

#### Characteristics

| Characteristic | UUID | Full UUID | Properties | Description |
|----------------|------|-----------|------------|-------------|
| Command (TX) | `0xBBB0` | `0000bbb0-0000-1000-8000-00805f9b34fb` | Write, Write Without Response | Client writes commands here |
| Response (RX) | `0xBBB1` | `0000bbb1-0000-1000-8000-00805f9b34fb` | Notify | Device sends responses/status here |

**CCCD for Response characteristic**: Located at the descriptor handle immediately following the Response characteristic.

The device begins sending notifications immediately upon BLE connection. Implementations SHOULD still subscribe to notifications via the CCCD for compatibility.

---

## BLE Protocol

### Command Format (Client → Device)

Commands are written as raw bytes to the Command characteristic (UUID `0xBBB0`) using Write Without Response:

```
Offset  Size  Field
0       1     Command ID
1       3     Parameters (command-specific, zero-padded if unused)
```

Total command length: **4 bytes**.

### Response/Notification Format (Device → Client)

Responses arrive as notifications on the Response characteristic (UUID `0xBBB1`):

```
Offset  Size  Field
0       1     Message Type
1       N     Payload (type-specific)
```

---

## BLE Messages

### Command: Activate WiFi (ID: `0x06`)

Instructs the device to connect to its stored WiFi network and report connection details.

**Write value (4 bytes):**
```
06 00 00 00
```

**Expected response**: WiFi Connected Info notification (Message Type `0x07`).

---

### Command: Disconnect WiFi (ID: `0x05`)

Instructs the device to disconnect from WiFi and return to BLE-only mode. Should be sent on application exit to allow clean reconnection in future sessions.

**Write value (4 bytes):**
```
05 00 00 00
```

| Byte | Value | Meaning |
|------|-------|---------|
| 0 | `0x05` | Command: Disconnect WiFi |
| 1–3 | `0x00 0x00 0x00` | No parameters |

**Expected response**: Notification `06 10 01 00 00` (acknowledgment). Note: this response
may cause BLE service cache invalidation on some platforms (e.g., macOS CoreBluetooth).
The BLE connection may become unstable after sending this command — this is expected.
Verify disconnect success via TCP (port 3333 becomes unreachable).

After this command, the device will:
1. Disconnect from WiFi (TCP port 3333 becomes unreachable)
2. Continue advertising BLE for new connections

---

### Notification: WiFi Connected Info (Type: `0x07`)

Sent by the device after successfully connecting to WiFi. Contains all information needed to reach the device over the network.

**Structure:**

| Offset | Size | Field | Example | Description |
|--------|------|-------|---------|-------------|
| 0 | 1 | Message Type | `0x07` | WiFi Connected Info |
| 1 | 1 | WiFi Status | `0x10` | Connected |
| 2 | 1 | Signal Indicator | `0x2C` (44) | WiFi signal metric |
| 3 | 2 | Reserved | `0x00 0x00` | — |
| 5 | 1 | SSID Length (N) | `0x18` (24) | Length of SSID string |
| 6 | N | SSID | `"MyHomeNetwork"` | UTF-8, NOT null-terminated |
| 6+N | 6 | BSSID | `AA:BB:CC:DD:EE:FF` | AP MAC address |
| 12+N | 4 | IP Address | `C0 A8 01 64` | 192.168.1.100 |
| 16+N | 4 | Subnet Mask | `FF FF FF 00` | 255.255.255.0 |
| 20+N | 4 | Gateway | `C0 A8 01 01` | 192.168.1.1 |

**Total length** = 24 + SSID Length bytes.

All IP addresses are in **network byte order (big-endian)**.

---

### Notification: Device Status Heartbeat (Type: `0x00`)

Sent periodically (~1 per second) and immediately upon connection.

**Structure (20 bytes over BLE):**

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 1 | Message Type | `0x00` |
| 1 | 1 | Unknown | Observed as `0x19` (25) — possibly internal temp or protocol version |
| 2 | 1 | Unknown | Observed as `0x10` |
| 3 | 1 | Unknown | Observed as `0x00` |
| 4 | 16 | Status Data | Same format as TCP port 3333 response (see below) |

---

## WiFi TCP Protocol (Port 3333)

### Connection Behavior

Upon TCP connection to `<device IP>:3333`, the device immediately sends a **16-byte status packet**. It continues to stream updated 16-byte status packets approximately once per second.

No authentication or handshake is required.

---

## Device Status Format (16 bytes)

This is the core status structure. Over **TCP port 3333**, it is sent as raw 16 bytes. Over **BLE**, it is wrapped in a 20-byte notification with a 4-byte header (type `0x00` + 3 bytes).

### Field Map

| Offset | Size | Field | Sentinel | Description |
|--------|------|-------|----------|-------------|
| 0–1 | 2 | Probe 1 Current Temp | `0x03E8` (1000) = not connected | Big-endian uint16, degrees Fahrenheit |
| 2–3 | 2 | Probe 1 Set Temp | `0x1000` (4096) = not configured | Big-endian uint16, degrees Fahrenheit |
| 4 | 1 | Alarm | `0x10` = silent, `0x00` = firing | Alarm state |
| 5 | 1 | Reserved | `0x00` | Always zero |
| 6–7 | 2 | Probe 2 Current Temp | `0x03E8` (1000) = not connected | Big-endian uint16, degrees Fahrenheit |
| 8–9 | 2 | Probe 2 Set Temp | `0x1000` (4096) = not configured | Big-endian uint16, degrees Fahrenheit |
| 10–11 | 2 | Probe 3 Current Temp | `0x03E8` (1000) = not connected | Big-endian uint16, degrees Fahrenheit |
| 12–13 | 2 | Probe 3 Set Temp | `0x1000` (4096) = not configured | Big-endian uint16, degrees Fahrenheit |
| 14 | 1 | Fan | Bitmask (see below) | Fan state |
| 15 | 1 | Status Flags | Bitmask (see below) | Door + turbo |

### Temperature Values

- All temperatures are **big-endian unsigned 16-bit integers** representing degrees Fahrenheit
- `0x03E8` (1000) in a current temp field means **probe not connected**
- `0x1000` (4096) in a set temp field means **no target configured**
- Valid temperature example: `0x00C8` = 200°F, `0x0045` = 69°F

### Probe 1

Probe 1 appears to be the primary/chamber probe (always connected). The fan automatically turns off when Probe 1 current temp reaches Probe 1 set temp.

### Alarm (Byte 4)

| Value | Meaning |
|-------|---------|
| `0x10` | Alarm not firing (normal) |
| `0x00` | Alarm active (sounding) |

### Fan (Byte 14)

| Bit | Value | Meaning |
|-----|-------|---------|
| Bit 4 | `0x10` | Fan on |
| — | `0x00` | Fan off |

The fan turns on automatically when the door is opened. It turns off automatically when Probe 1 reaches set temp.

### Status Flags (Byte 15)

| Bit | Value | Meaning |
|-----|-------|---------|
| Bit 0 | `0x01` | Door open |
| Bit 4 | `0x10` | Fan turbo mode |

These flags combine as a bitmask. Examples:
- `0x00` = door closed, turbo off
- `0x01` = door open, turbo off
- `0x10` = door closed, turbo on
- `0x11` = door open, turbo on

---

## Example Status Packets

### All probes connected, fan on, door closed
```
Hex: 00 69 00 E1 10 00 00 45 00 60 00 49 00 5F 10 00
     ├─┤  ├─┤  ││  ├─┤  ├─┤  ├─┤  ├─┤  │  │
     P1   P1   A│  P2   P2   P3   P3   F  S
    cur  set  lr  cur  set  cur  set  an tatus
    105  225    m   69   96   73   95  on  --

Probe 1: current=105°F, set=225°F
Probe 2: current=69°F, set=96°F
Probe 3: current=73°F, set=95°F
Alarm: silent (0x10)
Fan: on (0x10)
Door: closed, turbo off (0x00)
```

### Probes 2+3 disconnected, device idle
```
Hex: 00 69 00 C8 10 00 03 E8 10 00 03 E8 10 00 00 00

Probe 1: current=105°F, set=200°F
Probe 2: NOT CONNECTED (1000)
Probe 2 set: NOT CONFIGURED (4096)
Probe 3: NOT CONNECTED (1000)
Probe 3 set: NOT CONFIGURED (4096)
Alarm: silent (0x10)
Fan: off (0x00)
Door: closed, turbo off (0x00)
```

### Alarm firing, door open, fan turbo
```
Hex: 00 69 00 C8 00 00 03 E8 10 00 00 47 00 5F 10 11

Alarm: FIRING (0x00)
Fan: on (0x10)
Status: door open + turbo (0x11)
```

---

## Communication Sequence

```
┌──────────┐                              ┌──────────────┐
│  Client  │                              │ Gravity Dev  │
└────┬─────┘                              └──────┬───────┘
     │                                           │
     │  1. BLE Scan (find "Gravity980-*"         │
     │     or "BLEWIFI APP")                     │
     │                                           │
     │  2. BLE Connect                           │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  3. Connection Complete                   │
     │ <─────────────────────────────────────────│
     │                                           │
     │  4. Status Notifications begin (~1/sec)   │
     │ <─────────────────────────────────────────│
     │     (20 bytes: type 0x00 + 3 hdr + 16 status)
     │                                           │
     │  5. Discover Service UUID 0xAAAA          │
     │ <────────────────────────────────────────>│
     │                                           │
     │  6. Subscribe to notifications (0xBBB1)   │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  7. Try activate: [06 00 00 00]           │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  8a. If stored creds work → WiFi Info     │
     │ <─────────────────────────────────────────│
     │     (Done! Skip to TCP)                   │
     │                                           │
     │  8b. If no creds → no response or 0.0.0.0│
     │     → Continue to provisioning flow       │
     │                                           │
     │  9. Disconnect BLE                        │
     │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─>│
     │                                           │
     │  10. TCP Connect to <IP>:3333             │
     │ ═════════════════════════════════════════>│
     │                                           │
     │  11. Receive 16-byte status (immediate)   │
     │ <═════════════════════════════════════════│
     │                                           │
     │  12. Continuous status stream (~1/sec)     │
     │ <═════════════════════════════════════════│
     │                                           │
```

---

## Implementation Guide

### Step 1: BLE Discovery

1. Scan for BLE peripherals with names matching `Gravity980-*` or `BLEWIFI APP`, or advertising service UUID `0xAAAA`
2. The device uses Opulinks OUI: `88:4A:18:xx:xx:xx`
3. The advertised name may alternate between `Gravity980-XX:XX` and `BLEWIFI APP` depending on firmware state

### Step 2: Connect and Discover Services

1. Establish BLE connection
2. Discover GATT services — find service with UUID `0000aaaa-0000-1000-8000-00805f9b34fb`
3. Find characteristics:
   - **Write**: UUID `0000bbb0-0000-1000-8000-00805f9b34fb` (Write/Write Without Response)
   - **Notify**: UUID `0000bbb1-0000-1000-8000-00805f9b34fb` (Notify)
4. Subscribe to notifications on the Notify characteristic (write `0x01 0x00` to its CCCD)
5. Request MTU exchange (recommend 247+ bytes)

### Step 3: Activate WiFi

1. Write `[0x06, 0x00, 0x00, 0x00]` to the Write characteristic using Write Without Response
2. Wait for a notification with byte 0 = `0x07` (WiFi Connected Info)
3. If received with valid IP: parse the IP address, proceed to TCP
4. If no response or IP = `0.0.0.0`: device needs provisioning (see PIN + Provisioning sections below)

### Step 4: Transition to WiFi

1. Disconnect BLE (optional — device stays on WiFi regardless)
2. Open a TCP socket to `<parsed IP>:3333`
3. Read the initial 16-byte status packet
4. Continue reading for periodic status updates

### Step 5: Parse Status

Use the 16-byte status format documented above to extract:
- Three probe temperatures (current and set)
- Alarm state
- Fan state
- Door and turbo flags

---

## PIN / Validation Key

The 4-digit PIN is sent to the device over BLE as part of the authentication handshake. The PIN (displayed on the device packaging or screen) is treated as a **hexadecimal value** and encoded into 2 bytes.

### Command: Authenticate (8 bytes)

```
00 19 04 00 FF 05 [pin_hi] [pin_lo]
```

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 1 | Command Type | `0x00` |
| 1 | 1 | Protocol ID | `0x19` |
| 2–3 | 2 | Sub-command | `0x04 0x00` |
| 4–5 | 2 | Fixed | `0xFF 0x05` |
| 6–7 | 2 | PIN | 4-digit PIN as 16-bit hex (e.g., "1234" → `0x12 0x34`, "5678" → `0x56 0x78`) |

Example: PIN "1234" → `00 19 04 00 FF 05 12 34`

To skip authentication (device already paired/initialized), send `0xFF 0xFF` as the PIN bytes:
`00 19 04 00 FF 05 FF FF`

**Note**: Authentication is only required when the device displays a PIN on its screen (typically on first connection). If the device has previously been paired, the activate command (`0x06`) works without authentication. The recommended flow is: try activate first, only authenticate if it fails.

---

## WiFi Network Scan

### Command: Request Scan (6 bytes)

```
00 00 02 00 01 02
```

Triggers the device to scan for available WiFi networks. Results arrive as multiple notifications over the next 3–5 seconds.

### Notification: Scan Result (Type: `0x00`, Sub-type: `0x10`)

Each available network is reported in a separate notification:

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 1 | Message Type | `0x00` |
| 1 | 1 | Sub-type | `0x10` (scan result) |
| 2–3 | 2 | Payload Length | Little-endian uint16 |
| 4 | 1 | SSID Length (N) | Length of SSID string |
| 5 | N | SSID | UTF-8 network name |
| 5+N | 6 | BSSID | MAC address of access point |
| 11+N | 1 | Security | 0=Open, 1=WPA, 2=WPA2, 3=WPA/WPA2, 4=WPA3 |
| 12+N | 1 | Signal | Signal strength indicator (higher = stronger) |
| 13+N | 1 | Flags | 0x01 = previously connected network |

---

## WiFi Credential Provisioning

### Command: Set Credentials (ID: `0x01`)

Sends WiFi credentials (BSSID + password) to the device for storage.

```
01 00 [len_lo] [len_hi] [BSSID × 6] [security] [pass_len] [password...]
```

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 1 | Command Type | `0x01` |
| 1 | 1 | Reserved | `0x00` |
| 2–3 | 2 | Payload Length | Little-endian uint16 (= 6 + 1 + 1 + password_length) |
| 4–9 | 6 | BSSID | Target access point MAC (from scan results) |
| 10 | 1 | Security Type | Must match scan result security field |
| 11 | 1 | Password Length | Length of password string |
| 12 | N | Password | UTF-8 encoded password |

**Expected response**: Notification `[0x02, 0x10, 0x01, 0x00, 0x00]` confirming credential storage.

After provisioning, send the Activate WiFi command (`06 00 00 00`) to connect.

---

## Complete Provisioning Sequence

This flow is only needed when the device has no stored WiFi credentials (first-time setup or after credentials are cleared).

```
┌──────────┐                              ┌──────────────┐
│  Client  │                              │ Gravity Dev  │
└────┬─────┘                              └──────┬───────┘
     │                                           │
     │  (Activate [06 00 00 00] already failed)  │
     │                                           │
     │  1. Authenticate                          │
     │  [00 19 04 00 FF 05 pin_hi pin_lo]       │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  2. Try stored credentials                │
     │  [06 00 00 00]                            │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  3a. If connected → WiFi Info (0x07)      │
     │ <─────────────────────────────────────────│
     │  (Done! Skip to TCP)                      │
     │                                           │
     │  3b. If no stored creds → IP=0.0.0.0      │
     │ <─────────────────────────────────────────│
     │                                           │
     │  4. Request WiFi scan                     │
     │  [00 00 02 00 01 02]                      │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  5. Scan results (multiple notifications) │
     │ <─────────────────────────────────────────│
     │  (type=0x00, sub=0x10, one per network)   │
     │                                           │
     │  6. Provision credentials                 │
     │  [01 00 len BSSID sec pass_len pass]      │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  7. Confirmation (type=0x02)              │
     │ <─────────────────────────────────────────│
     │                                           │
     │  8. Activate WiFi                         │
     │  [06 00 00 00]                            │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  9. WiFi Connected Info (type=0x07)       │
     │ <─────────────────────────────────────────│
     │     (SSID, BSSID, IP, Mask, Gateway)      │
     │                                           │
```

---

## Sentinel Values Reference

| Value | Context | Meaning |
|-------|---------|---------|
| `0x03E8` (1000) | Current temp field | Probe not connected |
| `0x1000` (4096) | Set temp field | No target temperature configured |
| `0x10` | Alarm byte (offset 4) | Alarm not firing (normal state) |
| `0x00` | Alarm byte (offset 4) | Alarm actively sounding |

---

## Verified Through Live Testing

The following was confirmed by connecting to a live Gravity980 device:

- BLE scan finds device advertising as `Gravity980-XX:XX` or `BLEWIFI APP`
- BLE connection succeeds; only custom service (0xAAAA) is visible
- Activate command `[06 00 00 00]` works without authentication if credentials are stored
- PIN authentication via `[00 19 04 00 FF 05 pin_hi pin_lo]` required only for first-time setup
- WiFi scan via `[00 00 02 00 01 02]` returns available networks
- Credential provisioning via `[01 00 len BSSID sec pass_len pass]`
- Writing `[05 00 00 00]` disconnects WiFi, device returns to BLE-only mode
- TCP connection to port 3333 immediately returns 16-byte status
- Device stops sending TCP data within ~1 second of power loss (detectable via timeout)
- Each status field was individually verified by changing one setting at a time on the physical device
