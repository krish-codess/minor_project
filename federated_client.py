"""
Federated Learning Client (simulation)
Performs synthetic local training for LPR and sends only JSON model updates.
"""

from __future__ import annotations

import argparse
import random
from statistics import mean
from typing import Dict, List

from federated_server import get_global_weights, register_client, submit_client_update


def _synthetic_plate() -> str:
    states = ["TN", "KA", "MH", "AP", "KL"]
    return (
        random.choice(states)
        + f"{random.randint(1,99):02d}"
        + random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        + random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        + f"{random.randint(1,9999):04d}"
    )


def generate_synthetic_dataset(size: int = 120) -> List[dict]:
    data = []
    for _ in range(size):
        plate = _synthetic_plate()
        noisy_plate = plate
        if random.random() < 0.22:
            idx = random.randint(0, len(plate) - 1)
            noisy_plate = plate[:idx] + random.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") + plate[idx + 1 :]

        data.append(
            {
                "plate": plate,
                "ocr_out": noisy_plate,
                "has_hyphen": "-" in noisy_plate,
                "digit_ratio": sum(1 for c in noisy_plate if c.isdigit()) / max(1, len(noisy_plate)),
                "alpha_ratio": sum(1 for c in noisy_plate if c.isalpha()) / max(1, len(noisy_plate)),
            }
        )
    return data


def local_train(client_id: str, dataset_size: int = 120) -> dict:
    register_client(client_id)
    global_w = get_global_weights()
    data = generate_synthetic_dataset(dataset_size)

    mismatch_rate = mean(1.0 if row["plate"] != row["ocr_out"] else 0.0 for row in data)
    avg_digit_ratio = mean(row["digit_ratio"] for row in data)
    avg_alpha_ratio = mean(row["alpha_ratio"] for row in data)

    local_weights: Dict[str, float] = dict(global_w)

    local_weights["ocr_threshold"] = min(0.9, max(0.2, global_w["ocr_threshold"] + (mismatch_rate - 0.2) * 0.08))
    local_weights["digit_bonus"] = min(0.4, max(0.01, global_w["digit_bonus"] + (avg_digit_ratio - 0.4) * 0.06))
    local_weights["alpha_bonus"] = min(0.4, max(0.01, global_w["alpha_bonus"] + (avg_alpha_ratio - 0.4) * 0.06))
    local_weights["hyphen_bonus"] = min(0.3, max(0.0, global_w["hyphen_bonus"] + random.uniform(-0.01, 0.01)))
    local_weights["char_confidence_boost"] = min(
        1.0, max(0.1, global_w["char_confidence_boost"] + (0.5 - mismatch_rate) * 0.05)
    )

    local_accuracy = round(1.0 - mismatch_rate, 4)

    update = {
        "client_id": client_id,
        "num_samples": len(data),
        "weights": {k: round(v, 6) for k, v in local_weights.items()},
        "metrics": {
            "local_accuracy": local_accuracy,
            "mismatch_rate": round(mismatch_rate, 4),
            "avg_digit_ratio": round(avg_digit_ratio, 4),
            "avg_alpha_ratio": round(avg_alpha_ratio, 4),
        },
    }
    return update


def run_round(client_id: str, dataset_size: int = 120) -> dict:
    update = local_train(client_id=client_id, dataset_size=dataset_size)
    return submit_client_update(update)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Federated LPR client simulator")
    parser.add_argument("--client-id", default="gate-client-1", help="Unique FL client identifier")
    parser.add_argument("--samples", type=int, default=120, help="Synthetic sample count")
    args = parser.parse_args()

    state = run_round(client_id=args.client_id, dataset_size=args.samples)
    print(f"Round {state['round']} complete | clients={len(state['clients'])}")
    print("Global weights:", state["global_weights"])
