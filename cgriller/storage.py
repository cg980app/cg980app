"""On-disk persistence: cache directory, device cache, and session metadata."""

import json
import time
from pathlib import Path


CACHE_DIR = Path.home() / ".cgriller"
CACHE_FILE = CACHE_DIR / "device_cache.json"
LOG_DIR = CACHE_DIR / "logs"
SESSIONS_FILE = CACHE_DIR / "sessions.json"


def ensure_cache_dir():
    CACHE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)


def load_sessions_meta() -> dict:
    """Load session metadata (names, notes) from sessions.json."""
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_sessions_meta(data: dict):
    SESSIONS_FILE.write_text(json.dumps(data, indent=2))


def list_sessions() -> list[dict]:
    """List all session CSV files with metadata."""
    meta = load_sessions_meta()
    sessions = []
    for csv_file in sorted(LOG_DIR.glob("session_*.csv"), reverse=True):
        name = csv_file.stem
        stat = csv_file.stat()
        info = meta.get(name, {})
        sessions.append({
            "file": name,
            "path": str(csv_file),
            "label": info.get("label", ""),
            "size": stat.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
        })
    return sessions


def rename_session(file_stem: str, label: str):
    """Set a friendly label for a session."""
    meta = load_sessions_meta()
    if file_stem not in meta:
        meta[file_stem] = {}
    meta[file_stem]["label"] = label
    save_sessions_meta(meta)


def save_device_cache(ble_address: str, ip: str):
    ensure_cache_dir()
    data = {"ble_address": ble_address, "ip": ip, "timestamp": time.time()}
    CACHE_FILE.write_text(json.dumps(data))


def load_device_cache() -> dict | None:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None
