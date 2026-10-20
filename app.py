#!/usr/bin/env python3
"""
Production Runner for Pune & PCMC EV Grid Command Center
Runs the background EV telemetry poller daemon and serves the web UI on Render/Cloud.
Includes Start/Stop telemetry toggle control.
"""

import os
import sys
import time
import json
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

os.environ['TZ'] = 'Asia/Kolkata'
if hasattr(time, 'tzset'):
    time.tzset()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
STATE_FILE = os.path.join(BASE_DIR, "telemetry_state.json")

def get_telemetry_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f).get("running", False)
        except Exception:
            pass
    return False

def set_telemetry_state(running: bool):
    with open(STATE_FILE, "w") as f:
        json.dump({"running": running, "updated_at": time.time()}, f)

def run_poller_daemon():
    """Background worker thread that runs continuous city-wide polling."""
    import hinjewadi_daemon
    print("[DAEMON] Initializing database and station registry...")
    hinjewadi_daemon.init_db()
    
    if not os.path.exists(hinjewadi_daemon.STATIONS_INDEX):
        print(f"[DAEMON] Error: {hinjewadi_daemon.STATIONS_INDEX} missing!", file=sys.stderr)
        return

    with open(hinjewadi_daemon.STATIONS_INDEX) as f:
        stations_list = [
            s for s in json.load(f)
            if s.get("station_id") not in hinjewadi_daemon.EXCLUDED_STATION_IDS
            and not any(ex in (s.get("name") or s.get("station_name") or "") for ex in hinjewadi_daemon.EXCLUDED_NAMES)
        ]

    print(f"[DAEMON] Poller active. Tracking {len(stations_list)} verified hubs in Pune & PCMC...")
    
    while True:
        if not get_telemetry_state():
            time.sleep(2)
            continue

        try:
            p = hinjewadi_daemon.poll_network_live(stations_list)
            m = p["metrics"]
            print(f"[DAEMON] {p.get('display_time')} | {len(p['stations'])} hubs ({m['total_guns']} sockets) | {m['active_charging']} CHARGING | Peak 24h: {m.get('peak_load_pct_24h')}%")
        except Exception as e:
            print(f"[DAEMON] Poller loop error: {e}", file=sys.stderr)
        time.sleep(20)

class CustomHTTPHandler(SimpleHTTPRequestHandler):
    """Serve static directory with caching headers disabled and telemetry controls."""
    def do_HEAD(self):
        """Handle HEAD requests with zero body (ideal for cron-job.org and ping monitors)."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', '2')
        self.end_headers()

    def do_GET(self):
        # Zero-overhead keepalive for cron-job.org and ping monitors
        ua = self.headers.get('User-Agent', '').lower()
        if self.path in ['/health', '/ping', '/healthz', '/status'] or 'cron-job' in ua:
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'OK')
            return

        if self.path == '/api/start_telemetry':
            set_telemetry_state(True)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "started", "running": true}')
            return
        elif self.path == '/api/stop_telemetry':
            set_telemetry_state(False)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "stopped", "running": false}')
            return
        elif self.path == '/api/telemetry_status':
            is_running = get_telemetry_state()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"running": is_running}).encode('utf-8'))
            return
        super().do_GET()

    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()


def main():
    port = int(os.environ.get("PORT", 8080))
    host = "0.0.0.0"

    t = threading.Thread(target=run_poller_daemon, daemon=True)
    t.start()

    print(f"[SERVER] Starting HTTP server on {host}:{port}...")
    server = ThreadingHTTPServer((host, port), CustomHTTPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down...")
        server.server_close()

if __name__ == "__main__":
    main()
