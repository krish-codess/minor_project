"""
RFID-Based Automated Parking Management System
Prototype by Akshat Gupta & Krish Nakul Gohel
SRM Institute of Science and Technology
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for
from datetime import datetime
import json
import os

app = Flask(__name__)

# ── In-memory "database" ──────────────────────────────────────────────────────
PARKING_SLOTS = {str(i): {"id": str(i), "occupied": False, "vehicle": None, "entry_time": None}
                 for i in range(1, 11)}   # 10 slots

SESSION_LOG = []   # completed sessions
ACTIVE_SESSIONS = {}  # vehicle_id -> slot_id

RATE_PER_HOUR = 30   # ₹30 per hour (₹0.50/min)


# ── Utility ───────────────────────────────────────────────────────────────────
def timediff(start_iso: str) -> dict:
    """TIMEDIFF algorithm: compute duration and fee from entry timestamp."""
    start = datetime.fromisoformat(start_iso)
    now   = datetime.now()
    delta = now - start
    total_minutes = int(delta.total_seconds() / 60)
    total_hours   = delta.total_seconds() / 3600
    fee = max(10, round(total_hours * RATE_PER_HOUR, 2))  # minimum ₹10
    return {
        "start":          start.strftime("%H:%M:%S"),
        "end":            now.strftime("%H:%M:%S"),
        "duration_min":   total_minutes,
        "duration_str":   f"{total_minutes // 60}h {total_minutes % 60}m",
        "fee":            fee,
    }


def available_slots():
    return [s for s in PARKING_SLOTS.values() if not s["occupied"]]


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return render_template("dashboard.html",
                           slots=PARKING_SLOTS,
                           log=SESSION_LOG[-10:][::-1],
                           rate=RATE_PER_HOUR)


# ── QR / Entry ────────────────────────────────────────────────────────────────
@app.route("/scan/entry/<slot_id>")
def scan_entry(slot_id):
    """Simulates driver scanning the entry QR code on parking slot."""
    slot = PARKING_SLOTS.get(slot_id)
    if not slot:
        return jsonify({"error": "Invalid slot"}), 404
    if slot["occupied"]:
        return render_template("occupied.html", slot=slot)
    return render_template("entry.html", slot=slot)


@app.route("/api/entry", methods=["POST"])
def api_entry():
    data      = request.json
    slot_id   = data.get("slot_id")
    vehicle   = data.get("vehicle_id", "").strip().upper()

    slot = PARKING_SLOTS.get(slot_id)
    if not slot or not vehicle:
        return jsonify({"success": False, "message": "Invalid slot or vehicle"}), 400
    if slot["occupied"]:
        return jsonify({"success": False, "message": "Slot already occupied"}), 409
    if vehicle in ACTIVE_SESSIONS:
        return jsonify({"success": False, "message": "Vehicle already parked"}), 409

    now = datetime.now().isoformat()
    slot.update({"occupied": True, "vehicle": vehicle, "entry_time": now})
    ACTIVE_SESSIONS[vehicle] = slot_id

    return jsonify({"success": True,
                    "message": f"Entry logged. Slot {slot_id} assigned to {vehicle}.",
                    "entry_time": now,
                    "slot_id": slot_id})


# ── Exit ──────────────────────────────────────────────────────────────────────
@app.route("/scan/exit/<slot_id>")
def scan_exit(slot_id):
    slot = PARKING_SLOTS.get(slot_id)
    if not slot or not slot["occupied"]:
        return render_template("no_vehicle.html", slot_id=slot_id)
    billing = timediff(slot["entry_time"])
    return render_template("exit.html", slot=slot, billing=billing)


@app.route("/api/exit", methods=["POST"])
def api_exit():
    data      = request.json
    slot_id   = data.get("slot_id")
    slot      = PARKING_SLOTS.get(slot_id)

    if not slot or not slot["occupied"]:
        return jsonify({"success": False, "message": "No active session for this slot"}), 400

    billing = timediff(slot["entry_time"])
    record  = {
        "vehicle":      slot["vehicle"],
        "slot":         slot_id,
        "entry_time":   slot["entry_time"],
        "exit_time":    datetime.now().isoformat(),
        **billing,
    }
    SESSION_LOG.append(record)
    ACTIVE_SESSIONS.pop(slot["vehicle"], None)
    slot.update({"occupied": False, "vehicle": None, "entry_time": None})

    return jsonify({"success": True, "session": record})


# ── Status API ────────────────────────────────────────────────────────────────
@app.route("/api/slots")
def api_slots():
    return jsonify(list(PARKING_SLOTS.values()))


@app.route("/api/slots/<slot_id>")
def api_slot(slot_id):
    slot = PARKING_SLOTS.get(slot_id)
    if not slot:
        return jsonify({"error": "Not found"}), 404
    result = dict(slot)
    if slot["occupied"] and slot["entry_time"]:
        result["billing_preview"] = timediff(slot["entry_time"])
    return jsonify(result)


@app.route("/api/log")
def api_log():
    return jsonify(SESSION_LOG[::-1])


# ── Admin: manual slot override ───────────────────────────────────────────────
@app.route("/api/admin/clear/<slot_id>", methods=["POST"])
def admin_clear(slot_id):
    """Emergency clear of a slot (admin only in production)."""
    slot = PARKING_SLOTS.get(slot_id)
    if not slot:
        return jsonify({"error": "Not found"}), 404
    if slot["vehicle"]:
        ACTIVE_SESSIONS.pop(slot["vehicle"], None)
    slot.update({"occupied": False, "vehicle": None, "entry_time": None})
    return jsonify({"success": True})


if __name__ == "__main__":
    print("="*60)
    print("  RFID Parking Management System — Prototype")
    print("  SRM Institute of Science and Technology")
    print("  Open: http://localhost:5000")
    print("="*60)
    app.run(debug=True, host="0.0.0.0", port=5000)
