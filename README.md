# RFID-Based Automated Parking Management System
**SRM Institute of Science and Technology — Minor Project (21CSP302L)**  
**Students:** Akshat Gupta (RA2311003010021) · Krish Nakul Gohel (RA2311003010920)  
**Guide:** Dr. C. Harriet Linda

---

## Quick Start (Review Demo)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the web app
python app.py

# 3. Open browser
#    http://localhost:5000
```

That's it — the dashboard loads with 10 live slots.

---

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask web server — all API routes & logic |
| `timediff.py` | TIMEDIFF billing algorithm (standalone) |
| `camera_monitor.py` | OpenCV slot detection (laptop camera) |
| `generate_qr.py` | Generates printable QR codes for all 10 slots |
| `templates/` | HTML pages (dashboard, entry, exit) |

---

## Demo Flow (for Review)

### Option A — Web Dashboard
1. `python app.py`
2. Open `http://localhost:5000`
3. Click **"Log Entry"** on any green slot → type a vehicle ID (e.g. `TN09AB1234`)
4. Click **"Process Exit"** on the occupied slot → see TIMEDIFF billing → click "Confirm & Debit"
5. Revenue counter updates in real time

### Option B — QR Code Flow (simulates mobile scan)
1. `python generate_qr.py http://YOUR_LAPTOP_IP:5000`
2. Print `qr_codes/ALL_SLOTS_OVERVIEW.png`
3. Scan any QR with phone → entry/exit page opens on phone browser

### Option C — Camera Detection
```bash
# With real camera
python camera_monitor.py --slot 1 --camera 0

# Without camera (simulation mode)
python camera_monitor.py --simulate
```

### Option D — TIMEDIFF Self-Test
```bash
python timediff.py
```

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard |
| `/api/slots` | GET | All slot states |
| `/api/slots/<id>` | GET | Single slot + billing preview |
| `/api/entry` | POST | Log vehicle entry |
| `/api/exit` | POST | Log exit + compute fee |
| `/api/log` | GET | Completed session history |
| `/scan/entry/<id>` | GET | Entry page (QR target) |
| `/scan/exit/<id>` | GET | Exit page (QR target) |

### POST /api/entry
```json
{ "slot_id": "3", "vehicle_id": "TN09AB1234" }
```

### POST /api/exit
```json
{ "slot_id": "3" }
```

---

## Architecture

```
[Driver's Phone]
     │ scan QR
     ▼
[Flask Web App]  ←──── [OpenCV Camera Monitor]
     │                        │
     │ TIMEDIFF algo          │ occupancy detection
     ▼                        ▼
[In-memory session store] ──► [Dashboard]
     │
     ▼
[Simulated FASTag Payment]
     │ (production: NPCI API)
     ▼
[Bank auto-debit]
```

## Production Upgrade Path
- QR codes → RFID/FASTag readers at gate
- Laptop camera → HC-SR04 ultrasonic sensors per slot  
- `simulate` payment → live NPCI/FASTag API call
- In-memory store → MySQL / SQLite

---
*Prototype cost estimate: ₹3,480 for 10-slot deployment*
