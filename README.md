# chargrillerd

Command-line monitor and web dashboard for Char-Griller BBQ temperature controllers. Supports the **Gravity 980** and **Auto Akorn**.

It connects to your grill over Bluetooth, gets it onto your WiFi (no phone app needed), then streams live probe/fan/door/alarm status to your terminal and a browser dashboard — and lets you set temperatures and fan speed from the dashboard.

[chargrillerd on GitHub](https://github.com/cg980app/cg980app)

![dashboard](screenshot.png)

## Requirements

- **macOS or Linux** (Windows is not supported)
- **Python 3.12+**
- **Bluetooth** on the host machine (for first-time WiFi setup)
- The **`bleak`** Python package

## Install

```bash
git clone https://github.com/cg980app/cg980app.git
cd cg980app
pip install bleak
```

## First run

Power on the grill, make sure Bluetooth is on, then run:

```bash
python3 chargrillerd.py
```

On the first run the tool walks you through getting the grill onto WiFi:

1. **Scans** over Bluetooth for nearby Char-Griller devices and lists what it finds (pick one if there's more than one).
2. **Provisions WiFi.** If the grill already has stored WiFi credentials, it just reconnects. Otherwise it scans for nearby WiFi networks, you pick yours and type the password, and it sends the credentials to the grill over Bluetooth.
3. **Connects** to the grill over WiFi and starts streaming status.
4. **Opens the dashboard** at `http://localhost:8080` and starts logging the cook.

Leave it running for the duration of your cook. Press **Ctrl+C** to stop.

After the first setup, the grill remembers your WiFi, so on later cooks you can skip Bluetooth entirely — see [Connecting on later cooks](#connecting-on-later-cooks).

## Using the dashboard

Open `http://localhost:8080` in any browser on the same network (your laptop, your phone at the grill, etc.).

- **Live temperatures** — current and target temp for each connected probe, plus fan and door state.
- **Set targets** — type a chamber or probe target right in the dashboard and it's sent to the grill.
- **Fan control** — set fan speed (Off / 5 / 20 / 40 / 60 / 80 / 100 % / Auto).
- **Temperature graph** — zoomable, pannable chart of every probe's current and target temps over time, with markers for events (fan changes, alarms, target reached).
- **Browser alarms** — when the grill alarm fires, the page plays an audible alert (selectable tone, with a mute button).
- **Sessions** — name your cooks, browse past sessions, and replay their graphs read-only.

Every reading is also saved to a CSV log at `~/.cgriller/logs/` for post-cook analysis.

## Connecting on later cooks

Once the grill is on your WiFi you usually don't need Bluetooth again.

```bash
# Find the grill on your network automatically
python3 chargrillerd.py --wifi

# ...or connect straight to its IP if you know it (fastest)
python3 chargrillerd.py --ip 192.168.1.50
```

If a cook gets interrupted, resume it and keep the same graph/log:

```bash
python3 chargrillerd.py --resume ~/.cgriller/logs/session_20250529_141500.csv
```

## Command-line options

All options have sensible defaults — running with no flags is the normal path. Defaults can also be edited in the `CONFIG` block at the top of `cgriller/config.py`; command-line flags override them.

### Connecting to the grill

| Option | Description |
|--------|-------------|
| *(none)* | Auto-detect and connect over Bluetooth, provisioning WiFi if needed (default) |
| `--wifi` | Skip Bluetooth and find the grill on the local network |
| `--bluetooth` | Monitor entirely over Bluetooth — never connect to WiFi |
| `--ip DEVICE_IP` | Connect straight to a known grill IP, skipping all discovery |
| `--disconnect` | Tell the grill to drop off WiFi, then exit |
| `--device {auto,gravity,akorn}` | Force the device type instead of auto-detecting (default: `auto`) |
| `--adapter HCI` | Bluetooth adapter to use, e.g. `hci1` (multi-adapter Linux hosts) |
| `--scan-timeout SECS` | How long to scan over Bluetooth (default: `8`) |
| `--resume FILE` | Resume a previous cook from its CSV log |

`--wifi`, `--bluetooth`, `--ip`, and `--disconnect` are mutually exclusive.

### Web dashboard

| Option | Description |
|--------|-------------|
| `--port PORT` | Dashboard port (default: `8080`) |
| `--host ADDRESS` | Bind address (default: `0.0.0.0`, all interfaces). Use `127.0.0.1` to restrict the dashboard to this machine only |
| `--open` | Open the dashboard in your default browser at startup |
| `--max-temp DEGREES` | Graph Y-axis maximum (default: `500`) |

### Alarms

Every alarm is raised in the **browser dashboard** — an audible tone plus a banner you can mute or acknowledge. There are no desktop or phone notifications; keep the dashboard open in a tab. Alarms fire when the chamber drifts out of range, a food probe reaches its target, the grill's own alarm sounds, or the connection is lost.

The **alarm tone** and the **chamber drift range** are chosen right in the dashboard UI (drop-down menus next to the Mute button) and are remembered per-browser. Acknowledging an alarm (clicking the banner) snoozes all alarms for 5 minutes.

### Other

| Option | Description |
|--------|-------------|
| `--debug` | Print raw Bluetooth packets (troubleshooting) |
| `--version` | Print the version and exit |

### Examples

```bash
# Quiet local-only run, auto-open the browser
python3 chargrillerd.py --host 127.0.0.1 --open

# Monitor over Bluetooth only
python3 chargrillerd.py --bluetooth
```

## Device support

| | Gravity 980 | Auto Akorn |
|---|---|---|
| Probes | 3 | 3 |
| Set-temp control (chamber + 2 food probes) | Yes | Yes |
| Fan control | Status only — device manages internally | Yes (mode + speed) |
| Silence command | No (id `0x05` isn't a silencer on Gravity) | Yes |
| Door sensor | Yes | No (kamado — no door) |

Both are auto-detected; use `--device` only if detection picks the wrong one.

## Troubleshooting

- **Grill not found over Bluetooth** — make sure it's powered on and within range, Bluetooth is enabled on the host, and try a longer `--scan-timeout`. On Linux with multiple adapters, pick one with `--adapter`.
- **Found over Bluetooth but never connects on WiFi** — double-check the WiFi password; some grills won't join 5 GHz networks (use 2.4 GHz).
- **Dashboard won't load from another device** — make sure you're not using `--host 127.0.0.1` (that restricts it to the host machine), and that the port isn't blocked by a firewall.
- **Want to see what's happening on the wire** — run with `--debug`.

## Credits

chargrillerd reverse-engineered the Char-Griller BLE/WiFi monitoring protocol; the control command set was derived from analysis of the official Android client (no longer available from the manufacturer). Protocol details live in [`protocol.md`](protocol.md).

Much of this code was written with the help of [Claude Code](https://claude.ai/code).
