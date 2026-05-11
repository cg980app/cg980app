# Gravity BBQ Monitor

A command-line tool and web dashboard for monitoring a Char Griller Gravity 980 BBQ temperature controller. Connects via Bluetooth Low Energy (BLE) to bootstrap the device onto WiFi, then streams real-time probe temperatures, fan status, door state, and alarm info over TCP.

<img width="968" height="832" alt="Screenshot 2026-05-10 at 5 49 39 AM" src="https://github.com/user-attachments/assets/dc1c127c-8b7f-4061-aa73-31a70db1888b" />
(Screen shot taken after the end of smoking my latest brisket).

## Features

- **BLE WiFi provisioning** — discovers the Gravity device, scans for WiFi networks, provisions credentials, and activates the connection. No phone app needed.
- **Real-time CLI display** — live-updating terminal UI showing all probes, fan, door, and alarm state.
- **Web dashboard** — dark-themed browser UI at `http://localhost:8080` with temperature cards, historical graph, and event annotations. Accessible from any device on your local network.
- **Temperature graph** — zoomable/pannable chart with all probe current and target temps plotted over time. Event markers for door opens, fan changes, alarm triggers.
- **Session logging** — every reading is saved to CSV (`~/.gravity_bbq/logs/`) for post-cook analysis.
- **Session resume** — restart the script mid-cook and pick up where you left off, appending to the same log.
- **Alarm notifications** — persistent macOS alerts with repeating sound when probes cross thresholds or connection is lost. Won't stop until you dismiss.
- **Configurable thresholds** — per-probe low/high alarm bounds via command line, with a special `set` value that dynamically tracks the device's configured target temperature.
- **Auto-reconnect** — if WiFi drops, retries TCP for 30 seconds, then falls back to BLE re-activation.
- **Device disconnect** — dedicated command to cleanly disconnect the device from WiFi.

## Requirements

- macOS (uses CoreBluetooth via bleak, and `osascript`/`afplay` for notifications)
- Python 3.12+
- `bleak` package for BLE

```bash
pip install bleak
```

## Quick Start

```bash
# First time (or after --disconnect): connects via BLE, provisions WiFi if needed
python3 gravity_monitor.py

# Subsequent runs (device already on WiFi):
python3 gravity_monitor.py --wifi

# Or connect to a known IP directly:
python3 gravity_monitor.py --ip 192.168.0.151
```

Open `http://localhost:8080` in a browser for the web dashboard.

## Usage

```
python3 gravity_monitor.py [OPTIONS]
```

### Connection Options

| Flag | Description |
|------|-------------|
| *(default)* | Connect via BLE — discovers device, provisions WiFi if needed |
| `--wifi` | Skip BLE, scan local subnet for device already on WiFi |
| `--ip ADDRESS` | Connect directly to a known device IP |
| `--scan-timeout SECS` | BLE scan duration (default: 8s) |
| `--disconnect` | Send BLE disconnect command and exit |

### Monitoring Options

| Flag | Description |
|------|-------------|
| `--port PORT` | Web dashboard port (default: 8080) |
| `--resume FILE` | Resume a previous session CSV (appends new data to same file) |
| `--max-temp DEGREES` | Cap Y-axis scaling to prevent spikes (default: 500°F) |

### Alarm Options

| Flag | Description |
|------|-------------|
| `--alarm-sound FILE` | Custom audio file for alarms (default: `/System/Library/Sounds/Basso.aiff`) |
| `--p1-low °F` | Probe 1 low temperature alarm |
| `--p1-high °F` | Probe 1 high temperature alarm |
| `--p2-low °F` | Probe 2 low temperature alarm |
| `--p2-high °F` | Probe 2 high temperature alarm |
| `--p3-low °F` | Probe 3 low temperature alarm |
| `--p3-high °F` | Probe 3 high temperature alarm |

Threshold values can be an integer (fixed °F) or the special value `set` to dynamically track the device's configured target temperature.

### Debug

| Flag | Description |
|------|-------------|
| `--debug` | Print all BLE packets sent/received |

## Examples

```bash
# Monitor with alarms: chamber shouldn't exceed 275, meat probe targets device set temp
python3 gravity_monitor.py --p1-high 275 --p2-high set

# Use a custom alarm sound
python3 gravity_monitor.py --alarm-sound ~/Music/alarm.mp3

# Resume yesterday's cook session
python3 gravity_monitor.py --resume ~/.gravity_bbq/logs/session_20260510_140322.csv

# Disconnect the device from WiFi (e.g., before changing networks)
python3 gravity_monitor.py --disconnect

# Reconnect and provision new WiFi credentials
python3 gravity_monitor.py
# (follow prompts to select network and enter password)
```

## How It Works

### Connection Flow

1. **BLE scan** — finds devices advertising as `Gravity980-*` or `BLEWIFI APP`
2. **Auth handshake** — sends session-start command (`00 19 04 00 FF 05 FF FF`)
3. **Stored credentials check** — waits to see if device auto-connects to WiFi
4. **Provisioning** (if needed) — scans available networks, user selects one, enters password, credentials are sent to device
5. **WiFi activation** — device connects and reports its IP address
6. **TCP monitoring** — connects to port 3333, receives 16-byte status packets ~1/sec

### Status Packet (16 bytes)

| Bytes | Field |
|-------|-------|
| 0–1 | Probe 1 current temp (uint16 BE, °F) |
| 2–3 | Probe 1 set temp |
| 4 | Alarm (0x10=silent, 0x00=firing) |
| 5 | Reserved |
| 6–7 | Probe 2 current temp |
| 8–9 | Probe 2 set temp |
| 10–11 | Probe 3 current temp |
| 12–13 | Probe 3 set temp |
| 14 | Fan (bit 4: on) |
| 15 | Flags (bit 0: door open, bit 4: turbo) |

Sentinel values: `1000` = probe disconnected, `4096` = no target set.

### Data Storage

- **Session logs**: `~/.gravity_bbq/logs/session_YYYYMMDD_HHMMSS.csv`
- **Device cache**: `~/.gravity_bbq/device_cache.json` (last known BLE address + IP)

## Web Dashboard

The dashboard is served on all network interfaces — any device on your LAN can view it. Features:

- Live probe temperatures with device target display
- Alarm threshold display (when configured)
- Fan and door status cards
- Historical temperature graph with:
  - Zoom: All / 5m / 15m / 30m / 1h / 2h
  - Pan: ← / → / Latest
  - Event annotations (door, fan, alarm, target reached)
- Connection loss banner
- Auto-updates every second

## Known Limitations

- **WiFi provisioning after disconnect**: The device may need a power cycle before accepting new WiFi credentials. On subsequent runs (without power cycle), stored credentials typically work.
- **BLE on macOS**: CoreBluetooth occasionally has service discovery issues. The script handles this gracefully but some operations may need a retry.
- **Temperature units**: Fahrenheit only (no Celsius mode discovered in the device protocol).
- **Single client**: Only one BLE client can connect at a time. Multiple web dashboard viewers are fine.
