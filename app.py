"""
RFID-Based Automated Parking Management System  (v3.0 — AI-Security & Smart Economy)
Prototype by Akshat Gupta & Krish Nakul Gohel
SRM Institute of Science and Technology

v3.0 Additions over v2.1:
  • Security Blacklist     — blocks entry, returns 403, logs alert
  • LPR Cross-Check hook  — /api/verify-plate simulates OCR plate match
  • Heartbeat Monitor     — camera vs DB state mismatch → Unauthorized alert
  • EV Surcharge          — slots 9 & 10 add ₹50 charging fee
  • PDF Receipt           — /api/receipt/<id> generates ReportLab PDF
  • Voice UI              — Web Speech API in entry/exit templates
"""

from flask import Flask, render_template, request, jsonify, send_file
from datetime import datetime, timedelta
from io import BytesIO
import sqlite3, threading, random, string, os, time, socket, json
from blockchain_ledger import mine_transaction, smart_contract_compute_fee

from timediff import (
    compute_fee, compute_green_credits, is_vip_vehicle, get_effective_rate,
    RATE_PER_HOUR, DYNAMIC_RATE, VIP_LIST, EV_SLOTS, EV_SURCHARGE
)

# ReportLab — graceful fallback if missing
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    REPORTLAB_OK = True
except ImportError:
    REPORTLAB_OK = False
    print("⚠️  ReportLab not found — PDF receipts disabled. Run: pip install reportlab")

app = Flask(__name__)

DB_PATH     = "parking.db"
TOTAL_SLOTS = 10
BOOKING_TTL = 15
POLL_TIMEOUT = 25

_db_lock      = threading.Lock()
_change_event = threading.Event()

# ── Simulated LPR plate registry (vehicle_id → registered plate) ─────────────
LPR_REGISTRY = {
    "FT-0001": "TN09AB1234",
    "FT-0002": "KA01CD5678",
    "SRM-VIP-01": "TN01VIP001",
    "SRM-VIP-02": "TN01VIP002",
}


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
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
            is_ev           INTEGER NOT NULL DEFAULT 0,
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
            is_ev        INTEGER NOT NULL DEFAULT 0,
            is_surge     INTEGER NOT NULL DEFAULT 0,
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

        -- v3.0: Security blacklist
        CREATE TABLE IF NOT EXISTS blacklist (
            vehicle_id  TEXT PRIMARY KEY,
            reason      TEXT,
            added_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            added_by    TEXT NOT NULL DEFAULT 'admin'
        );

        -- v3.0: Security alerts log
        CREATE TABLE IF NOT EXISTS security_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type  TEXT NOT NULL,
            slot_id     TEXT,
            vehicle_id  TEXT,
            detail      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            resolved    INTEGER NOT NULL DEFAULT 0
        );

        -- v3.0: Heartbeat log from camera_monitor
        CREATE TABLE IF NOT EXISTS heartbeat_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id     TEXT NOT NULL,
            camera_state TEXT NOT NULL,
            db_state    TEXT NOT NULL,
            mismatch    INTEGER NOT NULL DEFAULT 0,
            checked_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS performance_logs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            component             TEXT NOT NULL,
            mode                  TEXT NOT NULL,
            slot_id               TEXT,
            standard_latency_ms   REAL,
            optimized_latency_ms  REAL,
            speedup_factor        REAL,
            frames                INTEGER,
            notes                 TEXT,
            created_at            TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS blockchain_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id  INTEGER,
            block_index     INTEGER,
            block_hash      TEXT,
            previous_hash   TEXT,
            mined_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            payload         TEXT,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id)
        );

        CREATE TABLE IF NOT EXISTS arrival_predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id    TEXT,
            slot_id       TEXT,
            latitude      REAL,
            longitude     REAL,
            distance_m    REAL,
            eta_seconds   INTEGER,
            confidence    REAL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS eco_leaderboard (
            vehicle_id    TEXT PRIMARY KEY,
            total_points  INTEGER NOT NULL DEFAULT 0,
            badges        TEXT,
            last_updated  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS eco_reward_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER,
            vehicle_id     TEXT NOT NULL,
            points         INTEGER NOT NULL,
            badges         TEXT,
            reasons        TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(transaction_id) REFERENCES transactions(id)
        );
        """)

        cols = [r["name"] for r in conn.execute("PRAGMA table_info(slots)").fetchall()]
        if "latitude" not in cols:
            conn.execute("ALTER TABLE slots ADD COLUMN latitude REAL")
        if "longitude" not in cols:
            conn.execute("ALTER TABLE slots ADD COLUMN longitude REAL")

        # Seed slots
        if conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 0:
            conn.executemany("INSERT INTO slots(id, is_ev) VALUES(?,?)",
                             [(str(i), 1 if i in (9, 10) else 0)
                              for i in range(1, TOTAL_SLOTS + 1)])

        # Seed geotags around SRMIST for P1-P10 (non-destructive updates)
        base_lat = 12.82304
        base_lon = 80.04445
        for i in range(1, TOTAL_SLOTS + 1):
            lat = round(base_lat + 0.00012 * ((i - 1) // 5), 7)
            lon = round(base_lon + 0.00010 * ((i - 1) % 5), 7)
            conn.execute(
                """
                UPDATE slots
                SET latitude=COALESCE(latitude, ?),
                    longitude=COALESCE(longitude, ?)
                WHERE id=?
                """,
                (lat, lon, str(i))
            )

        # Seed VIP members
        for vid in VIP_LIST:
            mtype = "FACULTY" if "FACULTY" in vid else "VIP"
            conn.execute(
                "INSERT OR IGNORE INTO members(vehicle_id, member_type) VALUES(?,?)",
                (vid, mtype)
            )

        # Seed demo blacklist entries
        for vid, reason in [("BLACKLISTED-01", "Fraud attempt"),
                             ("BL-TEST-99",     "Demo blacklist entry")]:
            conn.execute(
                "INSERT OR IGNORE INTO blacklist(vehicle_id, reason) VALUES(?,?)",
                (vid, reason)
            )

        # Demo gamification baseline for presentation.
        conn.execute(
            """
            INSERT OR IGNORE INTO eco_leaderboard(vehicle_id, total_points, badges)
            VALUES (?, ?, ?)
            """,
            ("SRM-VIP-01", 180, "Eco-Driver,Prompt Parker,EV Champion")
        )

        conn.commit()

    # Recovery: count active sessions
    with get_db() as conn:
        active = conn.execute("SELECT COUNT(*) FROM active_sessions").fetchone()[0]
    print(f"✅ Database Loaded: {active} active session(s) recovered.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def slot_to_dict(row) -> dict:
    d = dict(row)
    d["is_occupied"] = bool(d.get("is_occupied", 0))
    d["is_vip"]      = bool(d.get("is_vip", 0))
    d["is_ev"]       = bool(d.get("is_ev", 0))
    d["occupied"]    = d["is_occupied"]
    d["vehicle"]     = d.get("current_vehicle")
    d["slot_id"]     = d.get("id")
    d["latitude"]    = d.get("latitude")
    d["longitude"]   = d.get("longitude")
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


def is_blacklisted(vehicle_id: str, conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM blacklist WHERE vehicle_id=?",
        (vehicle_id.upper(),)
    ).fetchone() is not None


def log_change(conn, event_type: str, slot_id: str, payload: str = None):
    conn.execute(
        "INSERT INTO change_log(changed_at,event_type,slot_id,payload) VALUES(?,?,?,?)",
        (datetime.now().isoformat(), event_type, str(slot_id), payload)
    )
    _change_event.set()
    _change_event.clear()


def log_alert(conn, alert_type: str, slot_id: str = None,
              vehicle_id: str = None, detail: str = None):
    conn.execute("""
        INSERT INTO security_alerts(alert_type, slot_id, vehicle_id, detail)
        VALUES (?,?,?,?)
    """, (alert_type, slot_id, vehicle_id, detail))
    log_change(conn, "alert", slot_id or "0", f'{{"type":"{alert_type}"}}')


def generate_fasttag_id() -> str:
    return "FT-" + "".join(random.choices(string.digits + "ABCDEF", k=4))


def _merge_badges(existing_badges: str, new_badges) -> str:
    old = [b.strip() for b in (existing_badges or "").split(",") if b.strip()]
    merged = sorted(set(old + list(new_badges)))
    return ",".join(merged)


def update_green_credits(conn, vehicle_id: str, transaction_id: int, reward: dict):
    badges_csv = ",".join(reward.get("badges", []))
    reasons_json = json.dumps(reward.get("reasons", []), ensure_ascii=True)

    conn.execute(
        """
        INSERT INTO eco_reward_log(transaction_id, vehicle_id, points, badges, reasons)
        VALUES (?, ?, ?, ?, ?)
        """,
        (transaction_id, vehicle_id, int(reward.get("points", 0)), badges_csv, reasons_json),
    )

    row = conn.execute(
        "SELECT total_points, badges FROM eco_leaderboard WHERE vehicle_id=?",
        (vehicle_id,),
    ).fetchone()

    if row:
        total = int(row["total_points"]) + int(reward.get("points", 0))
        merged_badges = _merge_badges(row["badges"], reward.get("badges", []))
        conn.execute(
            """
            UPDATE eco_leaderboard
            SET total_points=?, badges=?, last_updated=datetime('now','localtime')
            WHERE vehicle_id=?
            """,
            (total, merged_badges, vehicle_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO eco_leaderboard(vehicle_id, total_points, badges)
            VALUES (?, ?, ?)
            """,
            (vehicle_id, int(reward.get("points", 0)), badges_csv),
        )


def build_slot_reconfiguration(conn, preferred_slot: str = "1"):
    """Builds a virtual movement plan and chooses a free target slot for VIP override."""
    pref = str(preferred_slot)
    occupied_pref = conn.execute(
        "SELECT is_occupied FROM slots WHERE id=?",
        (pref,),
    ).fetchone()
    if not occupied_pref or not bool(occupied_pref["is_occupied"]):
        return None

    free_slot = conn.execute(
        """
        SELECT id FROM slots
        WHERE is_occupied=0
        ORDER BY CASE WHEN id IN ('9','10') THEN 0 ELSE 1 END,
                 ABS(CAST(id AS INTEGER) - CAST(? AS INTEGER))
        LIMIT 1
        """,
        (pref,),
    ).fetchone()
    if not free_slot:
        return None

    free_id = str(free_slot["id"])
    p = int(pref)
    f = int(free_id)
    movement_plan = []
    step = 1 if f > p else -1
    for idx in range(p, f, step):
        movement_plan.append({
            "from": str(idx),
            "to": str(idx + step),
            "axis": "horizontal" if ((idx - 1) // 5) == ((idx + step - 1) // 5) else "vertical",
        })

    return {
        "preferred_slot": pref,
        "assigned_slot": free_id,
        "movement_plan": movement_plan,
    }


# ── Background: booking cleaner ───────────────────────────────────────────────
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
            alerts_raw = conn.execute("""
                SELECT * FROM security_alerts
                WHERE resolved=0 ORDER BY id DESC LIMIT 5
            """).fetchall()
            slots  = {r["id"]: slot_to_dict(r) for r in slots_raw}
            log    = [dict(r) for r in log_raw]
            alerts = [dict(r) for r in alerts_raw]
        return render_template("dashboard.html", slots=slots,
                               log=log, rate=RATE_PER_HOUR, alerts=alerts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# INSTANT-SCAN ENTRY
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/scan/entry/<slot_id>")
def scan_entry(slot_id):
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
                    rate    = effective_rate(conn)
                    slot_d  = slot_to_dict(slot)
                    billing = compute_fee(
                        slot["entry_time"],
                        is_vip=bool(slot["is_vip"]),
                        effective_rate=rate,
                        slot_id=slot_id
                    )
                    return render_template("exit.html", slot=slot_d, billing=billing)

                vehicle   = generate_fasttag_id()
                vip       = is_vip_db(vehicle, conn)
                is_ev     = slot_id in EV_SLOTS
                now       = datetime.now().isoformat()

                # Blacklist check (auto-generated IDs won't be blacklisted
                # but this guards manual override edge cases)
                if is_blacklisted(vehicle, conn):
                    log_alert(conn, "BLACKLIST_ENTRY", slot_id, vehicle,
                              "Blacklisted vehicle attempted instant-scan entry")
                    conn.commit()
                    return render_template("error.html",
                        message="Security Alert: Vehicle Blacklisted."), 403

                # LPR verification (simulate)
                plate_verified = vehicle not in LPR_REGISTRY or True  # always pass for generated IDs

                conn.execute("""
                    UPDATE slots
                    SET is_occupied=1, current_vehicle=?, entry_time=?,
                        is_vip=?, is_ev=?, booked_by=NULL, booked_at=NULL
                    WHERE id=?
                """, (vehicle, now, int(vip), int(is_ev), slot_id))

                conn.execute("""
                    INSERT OR REPLACE INTO active_sessions
                        (vehicle_id, slot_id, entry_time, is_vip)
                    VALUES (?,?,?,?)
                """, (vehicle, slot_id, now, int(vip)))

                conn.execute("""
                    INSERT INTO transactions
                        (vehicle_id, slot_id, entry_time, is_vip, is_ev)
                    VALUES (?,?,?,?,?)
                """, (vehicle, slot_id, now, int(vip), int(is_ev)))

                log_change(conn, "entry", slot_id)
                conn.commit()

        return render_template("welcome.html",
                               vehicle=vehicle,
                               slot_id=slot_id,
                               entry_time=now,
                               is_vip=vip,
                               is_ev=is_ev,
                           plate_verified=plate_verified,
                           slot_lat=slot["latitude"],
                           slot_lng=slot["longitude"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# EXIT
# ══════════════════════════════════════════════════════════════════════════════


@app.route("/scan/entry-confirm/<slot_id>/<vehicle_id>")
def scan_entry_confirm(slot_id, vehicle_id):
    try:
        with get_db() as conn:
            slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            return render_template("error.html", message="Slot not found")
        is_vip = bool(slot["is_vip"])
        is_ev  = slot_id in EV_SLOTS
        return render_template("welcome.html",
                               vehicle=vehicle_id, slot_id=slot_id,
                               entry_time=slot["entry_time"] or datetime.now().isoformat(),
                               is_vip=is_vip, is_ev=is_ev, plate_verified=True,
                               slot_lat=slot["latitude"],
                               slot_lng=slot["longitude"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        billing = compute_fee(
            slot["entry_time"],
            is_vip=bool(slot["is_vip"]),
            effective_rate=rate,
            slot_id=slot_id
        )
        return render_template("exit.html", slot=slot_d, billing=billing)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — ENTRY
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

                # ── Blacklist check ──────────────────────────────────────────
                if is_blacklisted(vehicle, conn):
                    log_alert(conn, "BLACKLIST_ENTRY", slot_id, vehicle,
                              "Blacklisted vehicle attempted entry via dashboard")
                    conn.commit()
                    return jsonify({
                        "success": False,
                        "message": "🚨 Security Alert: Vehicle Blacklisted.",
                        "alert":   "BLACKLIST"
                    }), 403

                slot = conn.execute(
                    "SELECT * FROM slots WHERE id=?", (slot_id,)
                ).fetchone()
                if not slot:
                    return jsonify({"success": False, "message": "Slot not found"}), 404

                vip = is_vip_db(vehicle, conn)
                reconfig = None
                if slot["is_occupied"]:
                    if vip and slot_id == "1":
                        reconfig = build_slot_reconfiguration(conn, preferred_slot="1")
                        if not reconfig:
                            return jsonify({"success": False,
                                            "message": "No slot available for VIP reconfiguration"}), 409
                        slot_id = reconfig["assigned_slot"]
                        slot = conn.execute(
                            "SELECT * FROM slots WHERE id=?", (slot_id,)
                        ).fetchone()
                    else:
                        return jsonify({"success": False,
                                        "message": "Slot already occupied"}), 409

                existing = conn.execute(
                    "SELECT slot_id FROM active_sessions WHERE vehicle_id=?",
                    (vehicle,)
                ).fetchone()
                if existing:
                    return jsonify({"success": False,
                                    "message": f"Vehicle parked in slot {existing['slot_id']}"}), 409

                is_ev = slot_id in EV_SLOTS
                now   = datetime.now().isoformat()

                # LPR check
                plate_verified = simulate_lpr(vehicle)

                conn.execute("""
                    UPDATE slots
                    SET is_occupied=1, current_vehicle=?, entry_time=?,
                        is_vip=?, is_ev=?, booked_by=NULL, booked_at=NULL
                    WHERE id=?
                """, (vehicle, now, int(vip), int(is_ev), slot_id))
                conn.execute("""
                    INSERT OR REPLACE INTO active_sessions
                        (vehicle_id, slot_id, entry_time, is_vip)
                    VALUES (?,?,?,?)
                """, (vehicle, slot_id, now, int(vip)))
                conn.execute("""
                    INSERT INTO transactions
                        (vehicle_id, slot_id, entry_time, is_vip, is_ev)
                    VALUES (?,?,?,?,?)
                """, (vehicle, slot_id, now, int(vip), int(is_ev)))
                log_change(
                    conn,
                    "entry",
                    slot_id,
                    json.dumps({"reconfigured": bool(reconfig), "vehicle": vehicle}),
                )
                conn.commit()

        return jsonify({
            "success":        True,
            "message":        f"Entry logged. Slot {slot_id} → {vehicle}",
            "entry_time":     now,
            "slot_id":        slot_id,
            "is_vip":         vip,
            "is_ev":          is_ev,
            "plate_verified": plate_verified,
            "reconfigured":   bool(reconfig),
            "movement_plan":  reconfig["movement_plan"] if reconfig else [],
            "preferred_slot": reconfig["preferred_slot"] if reconfig else slot_id,
        })

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
                                    "message": "No active session"}), 400

                rate      = effective_rate(conn)
                is_surge  = rate > RATE_PER_HOUR
                billing   = compute_fee(
                    slot["entry_time"],
                    is_vip=bool(slot["is_vip"]),
                    effective_rate=rate,
                    is_surge=is_surge,
                    slot_id=slot_id
                )

                # Smart-contract parity computation for immutable billing audit.
                contract_billing = smart_contract_compute_fee(
                    slot["entry_time"],
                    is_vip=bool(slot["is_vip"]),
                    effective_rate=rate,
                    is_surge=is_surge,
                    slot_id=slot_id
                )
                exit_time = datetime.now().isoformat()

                # Get transaction id for receipt link
                txn = conn.execute("""
                    UPDATE transactions
                    SET exit_time=?, total_fee=?, duration_min=?,
                        duration_str=?, rate_used=?, is_surge=?
                    WHERE vehicle_id=? AND slot_id=? AND exit_time IS NULL
                """, (exit_time, billing["fee"], billing["duration_min"],
                      billing["duration_str"], rate, int(is_surge),
                      slot["current_vehicle"], slot_id))

                txn_id = conn.execute("""
                    SELECT id FROM transactions
                    WHERE vehicle_id=? AND slot_id=? AND exit_time=?
                """, (slot["current_vehicle"], slot_id, exit_time)).fetchone()

                reward = compute_green_credits(
                    slot["entry_time"],
                    exit_time,
                    slot_id=slot_id,
                    effective_rate=rate,
                    is_surge=is_surge,
                )
                if txn_id:
                    update_green_credits(conn, slot["current_vehicle"], txn_id["id"], reward)

                mined_block = None
                if txn_id:
                    tx_payload = {
                        "transaction_id": txn_id["id"],
                        "vehicle_id": slot["current_vehicle"],
                        "slot_id": slot_id,
                        "entry_time": slot["entry_time"],
                        "exit_time": exit_time,
                        "timediff_fee": billing["fee"],
                        "contract_fee": contract_billing["fee"],
                        "is_vip": bool(slot["is_vip"]),
                        "is_surge": bool(is_surge),
                        "is_ev": bool(slot_id in EV_SLOTS),
                    }
                    try:
                        mined_block = mine_transaction(tx_payload)
                        conn.execute(
                            """
                            INSERT INTO blockchain_logs(
                                transaction_id, block_index, block_hash, previous_hash, payload
                            ) VALUES (?,?,?,?,?)
                            """,
                            (
                                txn_id["id"],
                                mined_block.get("index"),
                                mined_block.get("hash"),
                                mined_block.get("previous_hash"),
                                str(tx_payload),
                            ),
                        )
                    except Exception as chain_err:
                        mined_block = {
                            "hash": None,
                            "index": None,
                            "error": str(chain_err),
                        }

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

        receipt_url = f"/api/receipt/{txn_id['id']}" if txn_id else None

        return jsonify({"success": True, "session": {
            "vehicle":      slot["current_vehicle"],
            "slot":         slot_id,
            "entry_time":   slot["entry_time"],
            "exit_time":    exit_time,
            "receipt_url":  receipt_url,
            "block_hash":   mined_block.get("hash") if mined_block else None,
            "block_index":  mined_block.get("index") if mined_block else None,
            "green_credits": reward,
            **billing,
        }})

    except sqlite3.OperationalError as e:
        return jsonify({"success": False, "message": "DB error", "detail": str(e)}), 503
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — LPR VERIFY  (v3.0)
# ══════════════════════════════════════════════════════════════════════════════

def simulate_lpr(vehicle_id: str) -> bool:
    """
    Simulates OCR plate read and cross-check with registered FASTag.
    In production: replace with OpenCV + EasyOCR call.
    Returns True if plate matches or vehicle not in registry (pass-through).
    """
    if vehicle_id in LPR_REGISTRY:
        # Simulate 95% match rate, 5% fraud flag
        return random.random() > 0.05
    return True  # unknown vehicle → pass (no registered plate to compare)


@app.route("/api/verify-plate", methods=["POST"])
def api_verify_plate():
    """
    Dummy LPR endpoint.
    Body: { "vehicle_id": "FT-0001", "detected_plate": "TN09AB1234" }
    Returns: { "verified": true/false, "registered_plate": "...", "fraud": false }
    """
    try:
        data     = request.json or {}
        vehicle  = str(data.get("vehicle_id", "")).strip().upper()
        detected = str(data.get("detected_plate", "")).strip().upper()

        registered = LPR_REGISTRY.get(vehicle)

        if not registered:
            return jsonify({
                "verified":          True,
                "registered_plate":  None,
                "detected_plate":    detected,
                "fraud":             False,
                "message":           "No registered plate — entry allowed"
            })

        match = (detected == registered) if detected else True
        fraud = not match

        if fraud:
            with _db_lock:
                with get_db() as conn:
                    log_alert(conn, "LPR_MISMATCH", None, vehicle,
                              f"Registered: {registered} | Detected: {detected}")
                    conn.commit()

        return jsonify({
            "verified":         match,
            "registered_plate": registered,
            "detected_plate":   detected,
            "fraud":            fraud,
            "message":          "Plate verified ✅" if match else "🚨 FRAUD: Plate mismatch!"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — PDF RECEIPT  (v3.0)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/receipt/<int:transaction_id>")
def api_receipt(transaction_id):
    if not REPORTLAB_OK:
        return jsonify({"error": "ReportLab not installed"}), 503
    try:
        with get_db() as conn:
            txn = conn.execute(
                "SELECT * FROM transactions WHERE id=?", (transaction_id,)
            ).fetchone()
        if not txn:
            return jsonify({"error": "Transaction not found"}), 404

        txn = dict(txn)
        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                 rightMargin=40, leftMargin=40,
                                 topMargin=60, bottomMargin=40)
        styles = getSampleStyleSheet()
        elements = []

        # Header
        elements.append(Paragraph(
            "<b>🚗 RFID Parking Management System</b>",
            styles["Title"]
        ))
        elements.append(Paragraph(
            "SRM Institute of Science and Technology",
            styles["Normal"]
        ))
        elements.append(Spacer(1, 20))
        elements.append(Paragraph(
            f"<b>PAYMENT RECEIPT</b> — #{transaction_id}",
            styles["Heading2"]
        ))
        elements.append(Spacer(1, 12))

        # Details table
        entry_fmt = txn["entry_time"][:19].replace("T", " ") if txn["entry_time"] else "—"
        exit_fmt  = txn["exit_time"][:19].replace("T", " ")  if txn["exit_time"]  else "—"

        table_data = [
            ["Field", "Value"],
            ["Vehicle / FASTag ID", txn["vehicle_id"]],
            ["Slot",                f"P{txn['slot_id']}"],
            ["Entry Time",          entry_fmt],
            ["Exit Time",           exit_fmt],
            ["Duration",            txn["duration_str"] or "—"],
            ["Rate",                f"₹{txn['rate_used'] or 30}/hr{'  ⚡ SURGE' if txn['is_surge'] else ''}"],
            ["Member Type",         "👑 VIP (50% discount)" if txn["is_vip"] else "Guest"],
            ["EV Charging Fee",     f"₹{50}" if txn["is_ev"] else "N/A"],
            ["Total Fee Paid",      f"₹{txn['total_fee']}"],
        ]

        t = Table(table_data, colWidths=[200, 280])
        t.setStyle(TableStyle([
            ("BACKGROUND",   (0, 0), (-1, 0),  colors.HexColor("#0d1b3e")),
            ("TEXTCOLOR",    (0, 0), (-1, 0),  colors.white),
            ("FONTNAME",     (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("FONTSIZE",     (0, 0), (-1, 0),  11),
            ("BACKGROUND",   (0, -1), (-1, -1), colors.HexColor("#e8f5e9")),
            ("FONTNAME",     (0, -1), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE",     (0, -1), (-1, -1), 12),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f5f5f5")]),
            ("GRID",         (0, 0), (-1, -1),  0.5, colors.grey),
            ("PADDING",      (0, 0), (-1, -1),  8),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 20))

        elements.append(Paragraph(
            "Payment debited via FASTag. No cash transaction.",
            styles["Italic"]
        ))
        elements.append(Paragraph(
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            styles["Normal"]
        ))

        doc.build(elements)
        buf.seek(0)

        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"receipt_{transaction_id}_{txn['vehicle_id']}.pdf"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
                    effective_rate=rate,
                    slot_id=slot_id
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
            ev_today = conn.execute("""
                SELECT COUNT(*) AS cnt FROM transactions
                WHERE is_ev=1 AND exit_time >= ?
            """, (today_start,)).fetchone()["cnt"]
            alerts_today = conn.execute("""
                SELECT COUNT(*) AS cnt FROM security_alerts
                WHERE created_at >= ?
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
            "ev_sessions_today":      ev_today,
            "security_alerts_today":  alerts_today,
            "dynamic_pricing_active": rate > RATE_PER_HOUR,
            "current_rate":           rate,
            "occupancy_percent":      round(occ / TOTAL_SLOTS * 100),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — SECURITY  (v3.0)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/security/alerts")
def api_security_alerts():
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM security_alerts ORDER BY id DESC LIMIT 20"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/security/alerts/<int:alert_id>/resolve", methods=["POST"])
def api_resolve_alert(alert_id):
    try:
        with _db_lock:
            with get_db() as conn:
                conn.execute(
                    "UPDATE security_alerts SET resolved=1 WHERE id=?", (alert_id,)
                )
                conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/security/blacklist", methods=["GET"])
def api_blacklist_get():
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM blacklist ORDER BY added_at DESC"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/security/blacklist", methods=["POST"])
def api_blacklist_add():
    try:
        data   = request.json or {}
        vid    = str(data.get("vehicle_id", "")).strip().upper()
        reason = str(data.get("reason", "Manual block")).strip()
        if not vid:
            return jsonify({"success": False, "message": "vehicle_id required"}), 400
        with _db_lock:
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO blacklist(vehicle_id, reason) VALUES(?,?)",
                    (vid, reason)
                )
                conn.commit()
        return jsonify({"success": True, "vehicle_id": vid})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/eco/leaderboard")
def api_eco_leaderboard():
    try:
        limit = min(int(request.args.get("limit", 8)), 20)
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT vehicle_id, total_points, badges, last_updated
                FROM eco_leaderboard
                ORDER BY total_points DESC, last_updated DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["badges"] = [b.strip() for b in (item.get("badges") or "").split(",") if b.strip()]
            result.append(item)
        return jsonify({"leaders": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/eco/vehicle/<vehicle_id>")
def api_eco_vehicle(vehicle_id):
    try:
        vid = str(vehicle_id).strip().upper()
        with get_db() as conn:
            board = conn.execute(
                "SELECT total_points, badges, last_updated FROM eco_leaderboard WHERE vehicle_id=?",
                (vid,),
            ).fetchone()
            recent = conn.execute(
                """
                SELECT points, badges, reasons, created_at
                FROM eco_reward_log
                WHERE vehicle_id=?
                ORDER BY id DESC
                LIMIT 5
                """,
                (vid,),
            ).fetchall()
        return jsonify({
            "vehicle_id": vid,
            "total_points": int(board["total_points"]) if board else 0,
            "badges": [b.strip() for b in ((board["badges"] if board else "") or "").split(",") if b.strip()],
            "recent": [dict(r) for r in recent],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/voice/ev-upgrade", methods=["POST"])
def api_voice_ev_upgrade():
    try:
        data = request.json or {}
        slot_id = str(data.get("slot_id", "")).strip()
        vehicle = str(data.get("vehicle_id", "")).strip().upper()
        accept = bool(data.get("accept", False))

        if not slot_id or not vehicle:
            return jsonify({"success": False, "message": "slot_id and vehicle_id required"}), 400
        if not accept:
            return jsonify({"success": True, "upgraded": False, "message": "Upgrade declined"})

        with _db_lock:
            with get_db() as conn:
                session = conn.execute(
                    "SELECT slot_id, entry_time, is_vip FROM active_sessions WHERE vehicle_id=?",
                    (vehicle,),
                ).fetchone()
                if not session:
                    return jsonify({"success": False, "message": "Active session not found"}), 404

                current_slot = str(session["slot_id"])
                if current_slot in ("9", "10"):
                    return jsonify({
                        "success": True,
                        "upgraded": False,
                        "slot_id": current_slot,
                        "message": "Already in EV-ready slot",
                    })

                target = conn.execute(
                    """
                    SELECT id FROM slots
                    WHERE id IN ('9', '10') AND is_occupied=0
                    ORDER BY CAST(id AS INTEGER)
                    LIMIT 1
                    """
                ).fetchone()
                if not target:
                    return jsonify({"success": False, "message": "No EV slot currently available"}), 409

                target_slot = str(target["id"])
                conn.execute(
                    """
                    UPDATE slots
                    SET is_occupied=0, current_vehicle=NULL, entry_time=NULL, is_vip=0, is_ev=0
                    WHERE id=?
                    """,
                    (current_slot,),
                )
                conn.execute(
                    """
                    UPDATE slots
                    SET is_occupied=1, current_vehicle=?, entry_time=?, is_vip=?, is_ev=1,
                        booked_by=NULL, booked_at=NULL
                    WHERE id=?
                    """,
                    (vehicle, session["entry_time"], int(session["is_vip"]), target_slot),
                )
                conn.execute(
                    "UPDATE active_sessions SET slot_id=? WHERE vehicle_id=?",
                    (target_slot, vehicle),
                )
                conn.execute(
                    """
                    UPDATE transactions
                    SET slot_id=?, is_ev=1
                    WHERE vehicle_id=? AND exit_time IS NULL
                    """,
                    (target_slot, vehicle),
                )
                log_change(
                    conn,
                    "ev_upgrade",
                    target_slot,
                    json.dumps({"vehicle": vehicle, "from": current_slot, "to": target_slot}),
                )
                conn.commit()

        return jsonify({
            "success": True,
            "upgraded": True,
            "slot_id": target_slot,
            "message": f"EV upgrade successful. Moved to slot P{target_slot}",
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/reconfigure/demo", methods=["POST"])
def api_reconfigure_demo():
    try:
        data = request.json or {}
        vehicle = str(data.get("vehicle_id", "SRM-VIP-01")).strip().upper()
        preferred_slot = str(data.get("preferred_slot", "1")).strip() or "1"

        with _db_lock:
            with get_db() as conn:
                if not is_vip_db(vehicle, conn):
                    return jsonify({"success": False, "message": "VIP vehicle required for reconfiguration demo"}), 403
                plan = build_slot_reconfiguration(conn, preferred_slot)
                if not plan:
                    return jsonify({"success": False, "message": "No movement path available"}), 409

                log_change(
                    conn,
                    "reconfigure",
                    plan["assigned_slot"],
                    json.dumps({"vehicle": vehicle, "plan": plan["movement_plan"]}),
                )
                conn.commit()

        return jsonify({"success": True, "vehicle_id": vehicle, **plan})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/security/demo-tailgate", methods=["POST"])
def api_security_demo_tailgate():
    try:
        data = request.json or {}
        slot_id = str(data.get("slot_id", "3")).strip() or "3"
        with _db_lock:
            with get_db() as conn:
                conn.execute(
                    """
                    INSERT INTO heartbeat_log(slot_id, camera_state, db_state, mismatch)
                    VALUES (?, 'occupied', 'free', 1)
                    """,
                    (slot_id,),
                )
                log_alert(
                    conn,
                    "UNAUTHORIZED_ACCESS",
                    slot_id,
                    None,
                    "Manual demo trigger: simulated tailgating mismatch",
                )
                conn.commit()
        return jsonify({"success": True, "slot_id": slot_id})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — HEARTBEAT MONITOR  (v3.0)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/camera/heartbeat", methods=["POST"])
def camera_heartbeat():
    """
    Called by camera_monitor.py with its full occupancy snapshot.
    Compares camera state vs DB state for each slot.
    Flags mismatches as "Unauthorized Access" alerts.
    Body: { "states": { "1": true, "2": false, ... } }
    """
    try:
        data   = request.json or {}
        states = data.get("states", {})
        mismatches = []

        with _db_lock:
            with get_db() as conn:
                for slot_id, cam_occupied in states.items():
                    db_slot = conn.execute(
                        "SELECT is_occupied FROM slots WHERE id=?", (str(slot_id),)
                    ).fetchone()
                    if not db_slot:
                        continue
                    db_occupied  = bool(db_slot["is_occupied"])
                    cam_occupied = bool(cam_occupied)
                    mismatch     = cam_occupied != db_occupied

                    conn.execute("""
                        INSERT INTO heartbeat_log
                            (slot_id, camera_state, db_state, mismatch)
                        VALUES (?,?,?,?)
                    """, (slot_id,
                          "occupied" if cam_occupied else "free",
                          "occupied" if db_occupied  else "free",
                          int(mismatch)))

                    if mismatch and cam_occupied and not db_occupied:
                        # Camera sees car, DB says free → Unauthorized
                        log_alert(conn, "UNAUTHORIZED_ACCESS", slot_id, None,
                                  f"Camera: occupied | DB: free — possible tailgate")
                        mismatches.append(slot_id)

                conn.commit()

        return jsonify({
            "success":    True,
            "checked":    len(states),
            "mismatches": mismatches,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/camera/stream")
def camera_stream():
    """MJPEG stream of the live gate camera (fed by gate_monitor thread)."""
    try:
        import camera_buffer
    except ImportError:
        return "camera_buffer module not found", 503

    from flask import Response

    def generate():
        while True:
            frame = camera_buffer.get_frame()
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
            time.sleep(0.05)   # ~20 fps to browser

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/camera/snapshot")
def camera_snapshot():
    """Single latest JPEG frame — used as fallback when MJPEG isn't supported."""
    try:
        import camera_buffer
        frame = camera_buffer.get_frame()
    except ImportError:
        frame = None
    if not frame:
        from flask import Response
        return Response(status=204)
    from flask import Response
    return Response(frame, mimetype="image/jpeg")


@app.route("/api/qr-scan", methods=["POST"])
def api_qr_scan():
    """
    Process a QR code detected by the browser or any client.
    Body: {qr: <raw QR string>}
    Mirrors gate_monitor logic: auto entry or exit.
    """
    try:
        data = request.json or {}
        raw  = str(data.get("qr", "")).strip()
        if not raw:
            return jsonify({"success": False, "message": "qr field required"}), 400

        # Extract FASTag / vehicle ID from QR
        import gate_monitor as _gm
        fasttag_id = _gm.extract_fasttag(raw)
        if not fasttag_id:
            return jsonify({"success": False, "message": "Could not parse QR data"}), 400

        # Determine if exit (vehicle already parked) or entry
        with get_db() as conn:
            active = conn.execute(
                "SELECT slot_id FROM active_sessions WHERE vehicle_id=?",
                (fasttag_id,),
            ).fetchone()
            if not active:
                # Also check slots table
                active_slot = conn.execute(
                    "SELECT id AS slot_id FROM slots WHERE current_vehicle=? AND is_occupied=1",
                    (fasttag_id,),
                ).fetchone()
            else:
                active_slot = active

        if active_slot:
            # ── EXIT flow ──────────────────────────────────────────────────────
            slot_id = str(active_slot["slot_id"])
            with _db_lock:
                with get_db() as conn:
                    slot = conn.execute(
                        "SELECT * FROM slots WHERE id=?", (slot_id,)
                    ).fetchone()
                    if not slot or not slot["is_occupied"]:
                        return jsonify({"success": False, "message": "Slot not occupied"}), 409
                    entry_time = slot["entry_time"]
                    is_vip     = bool(slot["is_vip"])
                    is_ev      = slot_id in EV_SLOTS
                    occ  = get_occupied_count(conn)
                    rate = get_effective_rate(occ, TOTAL_SLOTS)
                    is_surge = rate > RATE_PER_HOUR
                    billing  = compute_fee(
                        entry_time, is_vip=is_vip,
                        effective_rate=rate, is_surge=is_surge,
                        slot_id=slot_id,
                    )
                    fee      = billing["fee"]
                    dur_str  = billing["duration_str"]
                    dur_min  = billing["duration_min"]
                    now = datetime.now().isoformat()
                    conn.execute("""
                        UPDATE transactions
                        SET exit_time=?, total_fee=?, duration_min=?,
                            duration_str=?, is_surge=?, rate_used=?
                        WHERE vehicle_id=? AND exit_time IS NULL
                    """, (now, fee, dur_min, dur_str, int(is_surge), rate, fasttag_id))
                    conn.execute(
                        "DELETE FROM active_sessions WHERE vehicle_id=?",
                        (fasttag_id,),
                    )
                    conn.execute("""
                        UPDATE slots
                        SET is_occupied=0, current_vehicle=NULL, entry_time=NULL,
                            is_vip=0, booked_by=NULL, booked_at=NULL
                        WHERE id=?
                    """, (slot_id,))
                    log_change(conn, "exit", slot_id)
                    conn.commit()
            return jsonify({
                "success":      True,
                "action":       "exit",
                "vehicle_id":   fasttag_id,
                "slot_id":      slot_id,
                "fee":          fee,
                "duration_str": dur_str,
                "is_vip":       is_vip,
                "is_ev":        is_ev,
                "message":      f"Exit processed. ₹{fee} due for {dur_str}.",
            })

        else:
            # ── ENTRY flow ─────────────────────────────────────────────────────
            is_ev   = _gm.is_ev_fasttag(fasttag_id)
            slot_id = _gm.pick_slot(is_ev, "http://localhost:5000")
            if not slot_id:
                return jsonify({"success": False, "message": "No free slots available"}), 409

            with _db_lock:
                with get_db() as conn:
                    if is_blacklisted(fasttag_id, conn):
                        log_alert(conn, "BLACKLIST_ENTRY", slot_id, fasttag_id,
                                  "Blacklisted vehicle QR scan")
                        conn.commit()
                        return jsonify({
                            "success": False,
                            "message": "Vehicle is blacklisted.",
                            "alert": "BLACKLIST",
                        }), 403
                    vip = is_vip_db(fasttag_id, conn)
                    is_ev = slot_id in EV_SLOTS
                    now   = datetime.now().isoformat()
                    conn.execute("""
                        UPDATE slots
                        SET is_occupied=1, current_vehicle=?, entry_time=?,
                            is_vip=?, is_ev=?, booked_by=NULL, booked_at=NULL
                        WHERE id=?
                    """, (fasttag_id, now, int(vip), int(is_ev), slot_id))
                    conn.execute("""
                        INSERT OR REPLACE INTO active_sessions
                            (vehicle_id, slot_id, entry_time, is_vip)
                        VALUES (?,?,?,?)
                    """, (fasttag_id, slot_id, now, int(vip)))
                    conn.execute("""
                        INSERT INTO transactions
                            (vehicle_id, slot_id, entry_time, is_vip, is_ev)
                        VALUES (?,?,?,?,?)
                    """, (fasttag_id, slot_id, now, int(vip), int(is_ev)))
                    log_change(conn, "entry", slot_id,
                               json.dumps({"vehicle": fasttag_id}))
                    conn.commit()
            return jsonify({
                "success":    True,
                "action":     "entry",
                "vehicle_id": fasttag_id,
                "slot_id":    slot_id,
                "is_vip":     vip,
                "is_ev":      is_ev,
                "message":    f"Entry logged. Slot P{slot_id} → {fasttag_id}.",
            })

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/camera/update", methods=["POST"])
def camera_update():
    try:
        data     = request.json or {}
        slot_id  = str(data.get("slot_id", "")).strip()
        occupied = bool(data.get("occupied", False))
        if not slot_id:
            return jsonify({"success": False, "message": "slot_id required"}), 400
        with _db_lock:
            with get_db() as conn:
                db_slot = conn.execute(
                    "SELECT id, is_occupied FROM slots WHERE id=?", (slot_id,)
                ).fetchone()
                if not db_slot:
                    return jsonify({"success": False, "message": "Slot not found"}), 404
                db_occupied = bool(db_slot["is_occupied"])

                if occupied and not db_occupied:
                    conn.execute(
                        """
                        INSERT INTO heartbeat_log(slot_id, camera_state, db_state, mismatch)
                        VALUES (?, 'occupied', 'free', 1)
                        """,
                        (slot_id,),
                    )
                    log_alert(
                        conn,
                        "UNAUTHORIZED_ACCESS",
                        slot_id,
                        None,
                        "Camera update mismatch: occupied seen without active session",
                    )

                log_change(conn, "camera", slot_id,
                           f'{{"occupied":{str(occupied).lower()}}}')
                conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/gps/arrival-prediction", methods=["POST"])
def api_gps_arrival_prediction():
    """
    Receives mocked V2X geofence events from gate_monitor.py.
    Body:
    {
      "vehicle_id": "FT-0001",
      "slot_id": "3",
      "location": {"lat":..., "lon":..., "distance_m":...},
      "prediction": {"arrival_eta_sec": 45, "confidence": 0.92}
    }
    """
    try:
        data = request.json or {}
        location = data.get("location", {})
        pred = data.get("prediction", {})

        with _db_lock:
            with get_db() as conn:
                conn.execute(
                    """
                    INSERT INTO arrival_predictions(
                        vehicle_id, slot_id, latitude, longitude,
                        distance_m, eta_seconds, confidence
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        str(data.get("vehicle_id", "")).upper() or None,
                        str(data.get("slot_id", "")) or None,
                        location.get("lat"),
                        location.get("lon"),
                        location.get("distance_m"),
                        pred.get("arrival_eta_sec"),
                        pred.get("confidence"),
                    ),
                )
                log_change(conn, "arrival_prediction", str(data.get("slot_id", "0")))
                conn.commit()

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# LONG-POLL
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/poll/<int:cursor>")
def api_poll(cursor):
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
                    slots  = conn.execute(
                        "SELECT * FROM slots ORDER BY CAST(id AS INTEGER)"
                    ).fetchall()
                    alerts = conn.execute(
                        "SELECT * FROM security_alerts WHERE resolved=0 ORDER BY id DESC LIMIT 5"
                    ).fetchall()
                return jsonify({
                    "cursor":  new_cursor,
                    "events":  [dict(r) for r in rows],
                    "slots":   [slot_to_dict(r) for r in slots],
                    "alerts":  [dict(r) for r in alerts],
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
    return jsonify({"cursor": max_c, "events": [], "slots": [], "alerts": []})


# ══════════════════════════════════════════════════════════════════════════════
# PRE-BOOKING & ADMIN
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
                if is_blacklisted(vehicle, conn):
                    return jsonify({"success": False,
                                    "message": "🚨 Vehicle Blacklisted"}), 403

                target = conn.execute(
                    "SELECT * FROM slots WHERE id=? AND is_occupied=0 AND booked_by IS NULL",
                    (slot_id,)
                ).fetchone() if slot_id else conn.execute("""
                    SELECT * FROM slots
                    WHERE is_occupied=0 AND booked_by IS NULL
                    ORDER BY CAST(id AS INTEGER) LIMIT 1
                """).fetchone()

                if not target:
                    return jsonify({"success": False, "message": "No free slot"}), 409

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
    print("  RFID Parking System — v3.0 (AI-Security & Smart Economy)")
    print("  SRM Institute of Science and Technology")
    print(f"  DB      : {os.path.abspath(DB_PATH)}")
    print(f"  Laptop  : http://localhost:5000")
    print(f"  Phone   : http://{local_ip}:5000")
    print(f"  Rate    : ₹{RATE_PER_HOUR}/hr base | ₹{DYNAMIC_RATE}/hr surge")
    print(f"  EV Fee  : ₹{EV_SURCHARGE} surcharge on slots 9 & 10")
    print(f"  PDF     : {'✅ ReportLab ready' if REPORTLAB_OK else '❌ Install reportlab'}")
    print("=" * 64)

    # Auto-start gate monitor in a background daemon thread
    try:
        import gate_monitor
        gm_thread = threading.Thread(
            target=gate_monitor.run_camera,
            args=(gate_monitor.CAMERA_INDEX, "http://localhost:5000"),
            kwargs={"preview": True},
            daemon=True,
            name="GateMonitor",
        )
        gm_thread.start()
        print("  Gate    : ✅ gate_monitor started (camera 0)")
    except Exception as _gm_err:
        print(f"  Gate    : ⚠️  gate_monitor skipped — {_gm_err}")

    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=5000)
