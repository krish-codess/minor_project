"""
RFID-Based Automated Parking Management System  (v2.0 — Production Upgrade)
Prototype by Akshat Gupta & Krish Nakul Gohel
SRM Institute of Science and Technology

Upgrades over v1.0:
  • SQLite persistence (parking.db) — data survives restarts
  • VIP membership billing with 50% discount
  • Dynamic pricing (1.5× when >80% occupancy)
  • /api/analytics endpoint — daily revenue, peak slot, avg duration
  • Robust try/except error handling on all critical routes
  • Pre-booking system — reserve a slot for 15 min, auto-released
"""

from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import sqlite3
import threading
import os

from timediff import (
    compute_fee, is_vip_vehicle, get_effective_rate,
    RATE_PER_HOUR, DYNAMIC_RATE, VIP_LIST
)

app = Flask(__name__)

DB_PATH       = "parking.db"
TOTAL_SLOTS   = 10
PREBOOKING_TTL_MINUTES = 15    # auto-release after 15 min if no entry

# Thread lock for DB writes (prevents SQLite "database is locked" on concurrent requests)
_db_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE LAYER
# ══════════════════════════════════════════════════════════════════════════════

def get_db() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with row_factory."""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrency
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create schema if it doesn't exist. Safe to call on every startup."""
    with get_db() as conn:
        conn.executescript("""
        -- Master slot registry (10 fixed slots, seeded once)
        CREATE TABLE IF NOT EXISTS Slots (
            slot_id     TEXT PRIMARY KEY,
            occupied    INTEGER NOT NULL DEFAULT 0,   -- 0=free, 1=occupied
            vehicle_id  TEXT,
            entry_time  TEXT,   -- ISO 8601
            is_vip      INTEGER NOT NULL DEFAULT 0,
            booked_by   TEXT,   -- pre-booking: vehicle_id that reserved this slot
            booked_at   TEXT    -- ISO 8601 timestamp of the pre-booking
        );

        -- Active session mirror (for quick lookups and analytics)
        CREATE TABLE IF NOT EXISTS ActiveSessions (
            vehicle_id  TEXT PRIMARY KEY,
            slot_id     TEXT NOT NULL,
            entry_time  TEXT NOT NULL,
            is_vip      INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(slot_id) REFERENCES Slots(slot_id)
        );

        -- Historical billing records
        CREATE TABLE IF NOT EXISTS BillingHistory (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id      TEXT NOT NULL,
            slot_id         TEXT NOT NULL,
            entry_time      TEXT NOT NULL,
            exit_time       TEXT NOT NULL,
            duration_min    INTEGER NOT NULL,
            duration_str    TEXT NOT NULL,
            fee             REAL NOT NULL,
            is_vip          INTEGER NOT NULL DEFAULT 0,
            rate_used       REAL NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        """)
        # Seed the 10 slots if not already present
        existing = conn.execute("SELECT COUNT(*) FROM Slots").fetchone()[0]
        if existing == 0:
            conn.executemany(
                "INSERT INTO Slots(slot_id) VALUES (?)",
                [(str(i),) for i in range(1, TOTAL_SLOTS + 1)]
            )
            conn.commit()
    print("✅ Database initialised →", DB_PATH)


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def slot_row_to_dict(row) -> dict:
    """Convert a Slots DB row to the same shape used by v1 templates."""
    d = dict(row)
    d["occupied"] = bool(d["occupied"])
    d["is_vip"]   = bool(d.get("is_vip", 0))
    return d


def get_occupied_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM Slots WHERE occupied=1"
    ).fetchone()[0]


def get_effective_rate_now(conn) -> float:
    occ = get_occupied_count(conn)
    return get_effective_rate(occ, TOTAL_SLOTS)


def billing_preview(slot: dict, conn) -> dict:
    """Compute fee for an active session (preview, not finalized)."""
    rate = get_effective_rate_now(conn)
    return compute_fee(
        slot["entry_time"],
        is_vip=slot.get("is_vip", False),
        effective_rate=rate,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PRE-BOOKING BACKGROUND CLEANER
# ══════════════════════════════════════════════════════════════════════════════

def _release_expired_bookings():
    """Background thread: release pre-bookings that exceed TTL."""
    import time
    time.sleep(2)
    while True:
        try:
            cutoff = (datetime.now() - timedelta(minutes=PREBOOKING_TTL_MINUTES)).isoformat()
            with _db_lock:
                with get_db() as conn:
                    conn.execute("""
                        UPDATE Slots
                        SET booked_by=NULL, booked_at=NULL
                        WHERE booked_at IS NOT NULL AND booked_at < ? AND occupied=0
                    """, (cutoff,))
                    conn.commit()
        except Exception as e:
            print(f"[Booking cleaner] Error: {e}")
        time.sleep(60)   # check every minute


threading.Thread(target=_release_expired_bookings, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    try:
        with get_db() as conn:
            slots_raw = conn.execute("SELECT * FROM Slots ORDER BY CAST(slot_id AS INTEGER)").fetchall()
            log_raw   = conn.execute(
                "SELECT * FROM BillingHistory ORDER BY id DESC LIMIT 10"
            ).fetchall()
            slots = {r["slot_id"]: slot_row_to_dict(r) for r in slots_raw}
            log   = [dict(r) for r in log_raw]
        return render_template("dashboard.html",
                               slots=slots,
                               log=log,
                               rate=RATE_PER_HOUR)
    except Exception as e:
        return jsonify({"error": "Dashboard load failed", "detail": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — ENTRY FLOW
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/scan/entry/<slot_id>")
def scan_entry(slot_id):
    try:
        with get_db() as conn:
            slot = conn.execute("SELECT * FROM Slots WHERE slot_id=?", (slot_id,)).fetchone()
        if not slot:
            return jsonify({"error": "Invalid slot"}), 404
        slot = slot_row_to_dict(slot)
        if slot["occupied"]:
            return render_template("occupied.html", slot=slot)
        return render_template("entry.html", slot=slot)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/entry", methods=["POST"])
def api_entry():
    try:
        data      = request.json or {}
        slot_id   = str(data.get("slot_id", "")).strip()
        vehicle   = str(data.get("vehicle_id", "")).strip().upper()

        if not slot_id or not vehicle:
            return jsonify({"success": False, "message": "slot_id and vehicle_id are required"}), 400

        vip = is_vip_vehicle(vehicle)

        with _db_lock:
            with get_db() as conn:
                slot = conn.execute(
                    "SELECT * FROM Slots WHERE slot_id=?", (slot_id,)
                ).fetchone()

                if not slot:
                    return jsonify({"success": False, "message": "Slot not found"}), 404
                if slot["occupied"]:
                    return jsonify({"success": False, "message": "Slot already occupied"}), 409

                # Check if vehicle already has an active session
                existing = conn.execute(
                    "SELECT slot_id FROM ActiveSessions WHERE vehicle_id=?", (vehicle,)
                ).fetchone()
                if existing:
                    return jsonify({
                        "success": False,
                        "message": f"Vehicle {vehicle} is already parked in slot {existing['slot_id']}"
                    }), 409

                # Check pre-booking conflict
                booked_by = slot["booked_by"]
                if booked_by and booked_by != vehicle:
                    return jsonify({
                        "success": False,
                        "message": f"Slot {slot_id} is pre-booked by another vehicle"
                    }), 409

                now = datetime.now().isoformat()

                # Determine effective rate at time of entry (logged for exit billing)
                occ = get_occupied_count(conn) + 1   # +1 for this vehicle
                rate = get_effective_rate(occ, TOTAL_SLOTS)

                conn.execute("""
                    UPDATE Slots
                    SET occupied=1, vehicle_id=?, entry_time=?, is_vip=?,
                        booked_by=NULL, booked_at=NULL
                    WHERE slot_id=?
                """, (vehicle, now, int(vip), slot_id))

                conn.execute("""
                    INSERT INTO ActiveSessions(vehicle_id, slot_id, entry_time, is_vip)
                    VALUES (?, ?, ?, ?)
                """, (vehicle, slot_id, now, int(vip)))

                conn.commit()

        return jsonify({
            "success":    True,
            "message":    f"Entry logged. Slot {slot_id} assigned to {vehicle}.",
            "entry_time": now,
            "slot_id":    slot_id,
            "is_vip":     vip,
            "rate":       rate,
        })

    except sqlite3.OperationalError as e:
        return jsonify({"success": False, "message": "Database error", "detail": str(e)}), 503
    except Exception as e:
        return jsonify({"success": False, "message": "Unexpected error", "detail": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — EXIT FLOW
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/scan/exit/<slot_id>")
def scan_exit(slot_id):
    try:
        with get_db() as conn:
            slot = conn.execute("SELECT * FROM Slots WHERE slot_id=?", (slot_id,)).fetchone()
            if not slot or not slot["occupied"]:
                return render_template("no_vehicle.html", slot_id=slot_id)
            slot = slot_row_to_dict(slot)
            rate = get_effective_rate_now(conn)
        billing = compute_fee(slot["entry_time"], is_vip=slot["is_vip"], effective_rate=rate)
        return render_template("exit.html", slot=slot, billing=billing)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/exit", methods=["POST"])
def api_exit():
    try:
        data    = request.json or {}
        slot_id = str(data.get("slot_id", "")).strip()

        if not slot_id:
            return jsonify({"success": False, "message": "slot_id is required"}), 400

        with _db_lock:
            with get_db() as conn:
                slot = conn.execute(
                    "SELECT * FROM Slots WHERE slot_id=?", (slot_id,)
                ).fetchone()

                if not slot or not slot["occupied"]:
                    return jsonify({"success": False, "message": "No active session for this slot"}), 400

                slot = slot_row_to_dict(slot)
                rate = get_effective_rate_now(conn)
                billing = compute_fee(
                    slot["entry_time"],
                    is_vip=slot["is_vip"],
                    effective_rate=rate,
                )

                exit_time = datetime.now().isoformat()

                # Write to billing history
                conn.execute("""
                    INSERT INTO BillingHistory
                        (vehicle_id, slot_id, entry_time, exit_time,
                         duration_min, duration_str, fee, is_vip, rate_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    slot["vehicle_id"], slot_id,
                    slot["entry_time"], exit_time,
                    billing["duration_min"], billing["duration_str"],
                    billing["fee"], int(slot["is_vip"]),
                    rate,
                ))

                # Clear active session
                conn.execute(
                    "DELETE FROM ActiveSessions WHERE vehicle_id=?", (slot["vehicle_id"],)
                )

                # Free the slot
                conn.execute("""
                    UPDATE Slots
                    SET occupied=0, vehicle_id=NULL, entry_time=NULL, is_vip=0
                    WHERE slot_id=?
                """, (slot_id,))

                conn.commit()

        record = {
            "vehicle":      slot["vehicle_id"],
            "slot":         slot_id,
            "entry_time":   slot["entry_time"],
            "exit_time":    exit_time,
            **billing,
        }
        return jsonify({"success": True, "session": record})

    except sqlite3.OperationalError as e:
        return jsonify({"success": False, "message": "Database error", "detail": str(e)}), 503
    except Exception as e:
        return jsonify({"success": False, "message": "Unexpected error", "detail": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — STATUS / READ APIs
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/slots")
def api_slots():
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM Slots ORDER BY CAST(slot_id AS INTEGER)"
            ).fetchall()
        return jsonify([slot_row_to_dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slots/<slot_id>")
def api_slot(slot_id):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM Slots WHERE slot_id=?", (slot_id,)).fetchone()
            if not row:
                return jsonify({"error": "Not found"}), 404
            result = slot_row_to_dict(row)
            if result["occupied"] and result["entry_time"]:
                result["billing_preview"] = billing_preview(result, conn)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/log")
def api_log():
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM BillingHistory ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — ANALYTICS  (NEW in v2.0)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/analytics")
def api_analytics():
    """
    Returns JSON suitable for Chart.js dashboards:
      - total_revenue_today      : ₹ total for completed sessions today
      - most_occupied_slot       : slot_id with highest session count (all time)
      - avg_duration_min         : average parking duration (all time, minutes)
      - hourly_revenue_today     : list of {hour, revenue} for Chart.js bar chart
      - peak_occupancy_hours     : list of {hour, count} for line chart
      - vip_sessions_today       : count of VIP sessions today
      - dynamic_pricing_active   : True if current occupancy triggers surge
      - current_rate             : effective rate right now (₹/hr)
      - occupancy_percent        : integer 0-100
    """
    try:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        with get_db() as conn:

            # ── Total revenue today ──────────────────────────────────────────
            rev_row = conn.execute("""
                SELECT COALESCE(SUM(fee), 0) AS total
                FROM BillingHistory
                WHERE exit_time >= ?
            """, (today_start,)).fetchone()
            total_revenue_today = round(rev_row["total"], 2)

            # ── Most occupied slot (all-time session count) ──────────────────
            peak_slot_row = conn.execute("""
                SELECT slot_id, COUNT(*) AS cnt
                FROM BillingHistory
                GROUP BY slot_id
                ORDER BY cnt DESC
                LIMIT 1
            """).fetchone()
            most_occupied_slot = peak_slot_row["slot_id"] if peak_slot_row else None

            # ── Average duration (all time) ──────────────────────────────────
            avg_row = conn.execute("""
                SELECT COALESCE(AVG(duration_min), 0) AS avg_min
                FROM BillingHistory
            """).fetchone()
            avg_duration_min = round(avg_row["avg_min"], 1)

            # ── Hourly revenue today (for Chart.js bar chart) ────────────────
            hourly_rows = conn.execute("""
                SELECT
                    CAST(strftime('%H', exit_time) AS INTEGER) AS hour,
                    ROUND(SUM(fee), 2) AS revenue
                FROM BillingHistory
                WHERE exit_time >= ?
                GROUP BY hour
                ORDER BY hour
            """, (today_start,)).fetchall()
            hourly_revenue_today = [{"hour": r["hour"], "revenue": r["revenue"]}
                                    for r in hourly_rows]

            # ── Peak occupancy by entry hour ─────────────────────────────────
            peak_hours_rows = conn.execute("""
                SELECT
                    CAST(strftime('%H', entry_time) AS INTEGER) AS hour,
                    COUNT(*) AS count
                FROM BillingHistory
                WHERE exit_time >= ?
                GROUP BY hour
                ORDER BY hour
            """, (today_start,)).fetchall()
            peak_occupancy_hours = [{"hour": r["hour"], "count": r["count"]}
                                    for r in peak_hours_rows]

            # ── VIP sessions today ───────────────────────────────────────────
            vip_row = conn.execute("""
                SELECT COUNT(*) AS cnt
                FROM BillingHistory
                WHERE is_vip=1 AND exit_time >= ?
            """, (today_start,)).fetchone()
            vip_sessions_today = vip_row["cnt"]

            # ── Current occupancy & rate ─────────────────────────────────────
            occ_count = get_occupied_count(conn)
            current_rate = get_effective_rate(occ_count, TOTAL_SLOTS)
            dynamic_active = current_rate > RATE_PER_HOUR
            occupancy_pct  = round(occ_count / TOTAL_SLOTS * 100)

        return jsonify({
            "total_revenue_today":   total_revenue_today,
            "most_occupied_slot":    most_occupied_slot,
            "avg_duration_min":      avg_duration_min,
            "hourly_revenue_today":  hourly_revenue_today,
            "peak_occupancy_hours":  peak_occupancy_hours,
            "vip_sessions_today":    vip_sessions_today,
            "dynamic_pricing_active": dynamic_active,
            "current_rate":          current_rate,
            "occupancy_percent":     occupancy_pct,
        })

    except Exception as e:
        return jsonify({"error": "Analytics error", "detail": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — PRE-BOOKING  (NEW in v2.0)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/book", methods=["POST"])
def api_book():
    """
    Reserve a free slot for up to PREBOOKING_TTL_MINUTES minutes.
    Body: { "slot_id": "3", "vehicle_id": "TN09AB1234" }
    If slot_id is omitted, system auto-picks the lowest free slot.
    """
    try:
        data    = request.json or {}
        vehicle = str(data.get("vehicle_id", "")).strip().upper()
        slot_id = str(data.get("slot_id", "")).strip() or None

        if not vehicle:
            return jsonify({"success": False, "message": "vehicle_id required"}), 400

        with _db_lock:
            with get_db() as conn:

                # Check vehicle not already active or booked
                existing = conn.execute(
                    "SELECT slot_id FROM ActiveSessions WHERE vehicle_id=?", (vehicle,)
                ).fetchone()
                if existing:
                    return jsonify({"success": False, "message": "Vehicle already has an active session"}), 409

                already_booked = conn.execute(
                    "SELECT slot_id FROM Slots WHERE booked_by=?", (vehicle,)
                ).fetchone()
                if already_booked:
                    return jsonify({"success": False,
                                    "message": f"Vehicle already has a booking for slot {already_booked['slot_id']}"}), 409

                if slot_id:
                    target = conn.execute(
                        "SELECT * FROM Slots WHERE slot_id=? AND occupied=0 AND booked_by IS NULL",
                        (slot_id,)
                    ).fetchone()
                    if not target:
                        return jsonify({"success": False, "message": "Slot unavailable or already booked"}), 409
                else:
                    target = conn.execute("""
                        SELECT * FROM Slots
                        WHERE occupied=0 AND booked_by IS NULL
                        ORDER BY CAST(slot_id AS INTEGER)
                        LIMIT 1
                    """).fetchone()
                    if not target:
                        return jsonify({"success": False, "message": "No free slots available"}), 409

                booked_slot = target["slot_id"]
                now = datetime.now().isoformat()
                conn.execute("""
                    UPDATE Slots SET booked_by=?, booked_at=? WHERE slot_id=?
                """, (vehicle, now, booked_slot))
                conn.commit()

        expires_at = (datetime.now() + timedelta(minutes=PREBOOKING_TTL_MINUTES)).isoformat()
        return jsonify({
            "success":    True,
            "slot_id":    booked_slot,
            "vehicle_id": vehicle,
            "booked_at":  now,
            "expires_at": expires_at,
            "message":    f"Slot {booked_slot} reserved for {PREBOOKING_TTL_MINUTES} min.",
        })

    except sqlite3.OperationalError as e:
        return jsonify({"success": False, "message": "Database error", "detail": str(e)}), 503
    except Exception as e:
        return jsonify({"success": False, "message": "Unexpected error", "detail": str(e)}), 500


@app.route("/api/book/cancel", methods=["POST"])
def api_book_cancel():
    """Cancel a pre-booking. Body: { "vehicle_id": "TN09AB1234" }"""
    try:
        vehicle = str((request.json or {}).get("vehicle_id", "")).strip().upper()
        if not vehicle:
            return jsonify({"success": False, "message": "vehicle_id required"}), 400

        with _db_lock:
            with get_db() as conn:
                conn.execute(
                    "UPDATE Slots SET booked_by=NULL, booked_at=NULL WHERE booked_by=?",
                    (vehicle,)
                )
                conn.commit()
        return jsonify({"success": True, "message": "Booking cancelled."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — ADMIN
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/clear/<slot_id>", methods=["POST"])
def admin_clear(slot_id):
    """Emergency clear of a slot (admin only in production)."""
    try:
        with _db_lock:
            with get_db() as conn:
                slot = conn.execute("SELECT * FROM Slots WHERE slot_id=?", (slot_id,)).fetchone()
                if not slot:
                    return jsonify({"error": "Not found"}), 404
                conn.execute(
                    "DELETE FROM ActiveSessions WHERE vehicle_id=?", (slot["vehicle_id"],)
                )
                conn.execute("""
                    UPDATE Slots
                    SET occupied=0, vehicle_id=NULL, entry_time=NULL, is_vip=0,
                        booked_by=NULL, booked_at=NULL
                    WHERE slot_id=?
                """, (slot_id,))
                conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/vip-list")
def admin_vip_list():
    """Returns the current VIP vehicle list (read-only, for admin dashboard)."""
    return jsonify({"vip_list": sorted(VIP_LIST)})


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

init_db()

if __name__ == "__main__":
    print("="*62)
    print("  RFID Parking Management System — v2.0 (Production Upgrade)")
    print("  SRM Institute of Science and Technology")
    print(f"  Database  : {os.path.abspath(DB_PATH)}")
    print(f"  Base rate : ₹{RATE_PER_HOUR}/hr | Surge: ₹{DYNAMIC_RATE}/hr (>80% full)")
    print(f"  VIP list  : {len(VIP_LIST)} registered vehicles")
    print("  Open      : http://localhost:5000")
    print("="*62)
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=5000)
