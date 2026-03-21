"""
timediff.py  (v2.0 — Production Upgrade)
TIMEDIFF Algorithm — Edge-local parking fee computation with VIP & dynamic pricing.

Formula:
  duration = EXIT_TIMESTAMP - ENTRY_TIMESTAMP   (in seconds)
  billable  = max(0, duration_minutes - GRACE_MINUTES)
  raw_fee   = (billable / 60) * effective_rate
  fee       = clamp(raw_fee, MIN_FEE, MAX_DAILY_FEE)

VIP vehicles receive a 50% discount after all other rules are applied.
Dynamic pricing raises the rate to 1.5× when occupancy exceeds 80%.

Usage:
  from timediff import compute_fee, format_duration
  result = compute_fee(entry_iso, exit_iso, is_vip=True, dynamic_rate=45.0)
"""

from datetime import datetime, timedelta
import math

# ── Rate configuration ────────────────────────────────────────────────────────
RATE_PER_HOUR    = 30.0    # ₹ per hour (base)
DYNAMIC_RATE     = 45.0    # ₹ per hour when >80% slots occupied
VIP_DISCOUNT     = 0.50    # 50% discount for VIP vehicles
MIN_FEE          = 10.0    # ₹ minimum charge
GRACE_MINUTES    = 5       # free grace period before billing starts
MAX_DAILY_FEE    = 300.0   # ₹ daily cap

# ── VIP registry (in production: load from DB / config file) ──────────────────
VIP_LIST = {
    "SRM-VIP-01", "SRM-VIP-02", "SRM-VIP-03",
    "SRM-FACULTY-01", "SRM-FACULTY-02",
}


def is_vip_vehicle(vehicle_id: str) -> bool:
    """Check if a vehicle ID qualifies for VIP discount."""
    return vehicle_id.strip().upper() in VIP_LIST


def get_effective_rate(occupied_count: int, total_slots: int = 10) -> float:
    """
    Dynamic pricing: return DYNAMIC_RATE when >80% slots are full,
    otherwise return base RATE_PER_HOUR.
    """
    occupancy_ratio = occupied_count / total_slots if total_slots > 0 else 0
    return DYNAMIC_RATE if occupancy_ratio > 0.80 else RATE_PER_HOUR


def parse_timestamp(ts) -> datetime:
    """Accept ISO string or datetime object."""
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)


def compute_fee(
    entry_ts,
    exit_ts=None,
    is_vip: bool = False,
    effective_rate: float = None,
) -> dict:
    """
    TIMEDIFF core function (v2.0).

    Args:
        entry_ts       : ISO 8601 string or datetime — vehicle entry time
        exit_ts        : ISO 8601 string or datetime — vehicle exit time (default: now)
        is_vip         : If True, apply VIP discount (50%) to the final fee
        effective_rate : Override rate (for dynamic pricing). Defaults to RATE_PER_HOUR.

    Returns:
        dict with keys:
            entry, exit, elapsed_seconds, duration_min, duration_str,
            billable_hours, fee, is_vip, breakdown
    """
    t_entry = parse_timestamp(entry_ts)
    t_exit  = parse_timestamp(exit_ts) if exit_ts else datetime.now()

    if t_exit < t_entry:
        raise ValueError("Exit time cannot be before entry time")

    rate = effective_rate if effective_rate is not None else RATE_PER_HOUR

    delta   = t_exit - t_entry
    total_s = int(delta.total_seconds())
    total_m = total_s // 60

    # Apply grace period
    billable_m = max(0, total_m - GRACE_MINUTES)
    billable_h = billable_m / 60

    # Compute raw fee
    raw_fee = billable_h * rate

    # Apply minimum and daily cap
    fee = min(MAX_DAILY_FEE, max(MIN_FEE, math.ceil(raw_fee * 100) / 100))

    # Apply VIP discount
    vip_savings = 0.0
    if is_vip:
        vip_savings = round(fee * VIP_DISCOUNT, 2)
        fee = round(fee * (1 - VIP_DISCOUNT), 2)
        fee = max(fee, MIN_FEE / 2)  # VIPs still pay a minimum of ₹5

    hours   = total_m // 60
    minutes = total_m %  60

    return {
        "entry":            t_entry.isoformat(),
        "exit":             t_exit.isoformat(),
        "elapsed_seconds":  total_s,
        "duration_min":     total_m,
        "duration_str":     f"{hours}h {minutes}m",
        "billable_hours":   round(billable_h, 4),
        "fee":              round(fee, 2),
        "is_vip":           is_vip,
        "breakdown": {
            "rate_per_hour":   rate,
            "is_dynamic_rate": rate > RATE_PER_HOUR,
            "grace_minutes":   GRACE_MINUTES,
            "min_fee":         MIN_FEE,
            "max_daily_fee":   MAX_DAILY_FEE,
            "raw_fee":         round(raw_fee, 2),
            "vip_discount":    f"{int(VIP_DISCOUNT*100)}%" if is_vip else "N/A",
            "vip_savings":     vip_savings,
        }
    }


def format_duration(seconds: int) -> str:
    """Human-readable duration string."""
    m = seconds // 60
    return f"{m // 60}h {m % 60}m"


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("TIMEDIFF Algorithm v2.0 — Test Cases\n" + "="*55)
    now = datetime.now()

    test_cases = [
        ("Quick visit (3 min → min fee)",       timedelta(minutes=3),  False, RATE_PER_HOUR),
        ("Short park (30 min)",                 timedelta(minutes=30), False, RATE_PER_HOUR),
        ("Half day (4 hours)",                  timedelta(hours=4),    False, RATE_PER_HOUR),
        ("Full day (10 hours → capped)",        timedelta(hours=10),   False, RATE_PER_HOUR),
        ("VIP short park (30 min, 50% off)",    timedelta(minutes=30), True,  RATE_PER_HOUR),
        ("Dynamic rate — 80%+ occupancy (2h)",  timedelta(hours=2),    False, DYNAMIC_RATE),
        ("VIP + Dynamic rate (2h)",             timedelta(hours=2),    True,  DYNAMIC_RATE),
    ]

    for desc, delta, vip, rate in test_cases:
        entry = now - delta
        result = compute_fee(entry, is_vip=vip, effective_rate=rate)
        vip_label  = "VIP ✓" if vip else "     "
        rate_label = f"₹{rate}/hr"
        print(f"\n{desc}")
        print(f"  {vip_label} | Rate: {rate_label} | Duration: {result['duration_str']} | Fee: ₹{result['fee']}")
        if vip:
            print(f"  Savings: ₹{result['breakdown']['vip_savings']}")
