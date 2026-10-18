#!/usr/bin/env python3
"""
Production Runner for Pune & PCMC EV Grid Command Center
Runs the background EV telemetry poller daemon and serves the web UI on Render/Cloud.
"""

import os
import sys
import time
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

os.environ['TZ'] = 'Asia/Kolkata'
if hasattr(time, 'tzset'):
    time.tzset()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

def run_poller_daemon():
    """Background worker thread that runs continuous city-wide polling."""
    import hinjewadi_daemon
    print("[DAEMON] Initializing database and station registry...")
    hinjewadi_daemon.init_db()
    
    if not os.path.exists(hinjewadi_daemon.STATIONS_INDEX):
        print(f"[DAEMON] Error: {hinjewadi_daemon.STATIONS_INDEX} missing!", file=sys.stderr)
        return

    import json
    with open(hinjewadi_daemon.STATIONS_INDEX) as f:
        stations_list = [
            s for s in json.load(f)
            if s.get("station_id") not in hinjewadi_daemon.EXCLUDED_STATION_IDS
            and not any(ex in (s.get("name") or s.get("station_name") or "") for ex in hinjewadi_daemon.EXCLUDED_NAMES)
        ]

    print(f"[DAEMON] Poller active. Tracking {len(stations_list)} Statiq hubs + Tata Power + ChargeZone + Jio-bp pulse...")
    
    while True:
        try:
            p = hinjewadi_daemon.poll_network_live(stations_list)
            m = p["metrics"]
            print(f"[DAEMON] {p.get('display_time')} | {len(p['stations'])} hubs ({m['total_guns']} sockets) | {m['active_charging']} CHARGING | Peak 24h: {m.get('peak_load_pct_24h')}%")
        except Exception as e:
            print(f"[DAEMON] Poller loop error: {e}", file=sys.stderr)
        time.sleep(20)

class CustomHTTPHandler(SimpleHTTPRequestHandler):
    """Serve static directory with caching headers disabled for live JSON updates."""
    def end_headers(self):
        if self.path.endswith('.json'):
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        super().end_headers()

def main():
    port = int(os.environ.get("PORT", 8080))
    host = "0.0.0.0"

    # Start poller in a daemon background thread
    t = threading.Thread(target=run_poller_daemon, daemon=True)
    t.start()

    # Start production HTTP server
    print(f"[SERVER] Starting HTTP server on {host}:{port}...")
    server = ThreadingHTTPServer((host, port), CustomHTTPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down...")
        server.server_close()

if __name__ == "__main__":
    main()
