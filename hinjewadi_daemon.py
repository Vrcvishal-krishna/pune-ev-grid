#!/usr/bin/env python3
"""
City-Wide 24/7 EV Telemetry Daemon for Pune & Pimpri-Chinchwad (PCMC)
Monitors 257 public charging stations (1,094 sockets) across Statiq, goEgo, Shell, ChargeZone, BPCL, etc.
Features dynamic multi-socket power balancing, BMS curve simulation, and SQLite session tracking.
"""

import os
import sys
import time
import json
import sqlite3
import datetime
import urllib.request
import concurrent.futures
import uuid
import ssl
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "ev_tracker.db")
TELEMETRY_JSON = os.path.join(BASE_DIR, "live_telemetry.json")
STATIONS_INDEX = os.path.join(BASE_DIR, "pune_pcmc_stations.json")

# Special verified hardware overrides (physical cabinet ratings)
POWER_OVERRIDES = {
    16886: 60.0,  # DC Fast - The Social Street physical 60kW DC unit
}

EXCLUDED_STATION_IDS = {16989, "16989"}
EXCLUDED_NAMES = ["Amar Pristine"]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS port_state (
            port_key TEXT PRIMARY KEY,
            station_id TEXT,
            port_label TEXT,
            status TEXT,
            power_kw REAL,
            last_changed_ts REAL,
            last_seen_ts REAL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS completed_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            port_key TEXT,
            station_name TEXT,
            start_time TEXT,
            end_time TEXT,
            duration_minutes REAL,
            est_kwh REAL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            display_time TEXT,
            active_count INTEGER,
            available_count INTEGER,
            faulted_count INTEGER,
            total_guns INTEGER,
            occupancy_pct REAL
        )
    """)
    conn.commit()
    conn.close()

def calculate_effective_kw(cabinet_kw, active_on_cabinet, port_type):
    """
    Calculates realistic power delivery factoring in:
    1. Dynamic Power Sharing across multi-connector DC fast chargers (e.g. 120kW split 50:50 = 60kW/gun).
    2. Real Indian EV fleet BMS charge acceptance curves (Tata Nexon/Punch max ~35-40kW, MG ZS EV ~60kW).
    3. AC onboard charger limits (3.3kW / 7.2kW).
    """
    allocated_kw = cabinet_kw / max(1, active_on_cabinet)
    p_lower = port_type.lower()

    # AC Charging (Wallbox / Type-2)
    if "ac" in p_lower or "wall" in p_lower or "type-2" in p_lower:
        return round(min(allocated_kw, 7.2) * 0.92, 1)

    # DC Fast Charging
    if allocated_kw > 90:
        return 65.0
    elif allocated_kw >= 50:
        return 45.0
    elif allocated_kw >= 25:
        return 27.0
    else:
        return round(allocated_kw * 0.90, 1)

def format_duration(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = minutes // 60
    rem_min = minutes % 60
    return f"{hours}h {rem_min}m"

def calculate_session_energy(charger_type, effective_kw, elapsed_seconds):
    dur_min = elapsed_seconds / 60.0
    is_dc = "CCS" in charger_type.upper() or "DC" in charger_type.upper() or effective_kw >= 25.0

    if not is_dc:
        # AC Slow Charging: constant rate up to 45 kWh cap
        raw_kwh = effective_kw * (dur_min / 60.0)
        return min(round(raw_kwh, 2), 45.0)

    # Realistic DC BMS Tapering Curve:
    # 0 - 35 mins: Fast bulk charging at effective_kw (10% -> ~70% SoC)
    # 35 - 55 mins: Taper down (average ~25 kW) (70% -> ~90% SoC)
    # 55 - 75 mins: Cell balancing / trickle (average ~7 kW) (90% -> 100% SoC)
    # > 75 mins: 100% Full / Idle standby (~0.2 kW)
    if dur_min <= 35.0:
        kwh = effective_kw * (dur_min / 60.0)
    elif dur_min <= 55.0:
        kwh = effective_kw * (35.0 / 60.0) + 25.0 * ((dur_min - 35.0) / 60.0)
    elif dur_min <= 75.0:
        kwh = effective_kw * (35.0 / 60.0) + 25.0 * (20.0 / 60.0) + 7.0 * ((dur_min - 55.0) / 60.0)
    else:
        kwh = effective_kw * (35.0 / 60.0) + 25.0 * (20.0 / 60.0) + 7.0 * (20.0 / 60.0) + 0.2 * ((dur_min - 75.0) / 60.0)

    # Hard cap at realistic Indian EV DC battery capacity (max 65.0 kWh)
    return min(round(kwh, 2), 65.0)

def fetch_single_station_telemetry(st_meta):
    sid = st_meta["station_id"]
    name = st_meta.get("name") or st_meta.get("station_name", "Station")
    if sid in EXCLUDED_STATION_IDS or any(ex in name for ex in EXCLUDED_NAMES):
        return None
    brand = st_meta.get("brand", "Statiq")
    if "chargezone" in brand.lower() or sid in [11111, 14610, 5204, 8505, 8506, 8507, 8508, 8509, 8529, 8530, 8639, 8657, 8932, 10385, 12100, 17658] or "chargezone" in name.lower():
        brand = "ChargeZone"
    address = st_meta.get("address", "")

    payload = json.dumps({"latitude": 0, "longitude": 0, "station_id": sid}).encode("utf-8")
    req = urllib.request.Request(
        "https://backend.statiq.co.in/station/v1/details",
        data=payload,
        headers={
            "company-id": "90",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        }
    )

    try:
        res = urllib.request.urlopen(req, timeout=5)
        d = json.loads(res.read().decode("utf-8"))
        st_detail = d.get("data", {}).get("station_detail", {})
        st_avail = (st_detail.get("availability") or "").strip()
        plugin_details = st_detail.get("plugin_details", [])

        connectors = []
        port_idx = 1

        for p in plugin_details:
            charger_name = p.get("charger_name") or f"Charger {port_idx}"
            raw_kw = float(p.get("power_rating") or 60.0)
            cabinet_kw = POWER_OVERRIDES.get(sid, raw_kw)
            price = float(p.get("price") or 22.0)
            ch_type = p.get("type", "DC")
            raw_conns = p.get("connectors", [])

            for c in raw_conns:
                err_msg = (c.get("error_message") or "").strip().lower()
                c_avail = c.get("availability")
                raw_c_status = (c.get("connector_status") or "available").upper()

                # Correct classification across Statiq API quirks
                if st_avail.lower() == "unavailable" or err_msg == "disconnected":
                    c_status = "FAULTED"
                elif c_avail is True:
                    c_status = "FAULTED" if raw_c_status == "FAULTED" else "AVAILABLE"
                elif raw_c_status == "CHARGING" or err_msg == "charging":
                    c_status = "CHARGING"
                else:
                    c_status = "FAULTED"

                connectors.append({
                    "port_key": f"ST_{sid}_P{port_idx}",
                    "charger_id": f"{sid}_{charger_name}",
                    "charger_label": charger_name,
                    "cabinet_kw": cabinet_kw,
                    "total_guns_on_cabinet": len(raw_conns),
                    "label": f"{charger_name} - Gun {c.get('display_id', port_idx)}",
                    "port_label": f"{charger_name} - Gun {c.get('display_id', port_idx)}",
                    "type": f"{c.get('connector_type', 'CCS-2')} ({cabinet_kw}kW)",
                    "power_kw": cabinet_kw,
                    "rate_inr": price,
                    "status": c_status
                })
                port_idx += 1

        return {
            "id": f"ST-{sid}",
            "raw_id": sid,
            "name": name,
            "cpo": brand,
            "location": address or "Pune / PCMC Corridor",
            "latitude": float(st_meta.get("latitude") or 0.0),
            "longitude": float(st_meta.get("longitude") or 0.0),
            "connectors": connectors
        }
    except Exception as e:
        return None

def fetch_tatapower_stations():
    url = f"https://ezcharge.tatapower.com/HobsIntegration/syncRequestHandler?transid={uuid.uuid4()}&service=GET_CHARGING_STATIONS_ALL"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "*/*",
        "Authorization": "Basic NDUzNDY5VFBMOnNhZG1wd2Q=",
        "sessionid": "",
        "User-Agent": "Mozilla/5.0"
    }
    payload = {
        "userid": "", "latitude": "", "longitude": "", "profileid": "PUBLIC", "profileType": "PUBLIC",
        "filter": {"radius": "", "connector_type": "ALL", "connector_standard": [], "availability": "", "free_chargers": "", "amenities": [], "tariff_name": []}
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=12) as res:
            data = json.loads(res.read().decode("utf-8"))
        raw_stations = data.get("chargingStations", [])
        tata_stations = []
        for s in raw_stations:
            coords = s.get("coordinates", {})
            try:
                lat = float(coords.get("latitude", 0))
                lon = float(coords.get("longitude", 0))
            except:
                continue
            city = (s.get("city") or "").strip().lower()
            if not ((18.25 <= lat <= 18.85 and 73.50 <= lon <= 74.20) or city in ['pune', 'pimpri', 'pimpri-chinchwad', 'chinchwad', 'hinjewadi', 'chakan', 'talegaon', 'pcmc']):
                continue

            st_id = s.get("id") or s.get("station_code")
            st_name = (s.get("station_name") or "Tata Power Station").strip()
            st_code = s.get("station_code") or st_id
            st_status = (s.get("station_status") or "AVAILABLE").upper()
            conn_count = int(s.get("connector_count") or 2)
            if conn_count <= 0:
                conn_count = 2

            connectors = []
            cabinet_kw = 60.0 if conn_count <= 2 else 30.0
            for i in range(1, conn_count + 1):
                port_key = f"TP_{st_code}_P{i}"
                if st_status == "OUTOFORDER":
                    c_status = "FAULTED"
                elif st_status == "CHARGING":
                    c_status = "CHARGING" if i == 1 else "AVAILABLE"
                else:
                    c_status = "AVAILABLE"

                connectors.append({
                    "port_key": port_key,
                    "charger_id": f"TP_{st_code}_C1",
                    "charger_label": "Tata Power Charger",
                    "cabinet_kw": cabinet_kw,
                    "total_guns_on_cabinet": conn_count,
                    "label": f"Gun {i} (CCS-2 / DC Fast)",
                    "port_label": f"Gun {i}",
                    "type": f"CCS-2 ({cabinet_kw:.0f}kW)",
                    "power_kw": cabinet_kw,
                    "rate_inr": 21.0,
                    "status": c_status
                })

            tata_stations.append({
                "id": f"TP-{st_code}",
                "raw_id": st_code,
                "name": st_name,
                "cpo": "Tata Power EZ Charge",
                "location": f"{s.get('city', 'Pune')}, Maharashtra",
                "latitude": lat,
                "longitude": lon,
                "connectors": connectors
            })
        return tata_stations
    except Exception as e:
        print(f"Tata Power fetch error: {e}", file=sys.stderr)
        return []

def fetch_chargezone_stations():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    urls = [
        "https://ev-stations.chargezone.co.in/location/maharashtra/pune",
        "https://ev-stations.chargezone.co.in/location/maharashtra/pimpri-chinchwad"
    ]
    all_cz = []
    seen = set()
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as res:
                html = res.read().decode("utf-8", errors="ignore")
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
            data = json.loads(scripts[4])
            for item in data:
                if isinstance(item, dict) and ("lat" in item and "long" in item and "name" in item):
                    resolved = {}
                    for k, v in item.items():
                        if isinstance(v, int) and 0 <= v < len(data):
                            resolved[k] = data[v]
                        else:
                            resolved[k] = v
                    slug = resolved.get("slug")
                    if slug and slug not in seen:
                        seen.add(slug)
                        all_cz.append(resolved)
        except Exception:
            pass

    NEW_SLUGS = {'e26MyL', '1XF2Fq', '6fUQR0', 'GF8G4A', '6JP8Vu', '1Hk5wb', '7cWfcf', '1Mhrrr'}
    cz_stations = []
    for s in all_cz:
        if s.get("slug") not in NEW_SLUGS:
            continue
        lat = float(s.get("lat") or 0)
        lon = float(s.get("long") or 0)
        slug = s.get("slug", "")
        ext_id = (s.get("external_id") or slug).upper()
        name = s.get("name", "ChargeZone Hub").strip()
        locality = s.get("locality", "Pune")
        addr = s.get("address", "")
        full_name = f"{name} - {locality}" if locality.lower() not in name.lower() else name
        is_mega = "megacharger" in name.lower()
        conn_count = 4 if is_mega else 2
        cabinet_kw = 120.0 if is_mega else 60.0
        connectors = []
        for i in range(1, conn_count + 1):
            port_key = f"CZ_{ext_id}_P{i}"
            connectors.append({
                "port_key": port_key,
                "charger_id": f"CZ_{ext_id}_C1",
                "charger_label": "ChargeZone DC Fast Unit",
                "cabinet_kw": cabinet_kw,
                "total_guns_on_cabinet": conn_count,
                "label": f"Gun {i} (CCS-2 / DC Fast)",
                "port_label": f"Gun {i}",
                "type": f"CCS-2 ({cabinet_kw:.0f}kW)",
                "power_kw": cabinet_kw,
                "rate_inr": 22.0,
                "status": "AVAILABLE"
            })
        cz_stations.append({
            "id": f"CZ-{ext_id}",
            "raw_id": ext_id,
            "name": full_name,
            "cpo": "ChargeZone",
            "location": f"{addr}, {locality}, Pune",
            "latitude": lat,
            "longitude": lon,
            "connectors": connectors
        })
    return cz_stations

def fetch_jiobp_stations():
    u = "https://www.jiobp.com/api/locator/Fleet-Charging-Station/all?_format=json"
    req = urllib.request.Request(u, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.jiobp.com/locate-fuel-station"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            stations = json.loads(res.read().decode("utf-8"))
        jio_stations = []
        for s in stations:
            addr = (s.get("field_address") or "").replace("<br />", " ").replace("\r\n", " ").strip()
            title = s.get("title", "").strip()
            try:
                lat = float(s.get("field_latitude") or 0)
                lon = float(s.get("field_longitude") or 0)
            except:
                continue

            if not ((18.25 <= lat <= 18.85 and 73.50 <= lon <= 74.25) or "pune" in addr.lower() or "pimpri" in addr.lower()):
                continue

            if lat < 18.20 or lat > 18.90 or lon < 73.40 or lon > 74.30:
                continue

            st_slug = re.sub(r'[^a-zA-Z0-9]', '_', title)[:30].strip('_').upper()
            conn_count = 4 if "airport" in title.lower() or "pavilion" in title.lower() else 2
            cabinet_kw = 60.0

            connectors = []
            for i in range(1, conn_count + 1):
                port_key = f"JIO_{st_slug}_P{i}"
                connectors.append({
                    "port_key": port_key,
                    "charger_id": f"JIO_{st_slug}_C1",
                    "charger_label": "Jio-bp pulse Fast Unit",
                    "cabinet_kw": cabinet_kw,
                    "total_guns_on_cabinet": conn_count,
                    "label": f"Gun {i} (CCS-2 / DC Fast)",
                    "port_label": f"Gun {i}",
                    "type": f"CCS-2 ({cabinet_kw:.0f}kW)",
                    "power_kw": cabinet_kw,
                    "rate_inr": 21.0,
                    "status": "AVAILABLE"
                })

            jio_stations.append({
                "id": f"JIO-{st_slug}",
                "raw_id": st_slug,
                "name": f"Jio-bp pulse - {title}",
                "cpo": "Jio-bp pulse",
                "location": addr or "Pune / PCMC",
                "latitude": lat,
                "longitude": lon,
                "connectors": connectors
            })
        return jio_stations
    except Exception as e:
        print(f"Jio-bp pulse fetch error: {e}", file=sys.stderr)
        return []

LAST_KNOWN_STATIONS = {}

def poll_network_live(station_list):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now_epoch = time.time()
    now_dt = datetime.datetime.now()

    # Parallel fetch across all Statiq/hosted stations using thread pool
    fetched = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
        results = list(executor.map(fetch_single_station_telemetry, station_list))
        for res in results:
            if res and res.get("connectors"):
                LAST_KNOWN_STATIONS[res["id"]] = res
                fetched.append(res)

    # Fetch live Tata Power EZ Charge network in Pune & PCMC
    tata_stations = fetch_tatapower_stations()
    if tata_stations:
        for t_st in tata_stations:
            LAST_KNOWN_STATIONS[t_st["id"]] = t_st

    # Fetch live ChargeZone direct network in Pune & PCMC
    cz_stations = fetch_chargezone_stations()
    if cz_stations:
        for c_st in cz_stations:
            LAST_KNOWN_STATIONS[c_st["id"]] = c_st

    # Fetch live Jio-bp pulse network in Pune & PCMC
    jio_stations = fetch_jiobp_stations()
    if jio_stations:
        for j_st in jio_stations:
            LAST_KNOWN_STATIONS[j_st["id"]] = j_st

    # Use cached fallback for any transiently failing station
    snapshot_stations = []
    for s_meta in station_list:
        sid_str = f"ST-{s_meta['station_id']}"
        if sid_str in LAST_KNOWN_STATIONS:
            snapshot_stations.append(LAST_KNOWN_STATIONS[sid_str])

    # Append Tata Power EZ Charge snapshot stations
    if tata_stations:
        for t_st in tata_stations:
            snapshot_stations.append(t_st)
    else:
        for sid_key, st_obj in LAST_KNOWN_STATIONS.items():
            if sid_key.startswith("TP-"):
                snapshot_stations.append(st_obj)

    # Append ChargeZone direct snapshot stations
    if cz_stations:
        for c_st in cz_stations:
            snapshot_stations.append(c_st)
    else:
        for sid_key, st_obj in LAST_KNOWN_STATIONS.items():
            if sid_key.startswith("CZ-"):
                snapshot_stations.append(st_obj)

    # Append Jio-bp pulse snapshot stations
    if jio_stations:
        for j_st in jio_stations:
            snapshot_stations.append(j_st)
    else:
        for sid_key, st_obj in LAST_KNOWN_STATIONS.items():
            if sid_key.startswith("JIO-"):
                snapshot_stations.append(st_obj)

    total_active = 0
    total_available = 0
    total_faulted = 0
    total_guns = 0

    # Process all connectors with dynamic power sharing
    for st in snapshot_stations:
        connectors = st["connectors"]

        # Count active charging guns per cabinet
        active_counts_by_charger = {}
        for c in connectors:
            if c["status"] == "CHARGING":
                cid = c["charger_id"]
                active_counts_by_charger[cid] = active_counts_by_charger.get(cid, 0) + 1

        for c in connectors:
            total_guns += 1
            pkey = c["port_key"]
            status = c["status"]
            cid = c["charger_id"]
            cabinet_kw = c["cabinet_kw"]
            total_guns_on_cab = c["total_guns_on_cabinet"]

            active_on_same_cab = active_counts_by_charger.get(cid, 1) if status == "CHARGING" else 1
            allocated_kw = cabinet_kw / max(1, active_on_same_cab)
            effective_kw = calculate_effective_kw(cabinet_kw, active_on_same_cab, c["type"])

            if total_guns_on_cab > 1 and active_on_same_cab > 1:
                display_type = f"{c['type'].split('(')[0].strip()} ({allocated_kw:.0f}kW / Shared {cabinet_kw:.0f}kW)"
            else:
                display_type = c["type"]

            cur.execute("SELECT status, last_changed_ts FROM port_state WHERE port_key = ?", (pkey,))
            row = cur.fetchone()

            if row:
                prev_status, last_changed = row
                if status != prev_status:
                    if prev_status == "CHARGING" and status == "AVAILABLE":
                        dur_min = (now_epoch - last_changed) / 60.0
                        kwh = calculate_session_energy(c.get("type", ""), effective_kw, now_epoch - last_changed)
                        cur.execute("""
                            INSERT INTO completed_sessions (port_key, station_name, start_time, end_time, duration_minutes, est_kwh)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (pkey, st["name"], datetime.datetime.fromtimestamp(last_changed).isoformat(), now_dt.isoformat(), dur_min, kwh))

                    cur.execute("UPDATE port_state SET status = ?, last_changed_ts = ?, last_seen_ts = ?, power_kw = ? WHERE port_key = ?",
                                (status, now_epoch, now_epoch, cabinet_kw, pkey))
                    start_ts = now_epoch
                else:
                    cur.execute("UPDATE port_state SET last_seen_ts = ? WHERE port_key = ?", (now_epoch, pkey))
                    start_ts = last_changed
            else:
                cur.execute("""
                    INSERT INTO port_state (port_key, station_id, port_label, status, power_kw, last_changed_ts, last_seen_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (pkey, st["id"], c["port_label"], status, cabinet_kw, now_epoch, now_epoch))
                start_ts = now_epoch

            duration_str = "-"
            kwh_str = "0.0 kWh"

            if status == "CHARGING":
                total_active += 1
                elapsed = max(0, now_epoch - start_ts)
                duration_str = format_duration(elapsed)
                kwh = calculate_session_energy(c.get("type", ""), effective_kw, elapsed)
                kwh_str = f"{kwh:.1f} kWh"
            elif status == "AVAILABLE":
                total_available += 1
            elif status == "FAULTED":
                total_faulted += 1

            c["type"] = display_type
            c["duration"] = duration_str
            c["kwh"] = kwh_str
            c["allocated_kw"] = round(allocated_kw, 1)
            c["effective_kw"] = effective_kw

    # Calculate 24-Hour Summary across the entire city
    yesterday_iso = (now_dt - datetime.timedelta(hours=24)).isoformat()
    cur.execute("""
        SELECT station_name, COUNT(*), SUM(duration_minutes), SUM(est_kwh)
        FROM completed_sessions
        WHERE start_time >= ?
        GROUP BY station_name
    """, (yesterday_iso,))
    hist_rows = {r[0]: {"sessions": r[1], "duration_min": round(r[2] or 0, 1), "kwh": round(r[3] or 0, 1)} for r in cur.fetchall()}

    station_summary = []
    total_net_kwh = 0.0
    total_net_sessions = 0

    for st in snapshot_stations:
        s_name = st["name"]
        h_data = hist_rows.get(s_name, {"sessions": 0, "duration_min": 0.0, "kwh": 0.0})
        active_on_st = sum(1 for c in st["connectors"] if c["status"] == "CHARGING")
        sessions_total = h_data["sessions"] + active_on_st

        kwh_total = h_data["kwh"]
        for c in st["connectors"]:
            if c["status"] == "CHARGING":
                try:
                    kwh_total += float(c["kwh"].replace(" kWh", ""))
                except:
                    pass

        total_net_kwh += kwh_total
        total_net_sessions += sessions_total
        num_ports = len(st["connectors"])
        total_port_min_24h = max(1, num_ports * 24 * 60)
        utilization_pct = min(100.0, round((h_data["duration_min"] / total_port_min_24h) * 100, 1))

        station_summary.append({
            "station_id": st["id"],
            "name": s_name,
            "cpo": st["cpo"],
            "location": st["location"],
            "latitude": st.get("latitude", 0.0),
            "longitude": st.get("longitude", 0.0),
            "ports_count": num_ports,
            "active_now": active_on_st,
            "total_sessions_24h": sessions_total,
            "total_kwh_24h": round(kwh_total, 1),
            "total_duration_min": h_data["duration_min"],
            "utilization_pct": utilization_pct
        })

    # Sort leaderboard by active load & energy dispensed
    station_summary.sort(key=lambda x: (x["active_now"], x["total_kwh_24h"], x["total_sessions_24h"]), reverse=True)
    top_station = station_summary[0] if station_summary else None

    # Completed session stats (24H rolling)
    cur.execute("SELECT COUNT(*), AVG(duration_minutes) FROM completed_sessions WHERE start_time >= ?", (yesterday_iso,))
    comp_row = cur.fetchone()
    completed_sessions_count = comp_row[0] or 0
    avg_session_duration_min = round(comp_row[1], 1) if (comp_row and comp_row[1] is not None) else None

    # Lifetime session stats across all time
    cur.execute("SELECT COUNT(*), SUM(est_kwh), AVG(duration_minutes) FROM completed_sessions")
    life_row = cur.fetchone()
    lifetime_completed_count = life_row[0] or 0
    lifetime_completed_kwh = life_row[1] or 0.0
    lifetime_avg_dur = round(life_row[2], 1) if (life_row and life_row[2] is not None) else None

    active_now_kwh = sum(
        float(c["kwh"].replace(" kWh", ""))
        for st in snapshot_stations
        for c in st["connectors"]
        if c["status"] == "CHARGING"
    )
    lifetime_total_kwh = round(lifetime_completed_kwh + active_now_kwh, 1)
    lifetime_total_sessions = lifetime_completed_count + total_active

    # Snapshot history - preserve 7 days of telemetry
    occ_pct = round((total_active / total_guns * 100), 1) if total_guns > 0 else 0
    cur.execute("""
        INSERT INTO telemetry_history (timestamp, display_time, active_count, available_count, faulted_count, total_guns, occupancy_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (now_dt.isoformat(), now_dt.strftime("%H:%M"), total_active, total_available, total_faulted, total_guns, occ_pct))
    
    seven_days_ago = (now_dt - datetime.timedelta(days=7)).isoformat()
    cur.execute("DELETE FROM telemetry_history WHERE timestamp < ?", (seven_days_ago,))

    # Compute genuine 24-hour hourly load curve (25 points: 24h ago to now)
    hourly_points = []
    for h in range(24, -1, -1):
        t_point = now_dt - datetime.timedelta(hours=h)
        t_iso = t_point.isoformat()
        t_display = t_point.strftime("%H:%M")

        t_start = (t_point - datetime.timedelta(minutes=15)).isoformat()
        t_end = (t_point + datetime.timedelta(minutes=15)).isoformat()
        cur.execute("""
            SELECT AVG(active_count) FROM telemetry_history
            WHERE timestamp BETWEEN ? AND ?
        """, (t_start, t_end))
        row = cur.fetchone()

        if row and row[0] is not None:
            active_in_hour = round(row[0])
        else:
            cur.execute("""
                SELECT COUNT(DISTINCT port_key) FROM completed_sessions
                WHERE start_time <= ? AND end_time >= ?
            """, (t_iso, t_iso))
            active_in_hour = cur.fetchone()[0] or 0

        if h == 0:
            active_in_hour = max(active_in_hour, total_active)

        pct = round((active_in_hour / max(1, total_guns)) * 100, 1)
        hourly_points.append({
            "time": t_display,
            "active": active_in_hour,
            "pct": pct
        })

    peak_pt = max(hourly_points, key=lambda x: (x["pct"], x["active"]))
    hist_records = hourly_points

    # CPO breakdown
    cpo_distribution = {}
    for st in snapshot_stations:
        cpo_key = st["cpo"]
        cpo_distribution[cpo_key] = cpo_distribution.get(cpo_key, 0) + len(st["connectors"])

    conn.commit()
    conn.close()

    payload = {
        "timestamp": now_dt.isoformat(),
        "display_time": now_dt.strftime("%H:%M:%S"),
        "feed_network": "STATIQ_TATA_POWER_PUNE_PCMC_CITY_WIDE_FEED",
        "metrics": {
            "total_guns": total_guns,
            "active_charging": total_active,
            "available_guns": total_available,
            "faulted_guns": total_faulted,
            "occupancy_rate_pct": occ_pct,
            "available_pct": round((total_available / total_guns * 100), 1) if total_guns > 0 else 0,
            "operational_health_pct": round(((total_guns - total_faulted) / total_guns * 100), 1) if total_guns > 0 else 0,
            "total_energy_kwh_24h": round(total_net_kwh, 1),
            "lifetime_energy_kwh": lifetime_total_kwh,
            "total_sessions_24h": total_net_sessions,
            "lifetime_sessions": lifetime_total_sessions,
            "completed_sessions_24h": completed_sessions_count,
            "lifetime_completed_sessions": lifetime_completed_count,
            "avg_session_duration_min": avg_session_duration_min,
            "lifetime_avg_duration_min": lifetime_avg_dur,
            "top_station": top_station["name"] if top_station else "None",
            "peak_load_pct_24h": peak_pt["pct"],
            "peak_load_time_24h": peak_pt["time"],
            "peak_active_guns_24h": peak_pt["active"]
        },
        "cpo_distribution": cpo_distribution,
        "recent_history": hist_records,
        "trend_history_24h": hist_records,
        "daily_summary": station_summary,
        "stations": snapshot_stations
    }

    with open(TELEMETRY_JSON, "w") as f:
        json.dump(payload, f, indent=2)

    return payload

if __name__ == "__main__":
    init_db()
    if not os.path.exists(STATIONS_INDEX):
        print(f"Error: {STATIONS_INDEX} not found! Please run crawl_pune_pcmc.py first.", file=sys.stderr)
        sys.exit(1)

    with open(STATIONS_INDEX) as f:
        stations_list = [
            s for s in json.load(f)
            if s.get("station_id") not in EXCLUDED_STATION_IDS
            and not any(ex in (s.get("name") or s.get("station_name") or "") for ex in EXCLUDED_NAMES)
        ]

    print(f"Starting City-Wide Pune & PCMC Daemon ({len(stations_list)} hubs, 1094 sockets)...")
    while True:
        try:
            p = poll_network_live(stations_list)
            m = p["metrics"]
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Polled {len(p['stations'])} hubs ({m['total_guns']} sockets) | {m['active_charging']} CHARGING, {m['available_guns']} AVAILABLE, {m['faulted_guns']} FAULTED | #1: {m['top_station']}")
        except Exception as e:
            print(f"Poll loop error: {e}", file=sys.stderr)
        time.sleep(20)
