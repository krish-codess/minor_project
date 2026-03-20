"""
timediff.py
TIMEDIFF algorithm — Edge-local parking fee computation.

This module implements the TIMEDIFF billing algorithm referenced in the project.
It runs entirely on the local device (no cloud dependency), making it resilient
to network outages — a key architectural advantage over cloud-based competitors
(Khanna & Anand, 2021).

Formula:
  duration = EXIT_TIMESTAMP - ENTRY_TIMESTAMP   (in seconds)
  fee = max(MIN_FEE, ceil(duration_hours * RATE_PER_HOUR))

Usage:
  from timediff import compute_fee, format_duration
  session = compute_fee(entry_iso, exit_iso)
"""

from datetime import datetime, timedelta
import math

# ── Rate configuration ────────────────────────────────────────────────────────
RATE_PER_HOUR  = 30.0    # ₹ per hour
MIN_FEE        = 10.0    # ₹ minimum charge (covers first ~20 min)
GRACE_MINUTES  = 5       # free grace period before billing starts
MAX_DAILY_FEE  = 300.0   # ₹ daily cap


def parse_timestamp(ts) -> datetime:
    """Accept ISO string or datetime object."""
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)


def compute_fee(entry_ts, exit_ts=None) -> dict:
    """
    TIMEDIFF core function.

    Args:
        entry_ts : ISO 8601 string or datetime — vehicle entry time
        exit_ts  : ISO 8601 string or datetime — vehicle exit time (default: now)

    Returns:
        dict with keys:
            entry, exit, elapsed_seconds, duration_min, duration_str,
            billable_hours, fee, breakdown
    """
    t_entry = parse_timestamp(entry_ts)
    t_exit  = parse_timestamp(exit_ts) if exit_ts else datetime.now()

    if t_exit < t_entry:
        raise ValueError("Exit time cannot be before entry time")

    delta   = t_exit - t_entry
    total_s = int(delta.total_seconds())
    total_m = total_s // 60

    # Apply grace period
    billable_m = max(0, total_m - GRACE_MINUTES)
    billable_h = billable_m / 60

    # Compute raw fee
    raw_fee = billable_h * RATE_PER_HOUR

    # Apply minimum and daily cap
    fee = min(MAX_DAILY_FEE, max(MIN_FEE, math.ceil(raw_fee * 100) / 100))

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
        "breakdown": {
            "rate_per_hour":   RATE_PER_HOUR,
            "grace_minutes":   GRACE_MINUTES,
            "min_fee":         MIN_FEE,
            "max_daily_fee":   MAX_DAILY_FEE,
            "raw_fee":         round(raw_fee, 2),
        }
    }


def format_duration(seconds: int) -> str:
    """Human-readable duration string."""
    m = seconds // 60
    return f"{m // 60}h {m % 60}m"


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("TIMEDIFF Algorithm — Test Cases\n" + "="*50)

    test_cases = [
        ("Quick visit (3 min → min fee)",      timedelta(minutes=3)),
        ("Short park (30 min)",                timedelta(minutes=30)),
        ("Half day (4 hours)",                 timedelta(hours=4)),
        ("Full day (10 hours → capped)",       timedelta(hours=10)),
    ]

    now = datetime.now()
    for desc, delta in test_cases:
        entry = now - delta
        result = compute_fee(entry)
        print(f"\n{desc}")
        print(f"  Duration : {result['duration_str']}")
        print(f"  Billable : {result['billable_hours']:.2f} hrs")
        print(f"  Fee      : ₹{result['fee']}")
