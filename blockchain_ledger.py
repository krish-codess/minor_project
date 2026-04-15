"""
Simulated private blockchain ledger for parking revenue audit.
Includes a smart-contract-style fee computation mirroring TIMEDIFF logic.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from typing import Dict, List

CHAIN_FILE = "blockchain_chain.json"

RATE_PER_HOUR = 30.0
DYNAMIC_RATE = 45.0
VIP_DISCOUNT = 0.50
MIN_FEE = 10.0
GRACE_MINUTES = 5
MAX_DAILY_FEE = 300.0
EV_SURCHARGE = 50.0
EV_SLOTS = {9, 10, "9", "10"}


def parse_timestamp(ts):
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)


def smart_contract_compute_fee(
    entry_ts,
    exit_ts=None,
    is_vip: bool = False,
    effective_rate: float = None,
    is_surge: bool = False,
    slot_id=None,
) -> Dict:
    """Deterministic fee contract compatible with timediff.compute_fee payload."""
    t_entry = parse_timestamp(entry_ts)
    t_exit = parse_timestamp(exit_ts) if exit_ts else datetime.now()

    if t_exit < t_entry:
        raise ValueError("Exit time cannot be before entry time")

    if is_surge:
        rate = DYNAMIC_RATE
    elif effective_rate is not None:
        rate = effective_rate
    else:
        rate = RATE_PER_HOUR

    delta = t_exit - t_entry
    total_s = int(delta.total_seconds())
    total_m = total_s // 60

    billable_m = max(0, total_m - GRACE_MINUTES)
    billable_h = billable_m / 60
    raw_fee = billable_h * rate
    fee = min(MAX_DAILY_FEE, max(MIN_FEE, math.ceil(raw_fee * 100) / 100))

    is_ev = slot_id in EV_SLOTS
    ev_extra = EV_SURCHARGE if is_ev else 0.0
    fee = min(MAX_DAILY_FEE, fee + ev_extra)

    vip_savings = 0.0
    if is_vip:
        vip_savings = round(fee * VIP_DISCOUNT, 2)
        fee = max(round(fee * (1 - VIP_DISCOUNT), 2), MIN_FEE / 2)

    hours, minutes = total_m // 60, total_m % 60

    return {
        "entry": t_entry.isoformat(),
        "exit": t_exit.isoformat(),
        "elapsed_seconds": total_s,
        "duration_min": total_m,
        "duration_str": f"{hours}h {minutes}m",
        "billable_hours": round(billable_h, 4),
        "fee": round(fee, 2),
        "is_vip": is_vip,
        "is_ev": is_ev,
        "is_surge": rate > RATE_PER_HOUR,
        "breakdown": {
            "rate_per_hour": rate,
            "is_dynamic_rate": rate > RATE_PER_HOUR,
            "grace_minutes": GRACE_MINUTES,
            "min_fee": MIN_FEE,
            "max_daily_fee": MAX_DAILY_FEE,
            "raw_fee": round(raw_fee, 2),
            "ev_surcharge": ev_extra,
            "vip_savings": vip_savings,
        },
    }


def _hash_block(block: Dict) -> str:
    payload = json.dumps(block, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _genesis_block() -> Dict:
    block = {
        "index": 0,
        "timestamp": datetime.now().isoformat(),
        "previous_hash": "0" * 64,
        "nonce": 0,
        "transaction": {"type": "genesis", "note": "RFID parking ledger"},
    }
    block["hash"] = _hash_block({k: v for k, v in block.items() if k != "hash"})
    return block


def load_chain(chain_file: str = CHAIN_FILE) -> List[Dict]:
    if not os.path.exists(chain_file):
        chain = [_genesis_block()]
        save_chain(chain, chain_file)
        return chain

    with open(chain_file, "r", encoding="utf-8") as f:
        chain = json.load(f)

    if not chain:
        chain = [_genesis_block()]
        save_chain(chain, chain_file)
    return chain


def save_chain(chain: List[Dict], chain_file: str = CHAIN_FILE) -> None:
    with open(chain_file, "w", encoding="utf-8") as f:
        json.dump(chain, f, indent=2)


def mine_transaction(transaction: Dict, chain_file: str = CHAIN_FILE, difficulty_prefix: str = "000") -> Dict:
    chain = load_chain(chain_file)
    prev = chain[-1]

    candidate = {
        "index": len(chain),
        "timestamp": datetime.now().isoformat(),
        "previous_hash": prev["hash"],
        "nonce": 0,
        "transaction": transaction,
    }

    while True:
        hash_value = _hash_block(candidate)
        if hash_value.startswith(difficulty_prefix):
            candidate["hash"] = hash_value
            break
        candidate["nonce"] += 1

    chain.append(candidate)
    save_chain(chain, chain_file)
    return candidate


def validate_chain(chain_file: str = CHAIN_FILE) -> bool:
    chain = load_chain(chain_file)
    for i in range(1, len(chain)):
        prev = chain[i - 1]
        cur = chain[i]

        expected_hash = _hash_block({k: v for k, v in cur.items() if k != "hash"})
        if cur.get("hash") != expected_hash:
            return False
        if cur.get("previous_hash") != prev.get("hash"):
            return False
    return True


if __name__ == "__main__":
    tx = {
        "vehicle_id": "FT-DEMO",
        "slot_id": "1",
        "fee": 42.0,
        "note": "demo transaction",
    }
    block = mine_transaction(tx)
    print("Mined block:", block["index"], block["hash"][:18])
    print("Chain valid:", validate_chain())
