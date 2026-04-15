"""
Federated Learning Server (simulation)
Maintains a global lightweight model and aggregates JSON weight updates.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, List

STATE_FILE = "federated_state.json"


def _default_state() -> dict:
    return {
        "round": 0,
        "global_weights": {
            "char_confidence_boost": 0.5,
            "ocr_threshold": 0.4,
            "hyphen_bonus": 0.05,
            "digit_bonus": 0.05,
            "alpha_bonus": 0.05,
        },
        "clients": {},
        "history": [],
        "updated_at": datetime.now().isoformat(),
    }


def load_state(state_file: str = STATE_FILE) -> dict:
    if not os.path.exists(state_file):
        state = _default_state()
        save_state(state, state_file)
        return state

    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    state.setdefault("round", 0)
    state.setdefault("global_weights", _default_state()["global_weights"])
    state.setdefault("clients", {})
    state.setdefault("history", [])
    state.setdefault("updated_at", datetime.now().isoformat())
    return state


def save_state(state: dict, state_file: str = STATE_FILE) -> None:
    state["updated_at"] = datetime.now().isoformat()
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def register_client(client_id: str, state_file: str = STATE_FILE) -> dict:
    state = load_state(state_file)
    state["clients"].setdefault(client_id, {
        "last_round": 0,
        "last_seen": datetime.now().isoformat(),
        "updates": 0,
    })
    state["clients"][client_id]["last_seen"] = datetime.now().isoformat()
    save_state(state, state_file)
    return state["clients"][client_id]


def get_global_weights(state_file: str = STATE_FILE) -> Dict[str, float]:
    return load_state(state_file)["global_weights"]


def aggregate_updates(client_updates: List[dict], state_file: str = STATE_FILE) -> dict:
    """
    Aggregates incoming client updates using weighted mean.
    Each update payload expected format:
      {
        "client_id": "gate-a",
        "num_samples": 120,
        "weights": {"ocr_threshold": 0.47, ...},
        "metrics": {"local_accuracy": 0.91}
      }
    """
    if not client_updates:
        return load_state(state_file)

    state = load_state(state_file)
    base = dict(state["global_weights"])

    total_samples = sum(max(1, int(u.get("num_samples", 1))) for u in client_updates)

    for key in base.keys():
        weighted = 0.0
        for update in client_updates:
            samples = max(1, int(update.get("num_samples", 1)))
            weights = update.get("weights", {})
            weighted += float(weights.get(key, base[key])) * samples
        base[key] = round(weighted / max(1, total_samples), 6)

    state["round"] += 1
    state["global_weights"] = base

    for update in client_updates:
        client_id = str(update.get("client_id", "unknown"))
        state["clients"].setdefault(
            client_id,
            {
                "last_round": 0,
                "last_seen": datetime.now().isoformat(),
                "updates": 0,
            },
        )
        state["clients"][client_id]["last_round"] = state["round"]
        state["clients"][client_id]["last_seen"] = datetime.now().isoformat()
        state["clients"][client_id]["updates"] = int(state["clients"][client_id].get("updates", 0)) + 1

    state["history"].append({
        "round": state["round"],
        "num_clients": len(client_updates),
        "total_samples": total_samples,
        "timestamp": datetime.now().isoformat(),
    })

    save_state(state, state_file)
    return state


def submit_client_update(client_update: dict, state_file: str = STATE_FILE) -> dict:
    return aggregate_updates([client_update], state_file)


if __name__ == "__main__":
    state = load_state()
    print("Federated Server State")
    print(json.dumps(state, indent=2))
