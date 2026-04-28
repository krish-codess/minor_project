"""
timediff.py  (v3.0 — AI-Security & Smart Economy)
TIMEDIFF Algorithm — Edge-local parking fee computation.

New in v3.0:
  • is_ev_slot  — adds ₹50 EV charging surcharge for slots 9 & 10
  • is_surge    — explicit surge flag (also auto-computed from occupancy)
  • compute_fee accepts slot_id for EV detection
"""

from datetime import datetime, timedelta
from collections import defaultdict
import math
import os
import random
import sqlite3

# ── Rate configuration ────────────────────────────────────────────────────────
RATE_PER_HOUR  = 30.0
DYNAMIC_RATE   = 45.0    # 1.5× surge rate
VIP_DISCOUNT   = 0.50
MIN_FEE        = 10.0
GRACE_MINUTES  = 5
MAX_DAILY_FEE  = 300.0
EV_SURCHARGE   = 50.0    # ₹50 for EV-ready slots 9 & 10
EV_SLOTS       = {9, 10, "9", "10"}
DB_PATH        = "parking.db"
MARL_MIN_HISTORY = 20

# ── VIP registry (fallback if not in DB members table) ───────────────────────
VIP_LIST = {
    "SRM-VIP-01", "SRM-VIP-02", "SRM-VIP-03",
    "SRM-FACULTY-01", "SRM-FACULTY-02",
}


def is_vip_vehicle(vehicle_id: str) -> bool:
    return vehicle_id.strip().upper() in VIP_LIST


def _legacy_rate(occupied_count: int, total_slots: int = 10) -> float:
    ratio = occupied_count / total_slots if total_slots > 0 else 0
    return DYNAMIC_RATE if ratio > 0.80 else RATE_PER_HOUR


def _load_hourly_history(db_path: str = DB_PATH):
    if not os.path.exists(db_path):
        return None

    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            rows = conn.execute(
                """
                SELECT CAST(strftime('%H', entry_time) AS INTEGER) AS hour,
                       COUNT(*) AS c
                FROM transactions
                WHERE entry_time IS NOT NULL
                GROUP BY hour
                ORDER BY hour
                """
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    hourly_counts = {int(hour): int(cnt) for hour, cnt in rows if hour is not None}
    total = sum(hourly_counts.values())
    return {
        "hourly_counts": hourly_counts,
        "total": total,
    }


def _marl_predictive_rate(occupied_count: int, total_slots: int = 10) -> float:
    """
    Lightweight MARL-style predictive pricing:
    - Agent A: near-term demand predictor from hourly history
    - Agent B: occupancy pressure assessor from current occupancy
    - Agent C: revenue stabilizer selecting rate action from Q-values
    """
    history = _load_hourly_history()
    if not history or history["total"] < MARL_MIN_HISTORY:
        return _legacy_rate(occupied_count, total_slots)

    hourly_counts = history["hourly_counts"]
    current_hour = datetime.now().hour
    next_hour = (current_hour + 1) % 24

    cur_cnt = hourly_counts.get(current_hour, 0)
    nxt_cnt = hourly_counts.get(next_hour, 0)
    global_avg = history["total"] / 24.0

    demand_ratio = (0.45 * cur_cnt + 0.55 * nxt_cnt) / max(1.0, global_avg)
    occupancy_ratio = occupied_count / total_slots if total_slots > 0 else 0.0

    if demand_ratio >= 1.35:
        demand_state = "high"
    elif demand_ratio >= 0.90:
        demand_state = "medium"
    else:
        demand_state = "low"

    # Multi-agent action values (simulated learned Q-values)
    q_table = {
        "low": {"base": 0.92, "surge": 0.48},
        "medium": {"base": 0.77, "surge": 0.86},
        "high": {"base": 0.40, "surge": 1.05},
    }

    # Occupancy pressure and small exploration jitter emulate ongoing learning.
    q_base = q_table[demand_state]["base"] - (occupancy_ratio * 0.08)
    q_surge = q_table[demand_state]["surge"] + (occupancy_ratio * 0.10)
    q_surge += random.uniform(-0.015, 0.015)

    if demand_state == "high" and occupancy_ratio >= 0.55:
        return DYNAMIC_RATE
    if q_surge > q_base:
        return DYNAMIC_RATE

    return RATE_PER_HOUR


def get_effective_rate(occupied_count: int, total_slots: int = 10) -> float:
    """
    MARL pricing hook (v4 research simulation).
    Falls back to legacy occupancy threshold when history is sparse/unavailable.
    """
    return _marl_predictive_rate(occupied_count, total_slots)


def parse_timestamp(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)


def compute_fee(
    entry_ts,
    exit_ts        = None,
    is_vip         : bool  = False,
    effective_rate : float = None,
    is_surge       : bool  = False,
    slot_id                = None,
) -> dict:
    """
    TIMEDIFF core — v3.0.

    Args:
        entry_ts       : ISO string or datetime
        exit_ts        : ISO string or datetime (default: now)
        is_vip         : Apply 50% VIP discount
        effective_rate : Override rate (dynamic pricing)
        is_surge       : Explicit surge flag (overrides rate to DYNAMIC_RATE)
        slot_id        : If 9 or 10, add EV surcharge of ₹50

    Returns dict with fee, breakdown, EV info, VIP info.
    """
    t_entry = parse_timestamp(entry_ts)
    t_exit  = parse_timestamp(exit_ts) if exit_ts else datetime.now()

    if t_exit < t_entry:
        raise ValueError("Exit time cannot be before entry time")

    # Determine rate
    if is_surge:
        rate = DYNAMIC_RATE
    elif effective_rate is not None:
        rate = effective_rate
    else:
        rate = RATE_PER_HOUR

    delta   = t_exit - t_entry
    total_s = int(delta.total_seconds())
    total_m = total_s // 60

    billable_m = max(0, total_m - GRACE_MINUTES)
    billable_h = billable_m / 60
    raw_fee    = billable_h * rate
    fee        = min(MAX_DAILY_FEE, max(MIN_FEE, math.ceil(raw_fee * 100) / 100))

    # EV surcharge
    is_ev    = slot_id in EV_SLOTS
    ev_extra = EV_SURCHARGE if is_ev else 0.0
    fee      = min(MAX_DAILY_FEE, fee + ev_extra)

    # VIP discount (applied after EV surcharge)
    vip_savings = 0.0
    if is_vip:
        vip_savings = round(fee * VIP_DISCOUNT, 2)
        fee = max(round(fee * (1 - VIP_DISCOUNT), 2), MIN_FEE / 2)

    hours, minutes = total_m // 60, total_m % 60

    return {
        "entry":           t_entry.isoformat(),
        "exit":            t_exit.isoformat(),
        "elapsed_seconds": total_s,
        "duration_min":    total_m,
        "duration_str":    f"{hours}h {minutes}m",
        "billable_hours":  round(billable_h, 4),
        "fee":             round(fee, 2),
        "is_vip":          is_vip,
        "is_ev":           is_ev,
        "is_surge":        rate > RATE_PER_HOUR,
        "breakdown": {
            "rate_per_hour":   rate,
            "is_dynamic_rate": rate > RATE_PER_HOUR,
            "grace_minutes":   GRACE_MINUTES,
            "min_fee":         MIN_FEE,
            "max_daily_fee":   MAX_DAILY_FEE,
            "raw_fee":         round(raw_fee, 2),
            "ev_surcharge":    ev_extra,
            "vip_savings":     vip_savings,
        }
    }


def compute_green_credits(
    entry_ts,
    exit_ts=None,
    slot_id=None,
    effective_rate: float = None,
    is_surge: bool = False,
) -> dict:
    """
    Eco gamification model for Green Credits.

    Credit triggers:
    - Prompt Parker: <= 30 min session
    - EV Champion: parked in EV-ready slots
    - Off-Peak Owl: non-surge period (base pricing hours)
    """
    t_entry = parse_timestamp(entry_ts)
    t_exit = parse_timestamp(exit_ts) if exit_ts else datetime.now()

    duration_min = max(0, int((t_exit - t_entry).total_seconds()) // 60)
    on_ev_slot = slot_id in EV_SLOTS

    active_rate = DYNAMIC_RATE if is_surge else (
        effective_rate if effective_rate is not None else RATE_PER_HOUR
    )
    off_peak = active_rate <= RATE_PER_HOUR

    points = 5  # participation baseline
    reasons = ["Session Completed (+5)"]
    badges = []

    if duration_min <= 30:
        points += 25
        reasons.append("Prompt Parking <= 30 min (+25)")
        badges.append("Prompt Parker")

    if on_ev_slot:
        points += 20
        reasons.append("Used EV-ready slot (+20)")
        badges.append("EV Champion")

    if off_peak:
        points += 15
        reasons.append("Off-peak arrival window (+15)")
        badges.append("Off-Peak Owl")

    if points >= 45:
        badges.append("Eco-Driver")

    return {
        "points": points,
        "duration_min": duration_min,
        "off_peak": off_peak,
        "is_ev": on_ev_slot,
        "badges": sorted(set(badges)),
        "reasons": reasons,
    }


def format_duration(seconds: int) -> str:
    m = seconds // 60
    return f"{m // 60}h {m % 60}m"


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("TIMEDIFF v3.0 — Self Test\n" + "="*55)
    now = datetime.now()
    tests = [
        ("30 min guest",              timedelta(minutes=30), False, False, RATE_PER_HOUR, "1"),
        ("30 min VIP",                timedelta(minutes=30), True,  False, RATE_PER_HOUR, "1"),
        ("2h surge",                  timedelta(hours=2),    False, True,  DYNAMIC_RATE,  "1"),
        ("2h EV slot 9",              timedelta(hours=2),    False, False, RATE_PER_HOUR, "9"),
        ("2h EV slot 10 + VIP",       timedelta(hours=2),    True,  False, RATE_PER_HOUR, "10"),
        ("2h EV + surge",             timedelta(hours=2),    False, True,  DYNAMIC_RATE,  "9"),
    ]
    for desc, delta, vip, surge, rate, sid in tests:
        r = compute_fee(now - delta, is_vip=vip, effective_rate=rate,
                        is_surge=surge, slot_id=sid)
        tags = []
        if vip:   tags.append("VIP")
        if surge: tags.append("SURGE")
        if r["is_ev"]: tags.append("EV+₹50")
        print(f"  {desc:35s} → ₹{r['fee']:6.2f}  {' '.join(tags)}")
