"""
timediff.py  (v2.1)
TIMEDIFF Algorithm — Edge-local parking fee computation.
Supports VIP discounts, dynamic surge pricing, grace period, and daily cap.
"""

from datetime import datetime, timedelta
import math

RATE_PER_HOUR  = 30.0
DYNAMIC_RATE   = 45.0
VIP_DISCOUNT   = 0.50
MIN_FEE        = 10.0
GRACE_MINUTES  = 5
MAX_DAILY_FEE  = 300.0

VIP_LIST = {
    "SRM-VIP-01", "SRM-VIP-02", "SRM-VIP-03",
    "SRM-FACULTY-01", "SRM-FACULTY-02",
}


def is_vip_vehicle(vehicle_id: str) -> bool:
    return vehicle_id.strip().upper() in VIP_LIST


def get_effective_rate(occupied_count: int, total_slots: int = 10) -> float:
    ratio = occupied_count / total_slots if total_slots > 0 else 0
    return DYNAMIC_RATE if ratio > 0.80 else RATE_PER_HOUR


def parse_timestamp(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)


def compute_fee(entry_ts, exit_ts=None, is_vip: bool = False,
                effective_rate: float = None) -> dict:
    t_entry = parse_timestamp(entry_ts)
    t_exit  = parse_timestamp(exit_ts) if exit_ts else datetime.now()

    if t_exit < t_entry:
        raise ValueError("Exit time cannot be before entry time")

    rate    = effective_rate if effective_rate is not None else RATE_PER_HOUR
    delta   = t_exit - t_entry
    total_s = int(delta.total_seconds())
    total_m = total_s // 60

    billable_m = max(0, total_m - GRACE_MINUTES)
    billable_h = billable_m / 60
    raw_fee    = billable_h * rate
    fee        = min(MAX_DAILY_FEE, max(MIN_FEE, math.ceil(raw_fee * 100) / 100))

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
        "breakdown": {
            "rate_per_hour":   rate,
            "is_dynamic_rate": rate > RATE_PER_HOUR,
            "grace_minutes":   GRACE_MINUTES,
            "min_fee":         MIN_FEE,
            "max_daily_fee":   MAX_DAILY_FEE,
            "raw_fee":         round(raw_fee, 2),
            "vip_savings":     vip_savings,
        }
    }


def format_duration(seconds: int) -> str:
    m = seconds // 60
    return f"{m // 60}h {m % 60}m"


if __name__ == "__main__":
    print("TIMEDIFF v2.1 — Self Test\n" + "="*45)
    now = datetime.now()
    tests = [
        ("3 min → min fee",        timedelta(minutes=3),  False, RATE_PER_HOUR),
        ("30 min",                 timedelta(minutes=30), False, RATE_PER_HOUR),
        ("4 hours",                timedelta(hours=4),    False, RATE_PER_HOUR),
        ("10 hours → daily cap",   timedelta(hours=10),   False, RATE_PER_HOUR),
        ("VIP 30 min",             timedelta(minutes=30), True,  RATE_PER_HOUR),
        ("Surge 2h (>80% full)",   timedelta(hours=2),    False, DYNAMIC_RATE),
        ("VIP + Surge 2h",         timedelta(hours=2),    True,  DYNAMIC_RATE),
    ]
    for desc, delta, vip, rate in tests:
        r = compute_fee(now - delta, is_vip=vip, effective_rate=rate)
        print(f"  {'VIP' if vip else '   '} | ₹{rate}/hr | {desc:25s} → ₹{r['fee']}")
