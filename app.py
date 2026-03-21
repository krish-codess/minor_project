"""
RFID-Based Automated Parking Management System  (v2.1 — IoT Integration)
Prototype by Akshat Gupta & Krish Nakul Gohel
SRM Institute of Science and Technology

v2.1 Additions over v2.0:
  • Instant-Scan Entry  — QR scan auto-generates FASTag ID, no manual typing
  • members table       — DB-backed VIP registry (editable via API)
  • Long-poll endpoint  — /api/poll/<cursor> for sub-2s dashboard sync
  • camera_monitor hook — POST /api/camera/update for OpenCV push updates
  • welcome.html        — success page after instant-scan entry
  • Full SQL everywhere — zero in-memory dict lookups remain
  • 0.0.0.0 binding     — accessible from phone on same WiFi
"""

from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import sqlite3, threading, random, string, os, time, socket

from timediff import (
    compute_fee, is_vip_vehicle, get_effective_rate,
    RATE_PER_HOUR, DYNAMIC_RATE, VIP_LIST
)

app = Flask(__name__)

DB_PATH     = "parking.db"
TOTAL_SLOTS = 10
BOOKING_TTL = 15        # minutes before a pre-booking auto-expires
POLL_TIMEOUT = 25       # seconds long-poll waits before returning empty

_db_lock      = threading.Lock()
_change_event = threading.Event()


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE LAYER
# ══════════════════════════════════════════════════════════════════════════════

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slots (
            id              TEXT PRIMARY KEY,
            is_occupied     INTEGER NOT NULL DEFAULT 0,
            current_vehicle TEXT,
            entry_time      TEXT,
            is_vip          INTEGER NOT NULL DEFAULT 0,
            booked_by       TEXT,
            booked_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id   TEXT NOT NULL,
            slot_id      TEXT NOT NULL,
            entry_time   TEXT NOT NULL,
            exit_time    TEXT,
            total_fee    REAL,
            duration_min INTEGER,
            duration_str TEXT,
            is_vip       INTEGER NOT NULL DEFAULT 0,
            rate_used    REAL,
            created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS members (
            vehicle_id  TEXT PRIMARY KEY,
            member_type TEXT NOT NULL DEFAULT 'GUEST'
        );

        CREATE TABLE IF NOT EXISTS active_sessions (
            vehicle_id TEXT PRIMARY KEY,
            slot_id    TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            is_vip     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS change_log (
            cursor      INTEGER PRIMARY KEY AUTOINCREMENT,
            changed_at  TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            slot_id     TEXT NOT NULL,
            payload     TEXT
        );
        """)

        if conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 0:
            conn.executemany("INSERT INTO slots(id) VALUES(?)",
                             [(str(i),) for i in range(1, TOTAL_SLOTS + 1)])

        for vid in VIP_LIST:
            mtype = "FACULTY" if "FACULTY" in vid else "VIP"
            conn.execute(
                "INSERT OR IGNORE INTO members(vehicle_id, member_type) VALUES(?,?)",
                (vid, mtype)
            )
        conn.commit()
    print("✅ Database initialised →", DB_PATH)


# ── Helpers ───────────────────────────────────────────────────────────────────

def slot_to_dict(row) -> dict:
    d = dict(row)
    d["is_occupied"] = bool(d.get("is_occupied", 0))
    d["is_vip"]      = bool(d.get("is_vip", 0))
    d["occupied"]    = d["is_occupied"]
    d["vehicle"]     = d.get("current_vehicle")
    d["slot_id"]     = d.get("id")
    return d


def get_occupied_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM slots WHERE is_occupied=1"
    ).fetchone()[0]


def effective_rate(conn) -> float:
    return get_effective_rate(get_occupied_count(conn), TOTAL_SLOTS)


def is_vip_db(vehicle_id: str, conn) -> bool:
    row = conn.execute(
        "SELECT member_type FROM members WHERE vehicle_id=?",
        (vehicle_id.upper(),)
    ).fetchone()
    if row:
        return row["member_type"] in ("VIP", "FACULTY")
    return is_vip_vehicle(vehicle_id)


def log_change(conn, event_type: str, slot_id: str, payload: str = None):
    conn.execute(
        "INSERT INTO change_log(changed_at,event_type,slot_id,payload) VALUES(?,?,?,?)",
        (datetime.now().isoformat(), event_type, str(slot_id), payload)
    )
    _change_event.set()
    _change_event.clear()


def generate_fasttag_id() -> str:
    """Simulate a FASTag RFID hardware read."""
    return "FT-" + "".join(random.choices(string.digits + "ABCDEF", k=4))


# ── Background: expire stale bookings ────────────────────────────────────────

def _booking_cleaner():
    time.sleep(3)
    while True:
        try:
            cutoff = (datetime.now() - timedelta(minutes=BOOKING_TTL)).isoformat()
            with _db_lock:
                with get_db() as conn:
                    conn.execute("""
                        UPDATE slots SET booked_by=NULL, booked_at=NULL
                        WHERE booked_at IS NOT NULL AND booked_at < ?
                          AND is_occupied=0
                    """, (cutoff,))
                    conn.commit()
        except Exception as e:
            print(f"[Booking cleaner] {e}")
        time.sleep(60)

threading.Thread(target=_booking_cleaner, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    try:
        with get_db() as conn:
            slots_raw = conn.execute(
                "SELECT * FROM slots ORDER BY CAST(id AS INTEGER)"
            ).fetchall()
            log_raw = conn.execute(
                "SELECT * FROM transactions ORDER BY id DESC LIMIT 10"
            ).fetchall()
            slots = {r["id"]: slot_to_dict(r) for r in slots_raw}
            log   = [dict(r) for r in log_raw]
        return render_template("dashboard.html", slots=slots,
                               log=log, rate=RATE_PER_HOUR)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# INSTANT-SCAN ENTRY  (v2.1 core feature)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/scan/entry/<slot_id>")
def scan_entry(slot_id):
    """
    Instant-Scan:
      1. Validate slot is free
      2. Auto-generate FASTag ID (simulates RFID read)
      3. Persist to DB immediately
      4. Render welcome page — zero manual input required
    """
    try:
        with _db_lock:
            with get_db() as conn:
                slot = conn.execute(
                    "SELECT * FROM slots WHERE id=?", (slot_id,)
                ).fetchone()

                if not slot:
                    return render_template("error.html",
                        message=f"Slot {slot_id} does not exist."), 404

                if slot["is_occupied"]:
                    rate   = effective_rate(conn)
                    slot_d = slot_to_dict(slot)
                    billing = compute_fee(slot["entry_time"],
                                          is_vip=bool(slot["is_vip"]),
                                          effective_rate=rate)
                    return render_template("exit.html", slot=slot_d, billing=billing)

                vehicle = generate_fasttag_id()
                vip     = is_vip_db(vehicle, conn)
                now     = datetime.now().isoformat()

                conn.execute("""
                    UPDATE slots
                    SET is_occupied=1, current_vehicle=?, entry_time=?,
                        is_vip=?, booked_by=NULL, booked_at=NULL
                    WHERE id=?
                """, (vehicle, now, int(vip), slot_id))

                conn.execute("""
                    INSERT OR REPLACE INTO active_sessions
                        (vehicle_id, slot_id, entry_time, is_vip)
                    VALUES (?,?,?,?)
                """, (vehicle, slot_id, now, int(vip)))

                conn.execute("""
                    INSERT INTO transactions
                        (vehicle_id, slot_id, entry_time, is_vip)
                    VALUES (?,?,?,?)
                """, (vehicle, slot_id, now, int(vip)))

                log_change(conn, "entry", slot_id)
                conn.commit()

        return render_template("welcome.html",
                               vehicle=vehicle,
                               slot_id=slot_id,
                               entry_time=now,
                               is_vip=vip)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# EXIT FLOW
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/scan/exit/<slot_id>")
def scan_exit(slot_id):
    try:
        with get_db() as conn:
            slot = conn.execute(
                "SELECT * FROM slots WHERE id=?", (slot_id,)
            ).fetchone()
            if not slot or not slot["is_occupied"]:
                return render_template("error.html",
                    message=f"No active session on slot {slot_id}.")
            slot_d  = slot_to_dict(slot)
            rate    = effective_rate(conn)
        billing = compute_fee(slot["entry_time"],
                              is_vip=bool(slot["is_vip"]),
                              effective_rate=rate)
        return render_template("exit.html", slot=slot_d, billing=billing)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — ENTRY (manual / dashboard)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/entry", methods=["POST"])
def api_entry():
    try:
        data    = request.json or {}
        slot_id = str(data.get("slot_id", "")).strip()
        vehicle = str(data.get("vehicle_id", "")).strip().upper()
        if not slot_id or not vehicle:
            return jsonify({"success": False,
                            "message": "slot_id and vehicle_id required"}), 400

        with _db_lock:
            with get_db() as conn:
                slot = conn.execute(
                    "SELECT * FROM slots WHERE id=?", (slot_id,)
                ).fetchone()
                if not slot:
                    return jsonify({"success": False, "message": "Slot not found"}), 404
                if slot["is_occupied"]:
                    return jsonify({"success": False,
                                    "message": "Slot already occupied"}), 409

                existing = conn.execute(
                    "SELECT slot_id FROM active_sessions WHERE vehicle_id=?",
                    (vehicle,)
                ).fetchone()
                if existing:
                    return jsonify({"success": False,
                                    "message": f"Vehicle parked in slot {existing['slot_id']}"}), 409

                vip = is_vip_db(vehicle, conn)
                now = datetime.now().isoformat()

                conn.execute("""
                    UPDATE slots
                    SET is_occupied=1, current_vehicle=?, entry_time=?,
                        is_vip=?, booked_by=NULL, booked_at=NULL
                    WHERE id=?
                """, (vehicle, now, int(vip), slot_id))
                conn.execute("""
                    INSERT OR REPLACE INTO active_sessions
                        (vehicle_id, slot_id, entry_time, is_vip)
                    VALUES (?,?,?,?)
                """, (vehicle, slot_id, now, int(vip)))
                conn.execute("""
                    INSERT INTO transactions (vehicle_id, slot_id, entry_time, is_vip)
                    VALUES (?,?,?,?)
                """, (vehicle, slot_id, now, int(vip)))
                log_change(conn, "entry", slot_id)
                conn.commit()

        return jsonify({"success": True,
                        "message": f"Entry logged. Slot {slot_id} → {vehicle}",
                        "entry_time": now, "slot_id": slot_id, "is_vip": vip})

    except sqlite3.OperationalError as e:
        return jsonify({"success": False, "message": "DB error", "detail": str(e)}), 503
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — EXIT
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/exit", methods=["POST"])
def api_exit():
    try:
        data    = request.json or {}
        slot_id = str(data.get("slot_id", "")).strip()
        if not slot_id:
            return jsonify({"success": False, "message": "slot_id required"}), 400

        with _db_lock:
            with get_db() as conn:
                slot = conn.execute(
                    "SELECT * FROM slots WHERE id=?", (slot_id,)
                ).fetchone()
                if not slot or not slot["is_occupied"]:
                    return jsonify({"success": False,
                                    "message": "No active session for this slot"}), 400

                rate      = effective_rate(conn)
                billing   = compute_fee(slot["entry_time"],
                                        is_vip=bool(slot["is_vip"]),
                                        effective_rate=rate)
                exit_time = datetime.now().isoformat()

                conn.execute("""
                    UPDATE transactions
                    SET exit_time=?, total_fee=?, duration_min=?,
                        duration_str=?, rate_used=?
                    WHERE vehicle_id=? AND slot_id=? AND exit_time IS NULL
                """, (exit_time, billing["fee"], billing["duration_min"],
                      billing["duration_str"], rate,
                      slot["current_vehicle"], slot_id))

                conn.execute(
                    "DELETE FROM active_sessions WHERE vehicle_id=?",
                    (slot["current_vehicle"],)
                )
                conn.execute("""
                    UPDATE slots
                    SET is_occupied=0, current_vehicle=NULL,
                        entry_time=NULL, is_vip=0
                    WHERE id=?
                """, (slot_id,))
                log_change(conn, "exit", slot_id)
                conn.commit()

        return jsonify({"success": True, "session": {
            "vehicle":     slot["current_vehicle"],
            "slot":        slot_id,
            "entry_time":  slot["entry_time"],
            "exit_time":   exit_time,
            **billing,
        }})
    except sqlite3.OperationalError as e:
        return jsonify({"success": False, "message": "DB error", "detail": str(e)}), 503
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — SLOTS / LOG / ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/slots")
def api_slots():
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM slots ORDER BY CAST(id AS INTEGER)"
            ).fetchall()
        return jsonify([slot_to_dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slots/<slot_id>")
def api_slot(slot_id):
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM slots WHERE id=?", (slot_id,)
            ).fetchone()
            if not row:
                return jsonify({"error": "Not found"}), 404
            result = slot_to_dict(row)
            if result["is_occupied"] and result["entry_time"]:
                rate = effective_rate(conn)
                result["billing_preview"] = compute_fee(
                    result["entry_time"],
                    is_vip=result["is_vip"],
                    effective_rate=rate
                )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/log")
def api_log():
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics")
def api_analytics():
    try:
        today_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        with get_db() as conn:
            rev = conn.execute("""
                SELECT COALESCE(SUM(total_fee),0) AS total
                FROM transactions WHERE exit_time >= ?
            """, (today_start,)).fetchone()["total"]

            peak_slot = conn.execute("""
                SELECT slot_id, COUNT(*) AS cnt FROM transactions
                GROUP BY slot_id ORDER BY cnt DESC LIMIT 1
            """).fetchone()

            avg_dur = conn.execute("""
                SELECT COALESCE(AVG(duration_min),0) AS avg_min
                FROM transactions WHERE duration_min IS NOT NULL
            """).fetchone()["avg_min"]

            hourly = conn.execute("""
                SELECT CAST(strftime('%H',exit_time) AS INTEGER) AS hour,
                       ROUND(SUM(total_fee),2) AS revenue
                FROM transactions WHERE exit_time >= ?
                GROUP BY hour ORDER BY hour
            """, (today_start,)).fetchall()

            peak_hours = conn.execute("""
                SELECT CAST(strftime('%H',entry_time) AS INTEGER) AS hour,
                       COUNT(*) AS count
                FROM transactions WHERE entry_time >= ?
                GROUP BY hour ORDER BY hour
            """, (today_start,)).fetchall()

            vip_today = conn.execute("""
                SELECT COUNT(*) AS cnt FROM transactions
                WHERE is_vip=1 AND exit_time >= ?
            """, (today_start,)).fetchone()["cnt"]

            occ  = get_occupied_count(conn)
            rate = get_effective_rate(occ, TOTAL_SLOTS)

        return jsonify({
            "total_revenue_today":    round(rev, 2),
            "most_occupied_slot":     peak_slot["slot_id"] if peak_slot else None,
            "avg_duration_min":       round(avg_dur, 1),
            "hourly_revenue_today":   [dict(r) for r in hourly],
            "peak_occupancy_hours":   [dict(r) for r in peak_hours],
            "vip_sessions_today":     vip_today,
            "dynamic_pricing_active": rate > RATE_PER_HOUR,
            "current_rate":           rate,
            "occupancy_percent":      round(occ / TOTAL_SLOTS * 100),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# LONG-POLL — sub-2s live sync  (v2.1)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/poll/<int:cursor>")
def api_poll(cursor):
    """
    Long-poll for real-time dashboard updates.
    Client passes its last cursor; server blocks until a change occurs,
    then returns new events + fresh slot states.
    """
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT * FROM change_log WHERE cursor > ? ORDER BY cursor",
                    (cursor,)
                ).fetchall()
            if rows:
                new_cursor = rows[-1]["cursor"]
                with get_db() as conn:
                    slots = conn.execute(
                        "SELECT * FROM slots ORDER BY CAST(id AS INTEGER)"
                    ).fetchall()
                return jsonify({
                    "cursor": new_cursor,
                    "events": [dict(r) for r in rows],
                    "slots":  [slot_to_dict(r) for r in slots],
                })
        except Exception:
            pass
        _change_event.wait(timeout=2)

    try:
        with get_db() as conn:
            max_c = conn.execute(
                "SELECT COALESCE(MAX(cursor),0) AS c FROM change_log"
            ).fetchone()["c"]
    except Exception:
        max_c = cursor
    return jsonify({"cursor": max_c, "events": [], "slots": []})


# ══════════════════════════════════════════════════════════════════════════════
# CAMERA MONITOR HOOK  (v2.1 — OpenCV push)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/camera/update", methods=["POST"])
def camera_update():
    """
    Called by camera_monitor.py when occupancy state changes.
    Body: { "slot_id": "3", "occupied": true }
    """
    try:
        data     = request.json or {}
        slot_id  = str(data.get("slot_id", "")).strip()
        occupied = bool(data.get("occupied", False))
        if not slot_id:
            return jsonify({"success": False, "message": "slot_id required"}), 400

        with _db_lock:
            with get_db() as conn:
                if not conn.execute(
                    "SELECT id FROM slots WHERE id=?", (slot_id,)
                ).fetchone():
                    return jsonify({"success": False, "message": "Slot not found"}), 404
                log_change(conn, "camera", slot_id,
                           f'{{"occupied":{str(occupied).lower()}}}')
                conn.commit()

        return jsonify({"success": True, "slot_id": slot_id,
                        "camera_occupied": occupied})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# PRE-BOOKING
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/book", methods=["POST"])
def api_book():
    try:
        data    = request.json or {}
        vehicle = str(data.get("vehicle_id", "")).strip().upper()
        slot_id = str(data.get("slot_id", "")).strip() or None
        if not vehicle:
            return jsonify({"success": False, "message": "vehicle_id required"}), 400

        with _db_lock:
            with get_db() as conn:
                if conn.execute(
                    "SELECT slot_id FROM active_sessions WHERE vehicle_id=?",
                    (vehicle,)
                ).fetchone():
                    return jsonify({"success": False,
                                    "message": "Vehicle already has active session"}), 409

                if slot_id:
                    target = conn.execute("""
                        SELECT * FROM slots
                        WHERE id=? AND is_occupied=0 AND booked_by IS NULL
                    """, (slot_id,)).fetchone()
                else:
                    target = conn.execute("""
                        SELECT * FROM slots
                        WHERE is_occupied=0 AND booked_by IS NULL
                        ORDER BY CAST(id AS INTEGER) LIMIT 1
                    """).fetchone()

                if not target:
                    return jsonify({"success": False,
                                    "message": "No free slot available"}), 409

                now = datetime.now().isoformat()
                conn.execute(
                    "UPDATE slots SET booked_by=?, booked_at=? WHERE id=?",
                    (vehicle, now, target["id"])
                )
                log_change(conn, "booking", target["id"])
                conn.commit()

        expires = (datetime.now() + timedelta(minutes=BOOKING_TTL)).isoformat()
        return jsonify({"success": True, "slot_id": target["id"],
                        "booked_at": now, "expires_at": expires})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/clear/<slot_id>", methods=["POST"])
def admin_clear(slot_id):
    try:
        with _db_lock:
            with get_db() as conn:
                slot = conn.execute(
                    "SELECT * FROM slots WHERE id=?", (slot_id,)
                ).fetchone()
                if not slot:
                    return jsonify({"error": "Not found"}), 404
                if slot["current_vehicle"]:
                    conn.execute(
                        "DELETE FROM active_sessions WHERE vehicle_id=?",
                        (slot["current_vehicle"],)
                    )
                conn.execute("""
                    UPDATE slots
                    SET is_occupied=0, current_vehicle=NULL, entry_time=NULL,
                        is_vip=0, booked_by=NULL, booked_at=NULL
                    WHERE id=?
                """, (slot_id,))
                log_change(conn, "exit", slot_id)
                conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/members", methods=["GET"])
def admin_members_get():
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM members ORDER BY member_type, vehicle_id"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/members", methods=["POST"])
def admin_members_post():
    try:
        data  = request.json or {}
        vid   = str(data.get("vehicle_id", "")).strip().upper()
        mtype = str(data.get("member_type", "GUEST")).strip().upper()
        if not vid:
            return jsonify({"success": False, "message": "vehicle_id required"}), 400
        with _db_lock:
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO members(vehicle_id,member_type) VALUES(?,?)",
                    (vid, mtype)
                )
                conn.commit()
        return jsonify({"success": True, "vehicle_id": vid, "member_type": mtype})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

init_db()

if __name__ == "__main__":
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "?.?.?.?"

    print("=" * 64)
    print("  RFID Parking System — v2.1 (IoT Integration & Persistence)")
    print("  SRM Institute of Science and Technology")
    print(f"  DB      : {os.path.abspath(DB_PATH)}")
    print(f"  Laptop  : http://localhost:5000")
    print(f"  Phone   : http://{local_ip}:5000")
    print(f"  Rate    : ₹{RATE_PER_HOUR}/hr base | ₹{DYNAMIC_RATE}/hr surge (>80%)")
    print("=" * 64)
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=5000)
