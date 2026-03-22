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
import sqlite3, threading, random, string, os, time, socket

from timediff import (
    compute_fee, is_vip_vehicle, get_effective_rate,
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
        """)

        # Seed slots
        if conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 0:
            conn.executemany("INSERT INTO slots(id, is_ev) VALUES(?,?)",
                             [(str(i), 1 if i in (9, 10) else 0)
                              for i in range(1, TOTAL_SLOTS + 1)])

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
                               plate_verified=plate_verified)
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
                               is_vip=is_vip, is_ev=is_ev, plate_verified=True)
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

                vip   = is_vip_db(vehicle, conn)
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
                log_change(conn, "entry", slot_id)
                conn.commit()

        return jsonify({
            "success":        True,
            "message":        f"Entry logged. Slot {slot_id} → {vehicle}",
            "entry_time":     now,
            "slot_id":        slot_id,
            "is_vip":         vip,
            "is_ev":          is_ev,
            "plate_verified": plate_verified,
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
                if not conn.execute(
                    "SELECT id FROM slots WHERE id=?", (slot_id,)
                ).fetchone():
                    return jsonify({"success": False, "message": "Slot not found"}), 404
                log_change(conn, "camera", slot_id,
                           f'{{"occupied":{str(occupied).lower()}}}')
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
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=5000)
