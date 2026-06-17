"""Session history: live readings, event detection, CSV persistence, SSE signal."""

import csv
import json
import threading
import time
from pathlib import Path

from .config import Settings
from .protocol import DeviceStatus
from .storage import LOG_DIR, ensure_cache_dir


class StatusHistory:
    """Thread-safe storage of historical status readings with CSV persistence."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.entries: list[dict] = []
        self.events: list[dict] = []
        self.current: DeviceStatus | None = None
        self.previous: DeviceStatus | None = None
        self.start_time = time.time()
        self.notified_targets: set[str] = set()
        # SSE push: bumped on every new reading/event so streaming clients
        # wake immediately instead of polling. Uses its own lock (not self.lock)
        # so waiting stream threads never block data updates.
        self.condition = threading.Condition()
        self.version = 0
        self.shutdown_flag = False  # set on graceful exit so SSE loops drop out

        # CSV log file
        ensure_cache_dir()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.session_name = f"session_{timestamp}"
        self.csv_path = LOG_DIR / f"{self.session_name}.csv"
        self.csv_file = open(self.csv_path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([
            "elapsed_sec", "timestamp",
            "probe1_cur", "probe1_set", "probe2_cur", "probe2_set",
            "probe3_cur", "probe3_set",
            "fan", "turbo", "door", "alarm",
            "fan_auto", "fan_speed"
        ])

    def add(self, status: DeviceStatus):
        elapsed = round(time.time() - self.start_time, 1)
        entry = {
            "t": elapsed,
            "p1": status.probe1.current,
            "p1_set": status.probe1.set_temp,
            "p2": status.probe2.current,
            "p2_set": status.probe2.set_temp,
            "p3": status.probe3.current,
            "p3_set": status.probe3.set_temp,
            "fan": status.fan_on,
            "turbo": status.fan_turbo,
            "door": status.door_open,
            "alarm": status.alarm_firing,
            "fan_auto": status.fan_auto,
            "fan_speed": status.fan_speed,
        }

        with self.lock:
            self.previous = self.current
            self.current = status
            self.entries.append(entry)

        # CSV persistence
        self.csv_writer.writerow([
            elapsed, time.strftime("%H:%M:%S"),
            status.probe1.current, status.probe1.set_temp,
            status.probe2.current, status.probe2.set_temp,
            status.probe3.current, status.probe3.set_temp,
            int(status.fan_on), int(status.fan_turbo),
            int(status.door_open), int(status.alarm_firing),
            int(status.fan_auto), status.fan_speed,
        ])
        self.csv_file.flush()

        # Detect events and send notifications
        self._check_events(status, elapsed)

        # Wake any SSE stream clients — new reading (and any events) are ready
        with self.condition:
            self.version += 1
            self.condition.notify_all()

    def _check_events(self, status: DeviceStatus, elapsed: float):
        prev = self.previous
        if prev is None:
            return

        if status.door_open and not prev.door_open:
            self._add_event(elapsed, "door", "Door opened")
        elif not status.door_open and prev.door_open:
            self._add_event(elapsed, "door", "Door closed")

        if status.alarm_firing and not prev.alarm_firing:
            self._add_event(elapsed, "alarm", "Alarm firing")
        elif not status.alarm_firing and prev.alarm_firing:
            self._add_event(elapsed, "alarm", "Alarm dismissed")

        if (status.fan_auto != prev.fan_auto or status.fan_speed != prev.fan_speed
                or status.fan_on != prev.fan_on or status.fan_turbo != prev.fan_turbo):
            if status.fan_auto:
                self._add_event(elapsed, "fan", "Fan auto")
            elif status.fan_speed > 0:
                self._add_event(elapsed, "fan", f"Fan {status.fan_speed}%")
            elif status.fan_turbo:
                self._add_event(elapsed, "fan", "Fan turbo")
            elif status.fan_on:
                self._add_event(elapsed, "fan", "Fan on")
            else:
                self._add_event(elapsed, "fan", "Fan off")

        # Food probes (2 & 3) record a "reached target" chart event. The audible
        # alarm for this (and every other condition) is raised in the browser
        # dashboard; the server no longer sends any notifications of its own.
        for name, probe in [("Probe 2", status.probe2), ("Probe 3", status.probe3)]:
            if probe.connected and probe.has_target and probe.current is not None:
                key = f"{name}_{probe.set_temp}"
                if probe.current >= probe.set_temp and key not in self.notified_targets:
                    self.notified_targets.add(key)
                    self._add_event(elapsed, "target", f"{name} reached {probe.set_temp}°F")

    def _add_event(self, elapsed: float, category: str, label: str):
        with self.lock:
            self.events.append({"t": elapsed, "cat": category, "label": label})

    def get_json(self) -> str:
        with self.lock:
            return json.dumps(self.entries)

    def get_events_json(self) -> str:
        with self.lock:
            return json.dumps(self.events)

    def get_current_json(self) -> str:
        with self.lock:
            if not self.current:
                return "{}"
            return json.dumps(self._current_dict_locked())

    def _current_dict_locked(self) -> dict:
        """Build the 'cur' snapshot dict. Caller must hold self.lock."""
        s = self.current
        stats = {}
        for key, label in [("p1", "probe1"), ("p2", "probe2"), ("p3", "probe3")]:
            vals = [e[key] for e in self.entries if e[key] is not None]
            if vals:
                stats[label] = {"min": min(vals), "max": max(vals), "avg": round(sum(vals) / len(vals), 1)}
        return {
            "probe1": {"current": s.probe1.current, "set": s.probe1.set_temp, "connected": s.probe1.connected},
            "probe2": {"current": s.probe2.current, "set": s.probe2.set_temp, "connected": s.probe2.connected},
            "probe3": {"current": s.probe3.current, "set": s.probe3.set_temp, "connected": s.probe3.connected},
            "fan": s.fan_on,
            "fan_auto": s.fan_auto,
            "fan_speed": s.fan_speed,
            "turbo": s.fan_turbo,
            "door": s.door_open,
            "alarm": s.alarm_firing,
            "device": self.settings.get_profile(),
            "timestamp": time.strftime("%H:%M:%S"),
            "stats": stats,
            "session_minutes": round((time.time() - self.start_time) / 60, 1),
            "max_temp": self.settings.max_temp,
            "start_epoch": self.start_time,
        }

    def get_stream_delta(self, entry_idx: int, event_idx: int) -> tuple[str | None, int, int]:
        """SSE payload: current status + any new entries/events since the given
        indexes. The first call (idx=0) returns the full history; subsequent
        calls return only what's new. Returns (json, new_entry_idx, new_event_idx)
        — the caller passes the new indexes back on the next tick."""
        with self.lock:
            if not self.current:
                return None, entry_idx, event_idx
            cur = self._current_dict_locked()
            new_entries = self.entries[entry_idx:]
            new_events = self.events[event_idx:]
            new_entry_idx = len(self.entries)
            new_event_idx = len(self.events)
        return (
            json.dumps({"cur": cur, "hist_new": new_entries, "events_new": new_events}),
            new_entry_idx,
            new_event_idx,
        )

    def close(self):
        self.csv_file.close()

    def request_shutdown(self):
        """Wake every SSE stream loop so it exits cleanly before the server is torn down."""
        with self.condition:
            self.shutdown_flag = True
            self.condition.notify_all()

    def load_from_csv(self, csv_path: str):
        """Load historical entries from a previous session CSV file and reopen it for appending."""
        with open(csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                def parse_int(v):
                    if v == "" or v == "None":
                        return None
                    return int(v)
                entry = {
                    "t": float(row["elapsed_sec"]),
                    "p1": parse_int(row["probe1_cur"]),
                    "p1_set": parse_int(row["probe1_set"]),
                    "p2": parse_int(row["probe2_cur"]),
                    "p2_set": parse_int(row["probe2_set"]),
                    "p3": parse_int(row["probe3_cur"]),
                    "p3_set": parse_int(row["probe3_set"]),
                    "fan": row["fan"] == "1",
                    "turbo": row["turbo"] == "1",
                    "door": row["door"] == "1",
                    "alarm": row["alarm"] == "1",
                }
                with self.lock:
                    self.entries.append(entry)
        # Adjust start_time so new entries continue from where the old session left off
        if self.entries:
            last_t = self.entries[-1]["t"]
            self.start_time = time.time() - last_t
            print(f"  Resumed {len(self.entries)} readings ({last_t:.0f}s of history)")

        # Reopen the same file for appending (no header, continues the log)
        self.csv_file.close()
        self.csv_path = Path(csv_path)
        self.csv_file = open(csv_path, "a", newline="")
        self.csv_writer = csv.writer(self.csv_file)
