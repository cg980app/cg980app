"""HTTP dashboard: serves the SPA, the SSE status stream, and command POSTs."""

import csv
import http.server
import json
import queue
import sys
import threading
from pathlib import Path

from .history import StatusHistory
from .protocol import CMD_IDS, build_tcp_cmd, validate_cmd_value
from .storage import LOG_DIR, list_sessions, load_sessions_meta, rename_session

_STATIC = Path(__file__).parent / "static"
DASHBOARD_HTML = (_STATIC / "dashboard.html").read_text()
SESSION_VIEW_HTML = (_STATIC / "session.html").read_text()

# Per-target gate: a device profile that sets one of these flags to False
# means the dashboard hides the control AND the server refuses to send the
# command, even if a client crafts the POST manually. Targets not listed
# here (probe1/2/3 set-temp) are accepted whenever supports_commands is True.
_TARGET_PROFILE_FLAG = {
    "silence": "supports_silence",
    "fan": "supports_fan_control",
}


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the status dashboard."""

    history: StatusHistory = None        # set by start_web_server
    cmd_queue: queue.Queue = None        # set by start_web_server

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            if self.path == "/api/command":
                target = body.get("target", "")
                value = body.get("value")
                profile = self.history.settings.get_profile()
                gate_flag = _TARGET_PROFILE_FLAG.get(target)
                if not profile.get("supports_commands", True):
                    # Device ignores TCP control commands (e.g. Gravity 980) —
                    # reject rather than silently queue an ineffective command.
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": "Device does not accept commands"}).encode())
                elif gate_flag and profile.get(gate_flag, True) is False:
                    # The device accepts commands generally, but not this one
                    # (e.g. Gravity ignores fan, and id 0x05 isn't really
                    # silence on Gravity — see protocol.md).
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": f"Device does not accept '{target}' command"}).encode())
                elif target not in CMD_IDS or value is None:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": "Invalid target or value"}).encode())
                else:
                    try:
                        ivalue = int(value)
                    except (TypeError, ValueError):
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"ok": False, "error": "Value must be an integer"}).encode())
                        return
                    err = validate_cmd_value(target, ivalue)
                    if err:
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"ok": False, "error": err}).encode())
                    else:
                        cmd = build_tcp_cmd(CMD_IDS[target], ivalue)
                        self.cmd_queue.put(cmd)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"ok": True, "sent": cmd.hex(" ")}).encode())
            elif self.path == "/api/session/rename":
                file_stem = body.get("file", "")
                label = body.get("label", "")
                if file_stem:
                    rename_session(file_stem, label)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True}).encode())
                else:
                    self.send_response(400)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception:
            self.send_response(500)
            self.end_headers()

    def do_GET(self):
        try:
            if self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(DASHBOARD_HTML.encode())
            elif self.path.startswith("/static/"):
                # Serve vendored assets (uPlot, etc.) so the dashboard works
                # offline. Path(...).name strips directory components, so a
                # request can't escape the static dir via "..".
                fpath = _STATIC / Path(self.path).name
                if not fpath.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                ctype = {
                    ".js": "application/javascript",
                    ".css": "text/css",
                    ".png": "image/png",
                    ".ico": "image/x-icon",
                }.get(fpath.suffix, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(fpath.read_bytes())
            elif self.path == "/api/stream":
                # Server-Sent Events: hold the connection open and push a
                # delta whenever StatusHistory signals new data. The first
                # message carries the full history; subsequent messages carry
                # only the entries/events added since the last tick. Requires
                # the threading server (below) so this long-lived request
                # doesn't block other endpoints like POST /api/command.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                last_version = -1
                entry_idx = 0
                event_idx = 0
                try:
                    while not self.history.shutdown_flag:
                        with self.history.condition:
                            new_data = self.history.condition.wait_for(
                                lambda: self.history.version != last_version or self.history.shutdown_flag,
                                timeout=15,
                            )
                            if self.history.shutdown_flag:
                                break
                            last_version = self.history.version
                        if new_data:
                            payload, entry_idx, event_idx = self.history.get_stream_delta(entry_idx, event_idx)
                            if payload is not None:
                                self.wfile.write(f"data: {payload}\n\n".encode())
                            else:
                                self.wfile.write(b": waiting\n\n")
                        else:
                            # 15s idle — send an SSE comment as a keepalive so a
                            # dead connection surfaces instead of hanging silently.
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # client closed the tab / navigated away
            elif self.path == "/api/status":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(self.history.get_current_json().encode())
            elif self.path == "/api/history":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(self.history.get_json().encode())
            elif self.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(self.history.get_events_json().encode())
            elif self.path == "/api/sessions":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                current = self.history.session_name
                meta = load_sessions_meta()
                current_label = meta.get(current, {}).get("label", "")
                self.wfile.write(json.dumps({
                    "current": current,
                    "current_label": current_label,
                    "sessions": list_sessions()
                }).encode())
            elif self.path.startswith("/api/session/data/"):
                name = self.path.split("/")[-1]
                csv_path = LOG_DIR / f"{name}.csv"
                if not csv_path.exists():
                    self.send_response(404)
                    self.end_headers()
                    return
                entries = []
                with open(csv_path, "r") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        def pint(v):
                            return int(v) if v and v != "None" else None
                        entries.append({
                            "t": float(row["elapsed_sec"]),
                            "p1": pint(row["probe1_cur"]),
                            "p1_set": pint(row["probe1_set"]),
                            "p2": pint(row["probe2_cur"]),
                            "p2_set": pint(row["probe2_set"]),
                            "p3": pint(row["probe3_cur"]),
                            "p3_set": pint(row["probe3_set"]),
                        })
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(entries).encode())
            elif self.path.startswith("/session/"):
                name = self.path.split("/")[-1]
                csv_path = LOG_DIR / f"{name}.csv"
                if not csv_path.exists():
                    self.send_response(404)
                    self.end_headers()
                    return
                meta = load_sessions_meta()
                label = meta.get(name, {}).get("label", name)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(SESSION_VIEW_HTML.replace("__SESSION_NAME__", name).replace("__SESSION_LABEL__", label).encode())
            else:
                self.send_response(404)
                self.end_headers()
        except OSError:
            # Client disconnected mid-response (closed the tab, EventSource
            # reconnect, etc.) — covers BrokenPipe/ConnectionReset/Aborted.
            pass

    def log_message(self, format, *args):
        pass  # suppress request logs


class _DashboardServer(http.server.ThreadingHTTPServer):
    """Threading HTTP server that swallows benign client-disconnect errors.

    SSE clients reconnect and close constantly; when one drops mid-response the
    socket raises a connection error during request teardown (outside our
    do_GET try/except). The default handler would dump a traceback for each, so
    we suppress those and only surface genuine errors."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return  # client went away — nothing to report
        super().handle_error(request, client_address)


def start_web_server(history: StatusHistory, host: str, port: int, cmd_queue: queue.Queue) -> http.server.HTTPServer:
    DashboardHandler.history = history
    DashboardHandler.cmd_queue = cmd_queue
    # ThreadingHTTPServer: each request (including long-lived SSE streams) runs
    # in its own daemon thread, so an open /api/stream connection doesn't block
    # POST /api/command or other clients.
    server = _DashboardServer((host, port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
