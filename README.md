# Pune & PCMC EV Grid Command Center ⚡

Live 24/7 telemetry monitoring grid tracking **376 public EV charging hubs (1,250 sockets)** across Pune & Pimpri-Chinchwad Metropolitan Region.

## Features
- **Networks Tracked:** Tata Power EZ Charge, Jio-bp pulse, ChargeZone, Statiq, goEgo, Shell Recharge, and BPCL.
- **Real-Time Telemetry:** Live socket statuses (`CHARGING`, `AVAILABLE`, `FAULTED`), dynamic multi-connector power sharing, and realistic Indian EV BMS charge curves.
- **City Thermal Heatmap:** Full-screen Leaflet & Google Maps integration highlighting maximum consumption hotspots.
- **24-Hour Load Curve:** Complete rolling 24-hour demand curve tracking city-wide peak load percentage, peak time, and active charging counts.

## Deployment on Render
1. Push this repository to GitHub.
2. In [Render Dashboard](https://dashboard.render.com), click **New +** -> **Web Service**.
3. Connect your GitHub repository.
4. Set:
   - **Environment:** `Python`
   - **Build Command:** *(leave empty or `pip install -r requirements.txt`)*
   - **Start Command:** `python app.py`
5. Click **Create Web Service**. Your live command center will be online in under 60 seconds!
