"""
gate_monitor.py  (v3.0 — Automated Gate Pipeline)
SRM Institute of Science and Technology

Full seamless flow:
  1. Webcam continuously scans for QR codes on windshields
  2. QR decoded → FASTag ID extracted
  3. EasyOCR reads license plate from same frame
  4. Plate prefix check → EV (→ slot 9/10) or Normal (→ slot 1-8)
  5. Auto-POST to /api/entry or /api/exit
  6. Opens welcome/exit page full-screen on laptop display
  7. Voice announcement via system TTS

Usage:
  python gate_monitor.py                    # live webcam
  python gate_monitor.py --camera 1         # external USB camera
  python gate_monitor.py --simulate         # no camera needed
  python gate_monitor.py --api http://192.168.1.8:5000
"""

import cv2
import numpy as np
import requests
import time
import threading
import argparse
import math
import webbrowser
import subprocess
import platform
import sys
import os
from datetime import datetime

# ── Try importing QR / OCR libs ───────────────────────────────────────────────
try:
    from pyzbar.pyzbar import decode as qr_decode
    PYZBAR_OK = True
except ImportError:
    PYZBAR_OK = False
    print("⚠️  pyzbar not found. Run: pip install pyzbar --break-system-packages")

try:
    import easyocr
    EASYOCR_OK = True
except ImportError:
    EASYOCR_OK = False
    print("⚠️  easyocr not found. Run: pip install easyocr --break-system-packages")

try:
    from federated_server import get_global_weights
    from federated_client import run_round as run_federated_round
    FL_OK = True
except Exception:
    FL_OK = False

# ── Configuration ─────────────────────────────────────────────────────────────
API_BASE        = "http://localhost:5000"
CAMERA_INDEX    = 0
COOLDOWN_SEC    = 8       # seconds before same QR can trigger again
SCAN_INTERVAL   = 0.1     # seconds between frames
OCR_INTERVAL    = 5       # run OCR every N frames (expensive)
FL_ENABLED      = True

# EV detection — plates starting with these prefixes go to slots 9/10
EV_PLATE_PREFIXES = ("EV", "ELEC", "ZEV", "BEV")

# Registered EV vehicles (FASTag → plate mapping for demo)
EV_REGISTRY = {
    "FT-EV01": "EV-TN09AB1234",
    "FT-EV02": "EV-KA01CD5678",
    "SRM-VIP-01": "TN01VIP001",
}

# ── State ─────────────────────────────────────────────────────────────────────
recent_scans   = {}   # fasttag_id → last_scan_timestamp (cooldown)
ocr_reader     = None  # lazy-loaded EasyOCR instance
gps_listener   = None

SRMIST_LAT = 12.82304
SRMIST_LON = 80.04445
GEOFENCE_RADIUS_M = 500.0


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def load_ocr():
    """Lazy-load EasyOCR (takes ~10s first time, downloads models)."""
    global ocr_reader
    if EASYOCR_OK and ocr_reader is None:
        print("🔄 Loading EasyOCR models (first run only)…")
        ocr_reader = easyocr.Reader(["en"], verbose=False)
        print("✅ EasyOCR ready")
    return ocr_reader


def _default_fl_weights() -> dict:
    return {
        "char_confidence_boost": 0.5,
        "ocr_threshold": 0.4,
        "hyphen_bonus": 0.05,
        "digit_bonus": 0.05,
        "alpha_bonus": 0.05,
    }


def _get_fl_weights() -> dict:
    if not FL_ENABLED or not FL_OK:
        return _default_fl_weights()
    try:
        w = get_global_weights()
        if isinstance(w, dict) and w:
            return {**_default_fl_weights(), **w}
    except Exception:
        pass
    return _default_fl_weights()


def _score_plate_candidate(conf: float, plate: str, weights: dict) -> float:
    alpha_ratio = sum(1 for c in plate if c.isalpha()) / max(1, len(plate))
    digit_ratio = sum(1 for c in plate if c.isdigit()) / max(1, len(plate))
    hyphen_bonus = weights["hyphen_bonus"] if "-" in plate else 0.0

    return (
        conf
        + weights["char_confidence_boost"] * 0.1
        + weights["alpha_bonus"] * alpha_ratio
        + weights["digit_bonus"] * digit_ratio
        + hyphen_bonus
    )


def is_ev_plate(plate: str) -> bool:
    """Check if a detected plate belongs to an EV."""
    plate = plate.upper().strip()
    return any(plate.startswith(p) for p in EV_PLATE_PREFIXES)


def is_ev_fasttag(fasttag_id: str) -> bool:
    """Check if FASTag is registered as an EV."""
    registered_plate = EV_REGISTRY.get(fasttag_id.upper(), "")
    return is_ev_plate(registered_plate)


def pick_slot(is_ev: bool, api_base: str) -> str:
    """
    Auto-pick best available slot.
    EV vehicles → prefer slots 9 or 10.
    Normal vehicles → prefer slots 1-8.
    Falls back to any free slot.
    """
    try:
        r = requests.get(f"{api_base}/api/slots", timeout=3)
        slots = r.json()
        free  = [s for s in slots if not s.get("is_occupied") and not s.get("occupied")]

        if is_ev:
            ev_free = [s for s in free if s["id"] in ("9", "10")]
            if ev_free:
                return ev_free[0]["id"]

        normal_free = [s for s in free if s["id"] not in ("9", "10")]
        if normal_free:
            return normal_free[0]["id"]

        # Fallback: any free slot
        if free:
            return free[0]["id"]
    except Exception as e:
        print(f"[pick_slot] Error: {e}")

    return None


def read_plate_from_frame(frame: np.ndarray) -> str:
    """
    Run EasyOCR on frame to extract license plate text.
    Returns best candidate string or empty string.
    """
    reader = load_ocr()
    if reader is None:
        return ""

    try:
        # Crop bottom-third of frame where plates usually appear
        h, w = frame.shape[:2]
        roi   = frame[int(h * 0.5):, :]

        # Preprocess: grayscale + contrast boost
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gray = cv2.convertScaleAbs(gray, alpha=1.5, beta=30)

        weights = _get_fl_weights()
        results = reader.readtext(gray, detail=1, paragraph=False)

        # Filter: plate-like strings (5-12 chars, alphanumeric)
        candidates = []
        for (bbox, text, conf) in results:
            clean = "".join(c for c in text.upper() if c.isalnum() or c == "-")
            if 5 <= len(clean) <= 12 and conf > float(weights["ocr_threshold"]):
                score = _score_plate_candidate(conf, clean, weights)
                candidates.append((score, clean))

        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
    except Exception as e:
        print(f"[OCR] Error: {e}")

    return ""


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(a)))


class MockGPSListener:
    def __init__(self, api_base: str):
        self.api_base = api_base

    def simulate_arrival(self, vehicle_id: str, slot_id: str):
        path = [
            (12.81900, 80.04050),
            (12.82020, 80.04190),
            (12.82130, 80.04310),
            (12.82220, 80.04400),
            (12.82300, 80.04440),
        ]

        for idx, (lat, lon) in enumerate(path):
            distance = _haversine_m(lat, lon, SRMIST_LAT, SRMIST_LON)
            eta = max(0, int((len(path) - idx - 1) * 30))

            if distance <= GEOFENCE_RADIUS_M:
                payload = {
                    "vehicle_id": vehicle_id,
                    "slot_id": slot_id,
                    "location": {
                        "lat": lat,
                        "lon": lon,
                        "distance_m": round(distance, 2),
                    },
                    "prediction": {
                        "arrival_eta_sec": eta,
                        "confidence": round(max(0.75, 0.95 - idx * 0.03), 2),
                    },
                    "event": "ARRIVAL_PREDICTION",
                    "campus": "SRMIST",
                }
                try:
                    requests.post(f"{self.api_base}/api/gps/arrival-prediction", json=payload, timeout=3)
                    print(f"  🛰️ Arrival prediction: {vehicle_id} within {int(distance)}m, ETA {eta}s")
                except Exception as e:
                    print(f"  [GPS] Post failed: {e}")
                break

            time.sleep(0.4)


def speak(text: str):
    """Cross-platform TTS voice announcement."""
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["say", "-r", "180", text])
        elif platform.system() == "Linux":
            subprocess.Popen(["espeak", text])
        else:
            # Windows
            subprocess.Popen(
                ["powershell", "-Command",
                 f"Add-Type -AssemblyName System.Speech; "
                 f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text}')"]
            )
    except Exception:
        pass  # voice is best-effort


def open_browser(url: str):
    """Open URL in default browser (non-blocking)."""
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()


def api_post(endpoint: str, payload: dict, api_base: str) -> dict:
    try:
        r = requests.post(f"{api_base}{endpoint}",
                          json=payload,
                          timeout=5)
        return r.json()
    except Exception as e:
        return {"success": False, "message": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# CORE: Process a detected QR scan
# ══════════════════════════════════════════════════════════════════════════════

def process_scan(fasttag_id: str, frame: np.ndarray, api_base: str):
    """
    Called when a QR code is decoded from the camera frame.
    Handles both entry and exit automatically.
    """
    now = time.time()

    # Cooldown check — prevent double-scanning
    if fasttag_id in recent_scans:
        if now - recent_scans[fasttag_id] < COOLDOWN_SEC:
            return
    recent_scans[fasttag_id] = now

    print(f"\n{'='*55}")
    print(f"  📷 QR Detected: {fasttag_id}  [{datetime.now().strftime('%H:%M:%S')}]")

    # ── Check if vehicle already has active session → EXIT flow ──────────────
    try:
        slots_r = requests.get(f"{api_base}/api/slots", timeout=3).json()
        active_slot = next(
            (s for s in slots_r
             if s.get("is_occupied") and
             (s.get("current_vehicle") == fasttag_id or
              s.get("vehicle") == fasttag_id)),
            None
        )
    except Exception:
        active_slot = None

    if active_slot:
        slot_id = active_slot.get("id") or active_slot.get("slot_id")
        print(f"  🏁 EXIT flow → Slot P{slot_id}")
        handle_exit(fasttag_id, slot_id, api_base)
        return

    # ── New vehicle → ENTRY flow ──────────────────────────────────────────────
    print(f"  🔖 ENTRY flow → scanning plate…")

    # Run plate OCR
    plate = read_plate_from_frame(frame)
    if plate:
        print(f"  🔍 Plate detected: {plate}")
    else:
        print(f"  🔍 Plate: not detected (using FASTag EV registry)")

    # Determine EV status
    ev_from_plate  = is_ev_plate(plate) if plate else False
    ev_from_fasttag = is_ev_fasttag(fasttag_id)
    is_ev = ev_from_plate or ev_from_fasttag

    print(f"  ⚡ EV: {'YES → routing to P9/P10' if is_ev else 'NO → routing to P1-P8'}")

    # Verify plate via API (LPR cross-check)
    if plate:
        lpr_r = api_post("/api/verify-plate",
                         {"vehicle_id": fasttag_id, "detected_plate": plate},
                         api_base)
        if lpr_r.get("fraud"):
            print(f"  🚨 FRAUD ALERT: Plate mismatch! {lpr_r.get('message')}")
            speak("Security alert. Plate mismatch detected. Please wait for assistance.")
            return

    # Auto-pick slot
    slot_id = pick_slot(is_ev, api_base)
    if not slot_id:
        print(f"  ❌ No free slots available")
        speak("Sorry, no parking slots are available at this time.")
        return

    print(f"  🅿️  Auto-assigned: Slot P{slot_id}")

    # POST entry
    result = api_post("/api/entry",
                      {"slot_id": slot_id, "vehicle_id": fasttag_id},
                      api_base)

    if result.get("success"):
        print(f"  ✅ Entry logged successfully")

        # Simulated FL local update after successful gate processing.
        if FL_OK and FL_ENABLED:
            try:
                run_federated_round(client_id=f"gate-{platform.node() or 'laptop'}", dataset_size=80)
            except Exception:
                pass

        # Voice announcement
        if is_ev:
            speak(f"Welcome. E.V. vehicle detected. Slot P {slot_id} assigned. "
                  f"E.V. charging surcharge of 50 rupees applies at exit.")
        elif result.get("is_vip"):
            speak(f"Welcome V.I.P. member. Slot P {slot_id} assigned. "
                  f"50 percent discount active.")
        else:
            speak(f"Welcome. Slot P {slot_id} assigned. "
                  f"Scan Q.R. again when you leave.")

        # Open welcome page on laptop screen
        open_browser(f"{api_base}/scan/entry-confirm/{slot_id}/{fasttag_id}")

        # Simulated V2X geofence arrival event (laptop-only mode).
        global gps_listener
        if gps_listener is None:
            gps_listener = MockGPSListener(api_base)
        threading.Thread(
            target=gps_listener.simulate_arrival,
            args=(fasttag_id, slot_id),
            daemon=True,
        ).start()

    elif result.get("alert") == "BLACKLIST":
        print(f"  🚨 BLACKLISTED VEHICLE: {fasttag_id}")
        speak("Entry denied. This vehicle is on the security blacklist. "
              "Please contact the parking office.")
    else:
        print(f"  ❌ Entry failed: {result.get('message')}")
        speak("Entry could not be processed. Please see the attendant.")


def handle_exit(fasttag_id: str, slot_id: str, api_base: str):
    """Process exit for a vehicle that's already parked."""
    # Get billing preview
    try:
        slot_data = requests.get(f"{api_base}/api/slots/{slot_id}", timeout=3).json()
        preview   = slot_data.get("billing_preview", {})
        fee       = preview.get("fee", 0)
        duration  = preview.get("duration_str", "")
        is_vip    = preview.get("is_vip", False)
        is_ev     = preview.get("is_ev", False)
    except Exception:
        fee, duration, is_vip, is_ev = 0, "", False, False

    print(f"  💰 Fee: ₹{fee} | Duration: {duration}")

    # Auto-process exit
    result = api_post("/api/exit", {"slot_id": slot_id}, api_base)

    if result.get("success"):
        print(f"  ✅ Exit processed. ₹{fee} debited.")
        receipt_url = result.get("session", {}).get("receipt_url", "")

        # Voice
        msg = f"Thank you. Parking fee of {fee} rupees debited from FASTag. "
        if is_ev:   msg += "E.V. charging fee included. "
        if is_vip:  msg += "V.I.P. discount applied. "
        msg += "Drive safely. Goodbye."
        speak(msg)

        # Open exit confirmation + receipt on screen
        open_browser(f"{api_base}/scan/exit/{slot_id}")
    else:
        print(f"  ❌ Exit failed: {result.get('message')}")
        speak("Exit could not be processed. Please see the attendant.")


# ══════════════════════════════════════════════════════════════════════════════
# CAMERA LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_camera(camera_index: int, api_base: str):
    """Main camera loop — reads frames, decodes QR, runs OCR periodically."""

    if not PYZBAR_OK:
        print("❌ pyzbar required for QR scanning. Run: pip install pyzbar --break-system-packages")
        return

    print(f"\n🎥 Opening camera {camera_index}…")
    cap = cv2.VideoCapture(camera_index)

    if not cap.isOpened():
        print(f"❌ Cannot open camera {camera_index}")
        return

    # Warm up EasyOCR in background
    threading.Thread(target=load_ocr, daemon=True).start()

    print("✅ Camera ready. Hold QR code in front of camera.")
    print("   Press Q to quit.\n")

    frame_count  = 0
    last_frame   = None

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(SCAN_INTERVAL)
            continue

        frame_count += 1
        last_frame   = frame.copy()

        # ── QR decode (every frame — fast) ───────────────────────────────────
        if PYZBAR_OK:
            decoded = qr_decode(frame)
            for obj in decoded:
                raw = obj.data.decode("utf-8", errors="ignore").strip()

                # Extract FASTag ID from QR data
                # QR contains URL like: http://host/scan/entry/3
                # OR direct FASTag ID like: FT-AB12
                fasttag_id = extract_fasttag(raw)
                if fasttag_id:
                    # Draw green box around QR
                    pts = obj.polygon
                    if len(pts) == 4:
                        pts_np = np.array([[p.x, p.y] for p in pts], dtype=np.int32)
                        cv2.polylines(frame, [pts_np], True, (0, 255, 0), 3)
                    cv2.putText(frame, f"QR: {fasttag_id}", (20, 40),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

                    # Process in background thread so camera keeps running
                    threading.Thread(
                        target=process_scan,
                        args=(fasttag_id, last_frame.copy(), api_base),
                        daemon=True
                    ).start()

        # ── Overlay ───────────────────────────────────────────────────────────
        cv2.putText(frame, "RFID Gate Monitor v3.0", (10, frame.shape[0] - 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 212, 255), 2)
        cv2.putText(frame, "Hold QR to camera | Press Q to quit",
                    (10, frame.shape[0] - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

        cv2.imshow("RFID Gate Monitor — SRM Parking", frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

        time.sleep(SCAN_INTERVAL)

    cap.release()
    cv2.destroyAllWindows()
    print("\n🛑 Gate monitor stopped.")


def extract_fasttag(raw: str) -> str:
    """
    Extract FASTag ID from QR data.
    Handles two formats:
      1. URL: http://host/scan/entry/3  → uses slot ID, generates FT-XXXX
      2. Direct: FT-AB12 or TN09AB1234 → uses as-is
    """
    raw = raw.strip()

    # Format 1: URL from generate_qr.py
    if "/scan/entry/" in raw or "/scan/exit/" in raw:
        # Generate a random FASTag (simulates RFID read at gate)
        import random, string
        return "FT-" + "".join(random.choices(string.digits + "ABCDEF", k=4))

    # Format 2: Direct FASTag ID
    if raw.startswith("FT-") or raw.startswith("SRM-"):
        return raw

    # Format 3: Any alphanumeric string 6-20 chars → treat as vehicle ID
    clean = "".join(c for c in raw if c.isalnum() or c in "-_")
    if 4 <= len(clean) <= 20:
        return clean.upper()

    return ""


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION MODE
# ══════════════════════════════════════════════════════════════════════════════

def run_simulation(api_base: str):
    """
    Simulates the full gate flow without a camera.
    Cycles through entry and exit scenarios including EV and VIP.
    """
    print("\n" + "="*55)
    print("  GATE MONITOR — SIMULATION MODE")
    print("  Simulating: QR scan → LPR → auto entry/exit")
    print("="*55 + "\n")

    # Warm up OCR in background
    if EASYOCR_OK:
        threading.Thread(target=load_ocr, daemon=True).start()

    test_vehicles = [
        ("FT-A1B2",   "TN09AB1234", False),   # Normal guest
        ("SRM-VIP-01","TN01VIP001", False),    # VIP
        ("FT-EV01",   "EV-TN09AB1234", True),  # EV vehicle
        ("BLACKLISTED-01", "KA01XX0000", False), # Blacklisted
        ("FT-C3D4",   "MH12CD5678", False),   # Normal guest 2
    ]

    blank_frame = np.zeros((480, 640, 3), dtype=np.uint8)

    for fasttag_id, plate, is_ev in test_vehicles:
        print(f"\n{'─'*45}")
        print(f"  🚗 Simulating: {fasttag_id} | Plate: {plate}")

        # Simulate process_scan with a blank frame
        # (OCR won't find a plate in blank frame, EV detected via registry)
        process_scan(fasttag_id, blank_frame, api_base)
        time.sleep(4)

    print("\n" + "─"*45)
    print("  Simulating EXIT for first vehicle (FT-A1B2)…")
    time.sleep(2)
    process_scan("FT-A1B2", blank_frame, api_base)

    print("\n✅ Simulation complete.")
    print("   Check dashboard at", api_base)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY-CONFIRM ROUTE (add to app.py if missing)
# ══════════════════════════════════════════════════════════════════════════════

ENTRY_CONFIRM_NOTE = """
NOTE: gate_monitor.py opens /scan/entry-confirm/<slot>/<vehicle> on the laptop screen.
Add this route to app.py if not already present:

@app.route("/scan/entry-confirm/<slot_id>/<vehicle_id>")
def scan_entry_confirm(slot_id, vehicle_id):
    with get_db() as conn:
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
    if not slot:
        return render_template("error.html", message="Slot not found")
    from timediff import is_vip_vehicle, EV_SLOTS
    is_vip = bool(slot["is_vip"])
    is_ev  = slot_id in EV_SLOTS
    return render_template("welcome.html",
        vehicle=vehicle_id, slot_id=slot_id,
        entry_time=slot["entry_time"] or "",
        is_vip=is_vip, is_ev=is_ev, plate_verified=True)
"""


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RFID Gate Monitor v3.0")
    parser.add_argument("--camera",   type=int, default=0,
                        help="Camera device index (default: 0 = laptop webcam)")
    parser.add_argument("--api",      type=str, default="http://localhost:5000",
                        help="Flask API base URL")
    parser.add_argument("--simulate", action="store_true",
                        help="Run in simulation mode (no camera needed)")
    args = parser.parse_args()

    print("="*55)
    print("  RFID Gate Monitor v3.0")
    print("  SRM Institute of Science and Technology")
    print(f"  API    : {args.api}")
    print(f"  Camera : {'Simulation' if args.simulate else f'Index {args.camera}'}")
    print(f"  QR Lib : {'✅ pyzbar' if PYZBAR_OK else '❌ missing'}")
    print(f"  OCR    : {'✅ easyocr' if EASYOCR_OK else '❌ missing'}")
    print(f"  FL     : {'✅ enabled' if FL_OK and FL_ENABLED else '⚠️ fallback'}")
    print("="*55)

    # Print note about entry-confirm route
    print(ENTRY_CONFIRM_NOTE)

    if args.simulate:
        run_simulation(args.api)
    else:
        run_camera(args.camera, args.api)
