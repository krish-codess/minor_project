# README_MARL_PRICING

## Objective

Task 4 replaces static occupancy-only surge logic with a lightweight Multi-Agent Reinforcement Learning (MARL) simulation that predicts demand and adjusts rates proactively.

## Implemented Components

- Updated `timediff.py`
  - Replaced `get_effective_rate(...)` internals with MARL-style predictive policy.
  - Added historical demand loading from `parking.db` transactions.
  - Introduced multi-agent simulation states:
    - demand predictor (hourly trend)
    - occupancy pressure assessor
    - revenue stabilizer (Q-value action selection)
  - Added fallback to legacy threshold logic for sparse history.

## Rate Policy Behavior

- Uses current-hour and next-hour historical transaction counts.
- Predicts demand state: low / medium / high.
- Applies simulated Q-table action values to choose base vs surge.
- Proactively applies surge in high predicted demand windows.

## Research Significance (Smart City)

- Moves from reactive to predictive congestion pricing.
- Better demand shaping for peak campus/city windows.
- Illustrates RL-driven policy adaptation using operational history.

## Stability Notes

- `compute_fee(...)` contract and response schema are preserved.
- EV surcharge and VIP discount semantics remain unchanged.
- If transaction history is insufficient, behavior falls back to legacy logic.
