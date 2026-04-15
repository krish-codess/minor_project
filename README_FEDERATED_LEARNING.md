# README_FEDERATED_LEARNING

## Objective

Task 1 adds a simulated Federated Learning (FL) architecture to the LPR pipeline so gate inference improves without sharing raw plate images.

## Implemented Components

- Added `federated_server.py`
  - Maintains global model weights in `federated_state.json`.
  - Aggregates client JSON updates using weighted averaging by sample count.
  - Tracks rounds, clients, and update history.
- Added `federated_client.py`
  - Generates synthetic local license-plate dataset.
  - Performs lightweight local training and creates JSON weight updates.
  - Submits updates to server via local simulation functions.
- Updated `gate_monitor.py`
  - Added FL hooks to fetch global weights (`_get_fl_weights`).
  - Updated OCR candidate ranking using FL-informed scoring (`_score_plate_candidate`).
  - Triggered a simulated local FL round after successful gate entry.
  - Preserved EasyOCR fallback and existing QR/entry/exit behavior.

## Privacy-Preserving Design

- No raw frame or plate image leaves client runtime.
- Only compact numeric model parameters are exchanged.
- This simulates real FL behavior where data remains on edge devices.

## How to Run

1. `python federated_server.py` to initialize state.
2. `python federated_client.py --client-id gate-client-1 --samples 120` to simulate one training round.
3. Run `python gate_monitor.py --simulate` or live camera mode.
4. Gate monitor uses current FL weights to rank OCR candidates.

## Research Significance (Smart City)

- Demonstrates privacy-first AI governance for intelligent mobility systems.
- Reduces central data retention risk for sensitive vehicle identities.
- Supports distributed campus/city deployment with local personalization.

## Stability Notes

- Existing LPR route, FASTag flow, blacklist checks, EV routing, and OCR fallback remain intact.
