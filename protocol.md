# Char-Griller Control Protocol — Additional Findings

Supplements the [cg980app protocol documentation](https://github.com/cg980app/cg980app/blob/main/protocol.md). Findings below were derived from analysis of the official Android client (no longer available from manufacturer) and live hardware testing on a Char-Griller Auto Akorn.

## Control Commands

The original protocol doc covers monitoring (read-only status packets). This section documents the write commands for setting temperatures and fan speed.

### Command Format

**BLE (write to 0xBBB0):**
```
00 19 04 00 FF [id] [value_hi] [value_lo]
```

**TCP (port 3333):**
```
04 00 FF [id] [value_hi] [value_lo]
```

TCP commands strip the `00 19` header. Values are big-endian uint16.

### Command IDs

| ID | Target | Value Range | Notes |
|----|--------|-------------|-------|
| `00` | Chamber temp (Probe 1) | 200-700°F | Grill target temperature |
| `01` | Timer | 0-4096 | Minutes; 4096 = clear |
| `02` | Probe 2 target | 50-250°F | Food probe target |
| `03` | Probe 3 target | 50-250°F | Food probe target |
| `04` | Power | varies | Power off command |
| `05` | Silence alarm | 0 | Silences the physical grill alarm beep |
| `06` | Fan speed | 0-100, or 4096 | 0=off, 1-100=%, 4096 (0x1000)=auto |

### Examples

| Action | TCP Command |
|--------|-------------|
| Set chamber to 250°F | `04 00 FF 00 00 FA` |
| Set chamber to 330°F | `04 00 FF 00 01 4A` |
| Set probe 3 to 150°F | `04 00 FF 03 00 96` |
| Set fan to 60% | `04 00 FF 06 00 3C` |
| Set fan to auto | `04 00 FF 06 10 00` |
| Set fan off | `04 00 FF 06 00 00` |
| Silence alarm | `04 00 FF 05 00 00` |

### Source

Command format derived from analysis of the official Char-Griller Android client (no longer available from manufacturer). Key findings from the `com.jdkj.grill` package:
- Command builder constructs the hex string with header, probe ID, and value
- Send method routes via BLE or TCP depending on connection state
- TCP path strips the `0019` header before sending

### Dual transport

The app sends commands over BLE or TCP depending on which is connected:
```java
// from com.jdkj.grill.utils.d.v()
if (bleConnected) {
    sendViaBle(command);    // full 8-byte command with 0019 header
} else if (tcpConnected) {
    sendViaTcp(command);    // 6-byte command, header stripped
}
```

## Extended Status Packet (20 bytes)

The original protocol documents a 16-byte status packet. The actual packet is 20 bytes with 4 additional bytes:

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0-15 | 16 | (original fields) | See cg980app protocol.md |
| 16 | 1 | Fan mode | `0x10` = auto, `0x00` = manual |
| 17 | 1 | Fan speed | 0-100 (percentage, only meaningful when manual) |
| 18 | 1 | Unknown | Static `0x11` on Auto Akorn |
| 19 | 1 | Unknown | Static `0x00` on Auto Akorn |

### BLE heartbeat wrapper

Over BLE, the 20-byte status is wrapped in a 24-byte notification:
```
[00] [19] [14] [00] [20 bytes of status data]
```
Header bytes: type=0x00, protocol=0x19, length=0x14 (20), reserved=0x00.

## Auto Akorn Differences

The Auto Akorn uses the same controller board as the Gravity 980 with these differences:

| Feature | Gravity 980 | Auto Akorn |
|---------|-------------|------------|
| BLE name | `Gravity980-XX:XX` | `Akorn-XX:XX` (briefly shows `Gravity` on boot) |
| WiFi info packet | Type `0x07` | Type `0x00` subtype `0x20` |
| Fan byte (14) | Reports actual state | Static `0x10` |
| Door byte (15) | Reports door open/closed | Static `0x00` (kamado, no door) |
| Fan mode (byte 16) | Untested | Verified: 0x10=auto, 0x00=manual |
| Fan speed (byte 17) | Untested | Verified: 0-100% |

### WiFi info packet (Auto Akorn format)

When the Akorn connects to WiFi, it reports connection info via a `0x00`/`0x20` subtype notification instead of the `0x07` type used by the Gravity 980:

```
[00] [20] [len_lo] [len_hi] [00] [ssid_len] [SSID...] [BSSID x6] [IP x4] [netmask x4] [gateway x4]
```

## Hardware Notes

- **WiFi chip**: Opulink (OUI `88:4A:18`). BLE MAC and WiFi MAC differ slightly (same OUI, different middle bytes).
- **TCP port 3333**: Accepts only one TCP client at a time. Single-client limitation.
- **BLE**: Supports multiple simultaneous GATT clients despite documentation suggesting otherwise.
- **TCP is bidirectional**: Both status streaming (device -> client) and commands (client -> device) use the same TCP connection.
