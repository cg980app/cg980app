# Char-Griller Control Protocol — BLE WiFi Bootstrap, Status & Control

## Overview

Char-Griller WiFi controllers (the **Gravity 980** and **Auto Akorn**) are grill/smoker
temperature controllers with three temperature probes, a fan, an alarm, and — on the
Gravity 980 — a door sensor. They use Bluetooth Low Energy (BLE) as a bootstrap mechanism
to bring the device onto a WiFi network. Once on WiFi, the device exposes a TCP service on
**port 3333** that streams real-time status and accepts control commands.

The BLE interaction is brief: the client connects, sends a command to activate WiFi using
stored credentials, receives the device's network information (IP address, SSID, etc.), and
then disconnects BLE. All subsequent status monitoring and control occurs over WiFi (though
commands can also be sent over BLE while connected — see [Control Commands](#control-commands)).

Both models share the same controller board; their differences are summarized in
[Device Differences](#device-differences).

**Device Hardware**: Opulinks BLE+WiFi SoC (OPL-series chip)
**BLE Advertised Name**: `Gravity980-XX:XX` or `Akorn-XX:XX` (last 4 of MAC). The Akorn may
briefly advertise `Gravity` on boot.
**BLE GATT Device Name**: `BLEWIFI APP`
**MAC Address (observed)**: `88:4A:18:XX:XX:XX` (Opulinks OUI)

> Findings here were verified by live hardware testing and by analysis of the official
> Char-Griller Android client (`com.jdkj.grill`, no longer available from the manufacturer).

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

The client should request an MTU exchange after service discovery. The device will respond
with its maximum MTU of **247 bytes**.

---

## GATT Service Structure

### Custom WiFi Control Service

| Field | Value |
|-------|-------|
| Service UUID (16-bit) | `0xAAAA` |
| Full UUID | `0000aaaa-0000-1000-8000-00805f9b34fb` |

> **Note**: On some platforms (e.g., macOS/iOS CoreBluetooth), only the custom service is
> visible. The standard GAP and GATT services may be hidden by the OS.

#### Characteristics

| Characteristic | UUID | Full UUID | Properties | Description |
|----------------|------|-----------|------------|-------------|
| Command (TX) | `0xBBB0` | `0000bbb0-0000-1000-8000-00805f9b34fb` | Write, Write Without Response | Client writes commands here |
| Response (RX) | `0xBBB1` | `0000bbb1-0000-1000-8000-00805f9b34fb` | Notify | Device sends responses/status here |

**CCCD for Response characteristic**: Located at the descriptor handle immediately following
the Response characteristic.

The device begins sending notifications immediately upon BLE connection. Implementations
SHOULD still subscribe to notifications via the CCCD for compatibility.

---

## BLE Protocol

### Command Format (Client → Device)

WiFi-bootstrap commands are written as raw bytes to the Command characteristic (UUID
`0xBBB0`) using Write Without Response:

```
Offset  Size  Field
0       1     Command ID
1       3     Parameters (command-specific, zero-padded if unused)
```

Total command length: **4 bytes**. (Control commands for setting temperatures/fan use a
longer framing — see [Control Commands](#control-commands).)

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

**Expected response**: WiFi Connected Info notification (Message Type `0x07` on the Gravity
980, or `0x00`/`0x20` on the Auto Akorn).

---

### Command: Disconnect WiFi (ID: `0x05`)

Instructs the device to disconnect from WiFi and return to BLE-only mode. Should be sent on
application exit to allow clean reconnection in future sessions.

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

### Command: Authenticate (8 bytes)

The device requires a session-start handshake the first time it's connected (when it
displays a PIN). The PIN (shown on the device packaging or screen) is treated as a
**hexadecimal value** encoded into 2 bytes.

```
00 19 04 00 FF 05 [pin_hi] [pin_lo]
```

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 1 | Command Type | `0x00` |
| 1 | 1 | Protocol ID | `0x19` |
| 2–3 | 2 | Sub-command | `0x04 0x00` |
| 4 | 1 | Fixed | `0xFF` |
| 5 | 1 | Command ID | `0x05` |
| 6–7 | 2 | PIN | 4-digit PIN as 16-bit hex (e.g., "1234" → `0x12 0x34`) |

Example: PIN "1234" → `00 19 04 00 FF 05 12 34`

To skip authentication (device already paired/initialized), send `0xFF 0xFF` as the PIN bytes:
`00 19 04 00 FF 05 FF FF`

> This 8-byte form is the same framing used by [Control Commands](#control-commands)
> (`00 19 04 00 FF [id] [hi] [lo]`) — authentication is command id `0x05` with the PIN as its
> value. The device does **not** validate the PIN bytes in practice, so the `FF FF` form is
> what this project sends.

**Note**: Authentication is only required when the device displays a PIN on its screen
(typically on first connection). If the device has previously been paired, the activate
command (`0x06`) works without authentication. The recommended flow is: try activate first,
only authenticate if it fails.

---

### Command: Request WiFi Scan (6 bytes)

```
00 00 02 00 01 02
```

Triggers the device to scan for available WiFi networks. Results arrive as multiple
notifications over the next 3–5 seconds (see below).

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
| 10 | 1 | Security Type | Sent as `0x01` for WPA/WPA2 regardless of the value reported by the scan (scan and provisioning use different security encodings) |
| 11 | 1 | Password Length | Length of password string |
| 12 | N | Password | UTF-8 encoded password |

**Expected response**: Notification beginning with `0x02` confirming credential storage.

After provisioning, send the Activate WiFi command (`06 00 00 00`) to connect.

---

### Notification: WiFi Connected Info — Gravity 980 (Type: `0x07`)

Sent by the device after successfully connecting to WiFi. Contains all information needed to
reach the device over the network.

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

**Total length** = 24 + SSID Length bytes. All IP addresses are in **network byte order
(big-endian)**. An IP of `0.0.0.0` means the device failed to join (e.g. no stored credentials).

### Notification: WiFi Connected Info — Auto Akorn (Type: `0x00`, Sub-type: `0x20`)

The Auto Akorn reports the same information via a `0x00`/`0x20` notification instead of `0x07`:

```
[00] [20] [len_lo] [len_hi] [00] [ssid_len] [SSID...] [BSSID x6] [IP x4] [netmask x4] [gateway x4]
```

The SSID, BSSID, IP, netmask, and gateway fields parse the same way as the `0x07` form,
starting at the `ssid_len` byte (offset 5).

---

### Notification: Device Status Heartbeat (Type: `0x00`)

Sent periodically (~1 per second) and immediately upon connection. Over BLE the status is
wrapped in a 24-byte notification:

```
[00] [19] [14] [00] [20 bytes of status data]
```

Header bytes: type=`0x00`, protocol=`0x19`, length=`0x14` (20), reserved=`0x00`. The 20-byte
payload is the same [Device Status Format](#device-status-format-20-bytes) sent raw over TCP.

---

## Control Commands

Beyond monitoring, the controller accepts write commands to set chamber/probe target
temperatures, fan speed, and to silence the alarm.

### Command Format

**BLE** (write to characteristic `0xBBB0`), 8 bytes:
```
00 19 04 00 FF [id] [value_hi] [value_lo]
```

**TCP** (port 3333), 4 bytes — the same command with the `00 19 04 00` framing
header stripped:
```
FF [id] [value_hi] [value_lo]
```

Values are big-endian uint16. (Older docs claimed the TCP form was 6 bytes
starting with `04 00`. That is incorrect: the Gravity 980 silently ignores
that form. The Akorn happens to accept both, but the upstream Android client
sends the 4-byte form to both devices.)

### Command IDs

| ID | Target | Value Range | Notes |
|----|--------|-------------|-------|
| `00` | Chamber temp (Probe 1) | 200-700°F | Grill target temperature |
| `01` | Timer | 0-4096 | Minutes; 4096 = clear ⚠️ different framing — see warning |
| `02` | Probe 2 target | 50-350°F | Food probe target |
| `03` | Probe 3 target | 50-350°F | Food probe target |
| `04` | Power | varies | Power off command ⚠️ different framing — see warning |
| `05` | Silence alarm | 0 | Silences the physical grill alarm beep on the Akorn. ⚠️ On the Gravity, this id flashes "0000" on the panel and appears to be a stop/break command, not a silencer — do not send. |
| `06` | Fan speed | 0-100, or 4096 | 0=off, 1-100=%, 4096 (0x1000)=auto |

> ⚠️ **Timer (`01`) and Power (`04`) use a different, full-packet command builder** in the
> Android client, not the simple 6-byte form above. Sending them with the simple structure
> corrupts grill state — this project does not implement them.

### Examples

| Action | TCP Command |
|--------|-------------|
| Set chamber to 250°F | `FF 00 00 FA` |
| Set chamber to 330°F | `FF 00 01 4A` |
| Set probe 3 to 150°F | `FF 03 00 96` |
| Set fan to 60% | `FF 06 00 3C` |
| Set fan to auto | `FF 06 10 00` |
| Set fan off | `FF 06 00 00` |
| Silence alarm | `FF 05 00 00` |

> The Gravity 980 only honors TCP commands once it's in its higher power state
> (active grilling). In its lower/idle state TCP writes are silently dropped.
> The Akorn appears to accept commands in any state.
>
> The Gravity also accepts a narrower set of commands. Confirmed working on
> Gravity over TCP (in the higher power state):
> - **Set chamber temp (`0x00`)**, **Probe 2 target (`0x02`)**, **Probe 3 target (`0x03`)**
>
> Confirmed *not* working / unsupported on Gravity:
> - **Fan-speed (`0x06`)** and **fan-mode (`0x07`)** are silently ignored. The
>   device manages its own fan internally to track the chamber set-temp; the
>   `0x07` id appears in the v2.0.8 Android source as a Gravity-specific
>   auto/turbo toggle but the implementation looks incomplete (referenced
>   fields are never assigned), and live testing produced no observable effect.
> - **Silence (`0x05`)** flashes "0000" on the panel rather than silencing —
>   appears to be a stop/break, not a silencer. Treat as do-not-send.
>
> The Akorn honors `0x00`, `0x02`, `0x03`, `0x05`, and `0x06`.

#### Observed panel error codes

While exploring the protocol on a Gravity, we triggered three panel codes
worth knowing about:

| Code | Trigger | Notes |
|------|---------|-------|
| `0040` | Sending id `0x01` (Timer) and/or `0x04` (Power) with the simple 4-byte form | These need the full-packet builder. Sending the simple form crashed the device's WiFi side and forced re-provisioning. **Do not send `0x01` or `0x04` with the simple form.** |
| `000E` | Sending an out-of-range value to id `0x05` (silence) or `0x06` (fan) | Recoverable; firmware rejects and shows the error. |
| `0000` | Sending id `0x05` (silence) on a Gravity | See note above — appears to be a stop/break, not silence. |

### Dual transport

The official app sends commands over BLE or TCP depending on which is connected:

```java
// from com.jdkj.grill.utils.d.v()
if (bleConnected) {
    sendViaBle(command);    // full 8-byte command with 0019 04 00 header
} else if (tcpConnected) {
    sendViaTcp(command);    // 4-byte command, 00 19 04 00 header stripped
}
```

---

## WiFi TCP Protocol (Port 3333)

### Connection Behavior

Upon TCP connection to `<device IP>:3333`, the device immediately sends a **20-byte status
packet** and then streams updated 20-byte packets approximately once per second. No
authentication or handshake is required.

- **Single client**: the device accepts only **one** TCP client at a time.
- **Bidirectional**: the same connection carries status (device → client) *and*
  [control commands](#control-commands) (client → device).

To parse a stream, scan for a valid packet boundary using the alarm byte (offset 4 ∈
{`0x10`, `0x00`}) together with the reserved byte (offset 5 = `0x00`), then consume 20 bytes.

---

## Device Status Format (20 bytes)

The core status structure. Sent raw over **TCP port 3333**, and as the 20-byte payload of the
BLE status heartbeat (wrapped with a 4-byte header — see above).

> Earlier Gravity 980 firmware was documented as a 16-byte packet (offsets 0–15). The current
> packet is 20 bytes; bytes 16–19 carry fan mode/speed and were verified on the Auto Akorn.

### Field Map

| Offset | Size | Field | Sentinel / Notes | Description |
|--------|------|-------|------------------|-------------|
| 0–1 | 2 | Probe 1 Current Temp | `0x03E8` (1000) = not connected | Big-endian uint16, °F |
| 2–3 | 2 | Probe 1 Set Temp | `0x1000` (4096) = not configured | Big-endian uint16, °F |
| 4 | 1 | Alarm | `0x10` = silent, `0x00` = firing | Alarm state |
| 5 | 1 | Reserved | `0x00` | Always zero |
| 6–7 | 2 | Probe 2 Current Temp | `0x03E8` (1000) = not connected | Big-endian uint16, °F |
| 8–9 | 2 | Probe 2 Set Temp | `0x1000` (4096) = not configured | Big-endian uint16, °F |
| 10–11 | 2 | Probe 3 Current Temp | `0x03E8` (1000) = not connected | Big-endian uint16, °F |
| 12–13 | 2 | Probe 3 Set Temp | `0x1000` (4096) = not configured | Big-endian uint16, °F |
| 14 | 1 | Fan | Bitmask (see below) | Fan present/on (static `0x10` on Akorn) |
| 15 | 1 | Status Flags | Bitmask (see below) | Door + turbo (static `0x00` on Akorn) |
| 16 | 1 | Fan Mode | `0x10` = auto, `0x00` = manual | Verified on Akorn |
| 17 | 1 | Fan Speed | 0–100 (%, meaningful when manual) | Verified on Akorn |
| 18 | 1 | Unknown | Static `0x11` on Auto Akorn | — |
| 19 | 1 | Unknown | Static `0x00` on Auto Akorn | — |

### Temperature Values

- All temperatures are **big-endian unsigned 16-bit integers** representing degrees Fahrenheit.
- `0x03E8` (1000) in a current temp field means **probe not connected**.
- `0x1000` (4096) in a set temp field means **no target configured**.
- Valid temperature example: `0x00C8` = 200°F, `0x0045` = 69°F.

### Probe 1

Probe 1 is the primary/chamber probe (always connected). On the Gravity 980 the fan turns off
automatically when Probe 1 current temp reaches Probe 1 set temp.

### Alarm (Byte 4)

| Value | Meaning |
|-------|---------|
| `0x10` | Alarm not firing (normal) |
| `0x00` | Alarm active (sounding) |

### Fan (Byte 14) & Fan Mode/Speed (Bytes 16–17)

On the Gravity 980, byte 14 bit 4 (`0x10`) indicates the fan is on. On the Auto Akorn, byte 14
is static `0x10` and the meaningful fan state lives in bytes 16–17 (mode + speed). Bytes 16–17:

- Byte 16 = `0x10` → fan in **auto** mode; `0x00` → **manual**.
- Byte 17 = fan **speed** 0–100% (only meaningful in manual mode).

### Status Flags (Byte 15)

| Bit | Value | Meaning |
|-----|-------|---------|
| Bit 0 | `0x01` | Door open |
| Bit 4 | `0x10` | Fan turbo mode |

Examples: `0x00` = door closed/turbo off, `0x01` = door open, `0x10` = turbo on, `0x11` = both.
The Auto Akorn (a kamado, no door) reports a static `0x00` here.

---

## Example Status Packets

The examples below show the original 16-byte core fields; current firmware appends bytes 16–19.

### All probes connected, fan on, door closed
```
Hex: 00 69 00 E1 10 00 00 45 00 60 00 49 00 5F 10 00

Probe 1: current=105°F, set=225°F
Probe 2: current=69°F, set=96°F
Probe 3: current=73°F, set=95°F
Alarm: silent (0x10)   Fan: on (0x10)   Door: closed, turbo off (0x00)
```

### Probes 2+3 disconnected, device idle
```
Hex: 00 69 00 C8 10 00 03 E8 10 00 03 E8 10 00 00 00

Probe 1: current=105°F, set=200°F
Probe 2: NOT CONNECTED (1000), set NOT CONFIGURED (4096)
Probe 3: NOT CONNECTED (1000), set NOT CONFIGURED (4096)
Alarm: silent (0x10)   Fan: off (0x00)   Door: closed (0x00)
```

### Alarm firing, door open, fan turbo
```
Hex: 00 69 00 C8 00 00 03 E8 10 00 00 47 00 5F 10 11

Alarm: FIRING (0x00)   Fan: on (0x10)   Status: door open + turbo (0x11)
```

---

## Communication Sequence

```
┌──────────┐                              ┌──────────────┐
│  Client  │                              │ Char-Griller │
└────┬─────┘                              └──────┬───────┘
     │                                           │
     │  1. BLE Scan (find "Gravity980-*",        │
     │     "Akorn-*", or "BLEWIFI APP")          │
     │                                           │
     │  2. BLE Connect                           │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  3. Status Notifications begin (~1/sec)   │
     │ <─────────────────────────────────────────│
     │                                           │
     │  4. Subscribe to notifications (0xBBB1)   │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  5. Try activate: [06 00 00 00]           │
     │ ─────────────────────────────────────────>│
     │                                           │
     │  6a. If stored creds work → WiFi Info     │
     │ <─────────────────────────────────────────│
     │      (Done! Skip to TCP)                  │
     │  6b. If no creds → no response / 0.0.0.0  │
     │      → run provisioning flow (below)      │
     │                                           │
     │  7. (optional) Disconnect BLE             │
     │  8. TCP Connect to <IP>:3333              │
     │ ═════════════════════════════════════════>│
     │  9. Receive 20-byte status (immediate),   │
     │     then continuous stream (~1/sec)       │
     │ <═════════════════════════════════════════│
     │ 10. (optional) send control commands      │
     │ ═════════════════════════════════════════>│
```

### Provisioning Sequence (first-time setup only)

Needed only when the device has no stored WiFi credentials.

```
1. Authenticate            [00 19 04 00 FF 05 pin_hi pin_lo]   (FF FF to skip)
2. Try stored credentials  [06 00 00 00]
   → if WiFi Info with valid IP: done
   → if IP = 0.0.0.0 / no response: continue
3. Request WiFi scan       [00 00 02 00 01 02]
4. Receive scan results    (type 0x00 / sub 0x10, one per network)
5. Provision credentials   [01 00 len BSSID 01 pass_len password]
6. Confirmation            (notification beginning 0x02)
7. Activate WiFi           [06 00 00 00]
8. WiFi Connected Info      (0x07 on Gravity, 0x00/0x20 on Akorn)
```

---

## Implementation Guide

### Step 1: BLE Discovery
1. Scan for BLE peripherals named `Gravity980-*`, `Akorn-*`, or `BLEWIFI APP`, or advertising
   service UUID `0xAAAA`.
2. The device uses Opulinks OUI `88:4A:18:xx:xx:xx`.
3. The advertised name may alternate between the model name and `BLEWIFI APP` depending on
   firmware state; the Akorn may briefly show `Gravity` on boot.

### Step 2: Connect and Discover Services
1. Establish the BLE connection.
2. Find service `0000aaaa-0000-1000-8000-00805f9b34fb`.
3. Find characteristics — Write: `0000bbb0-...`, Notify: `0000bbb1-...`.
4. Subscribe to notifications on the Notify characteristic (write `0x01 0x00` to its CCCD).
5. Request an MTU exchange (recommend 247+ bytes).

### Step 3: Activate WiFi
1. Write `[0x06, 0x00, 0x00, 0x00]` to the Write characteristic (Write Without Response).
2. Wait for a WiFi Connected Info notification (`0x07`, or `0x00`/`0x20` on Akorn).
3. Valid IP → proceed to TCP. No response or IP `0.0.0.0` → run the provisioning flow.

### Step 4: Transition to WiFi
1. Disconnect BLE (optional — the device stays on WiFi regardless).
2. Open a TCP socket to `<parsed IP>:3333`.
3. Read 20-byte status packets continuously.

### Step 5: Parse Status
Use the 20-byte [Device Status Format](#device-status-format-20-bytes) to extract the three
probe temperatures (current + set), alarm, fan, door/turbo, and fan mode/speed.

---

## Device Differences

| Feature | Gravity 980 | Auto Akorn |
|---------|-------------|------------|
| BLE name | `Gravity980-XX:XX` | `Akorn-XX:XX` (briefly `Gravity` on boot) |
| WiFi info packet | Type `0x07` | Type `0x00` subtype `0x20` |
| Fan byte (14) | Reports actual state | Static `0x10` |
| Door byte (15) | Reports door open/closed | Static `0x00` (kamado, no door) |
| Fan mode (byte 16) | Untested | Verified: `0x10`=auto, `0x00`=manual |
| Fan speed (byte 17) | Untested | Verified: 0–100% |

Both use the same controller board and the same command set.

---

## Sentinel Values Reference

| Value | Context | Meaning |
|-------|---------|---------|
| `0x03E8` (1000) | Current temp field | Probe not connected |
| `0x1000` (4096) | Set temp field | No target temperature configured |
| `0x10` | Alarm byte (offset 4) | Alarm not firing (normal state) |
| `0x00` | Alarm byte (offset 4) | Alarm actively sounding |
| `0x1000` (4096) | Fan command value | Fan set to auto |

---

## Hardware Notes

- **WiFi/BLE chip**: Opulinks (OUI `88:4A:18`). The BLE MAC and WiFi MAC differ slightly
  (same OUI, different middle bytes).
- **TCP port 3333**: accepts only one TCP client at a time (single-client limitation).
- **BLE**: supports multiple simultaneous GATT clients despite documentation suggesting otherwise.
- **TCP is bidirectional**: both status streaming and control commands share the one connection.

---

## Verified Through Live Testing

Confirmed against live Gravity 980 and Auto Akorn hardware:

- BLE scan finds the device advertising as `Gravity980-XX:XX`, `Akorn-XX:XX`, or `BLEWIFI APP`.
- BLE connection succeeds; only the custom service (`0xAAAA`) is visible on some platforms.
- Activate command `[06 00 00 00]` works without authentication when credentials are stored.
- PIN authentication via `[00 19 04 00 FF 05 pin_hi pin_lo]` is required only for first-time setup.
- WiFi scan via `[00 00 02 00 01 02]` returns available networks; provisioning via
  `[01 00 len BSSID 01 pass_len password]` stores them.
- Writing `[05 00 00 00]` disconnects WiFi; the device returns to BLE-only mode.
- TCP connection to port 3333 immediately returns a status packet, streaming ~1/sec.
- Control commands (`FF [id] [hi] [lo]`) set chamber/probe targets over the same TCP connection
  on both models. Fan speed and silence work on the Akorn; on the Gravity, fan
  is internal-only and silence (`0x05`) is not actually a silencer (see Control
  Commands section above).
- The device stops sending TCP data within ~1 second of power loss (detectable via timeout).
- Each status field was individually verified by changing one setting at a time on the device.

## Source

Protocol details were reverse-engineered from live hardware testing and from analysis of the
official Char-Griller Android client (`com.jdkj.grill`, no longer available from the
manufacturer). Key findings from that client:

- The command builder constructs the hex string from header, command ID, and value.
- The send method routes via BLE or TCP depending on connection state.
- The TCP path strips the `0019` header before sending.
