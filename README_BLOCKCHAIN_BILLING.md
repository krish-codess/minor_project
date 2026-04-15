# README_BLOCKCHAIN_BILLING

## Objective

Task 3 adds a simulated private blockchain ledger and smart-contract billing function to create an immutable financial audit trail for each exit transaction.

## Implemented Components

- Added `blockchain_ledger.py`
  - Implements hash-linked blockchain blocks with `hashlib` + `json`.
  - Adds proof-like mining (`difficulty_prefix`) in simulation.
  - Includes `smart_contract_compute_fee(...)` mirroring TIMEDIFF billing structure.
  - Supports chain validation (`validate_chain`).
- Updated `app.py`
  - Imported blockchain functions.
  - In `/api/exit`, computes contract parity billing and mines a block for successful exits.
  - Persists metadata into new `blockchain_logs` table.
  - Returns `block_hash` and `block_index` in exit session response.
  - Uses fail-soft mining: billing flow continues even if mining fails.
- Updated DB schema via `app.py:init_db`
  - Added `blockchain_logs` table.

## Smart Contract Mapping

- Contract function contains fee rules equivalent to baseline:
  - grace period
  - dynamic rate handling
  - EV surcharge
  - VIP discount
  - min/max bounds

## Research Significance (Smart City)

- Enables tamper-evident municipal revenue auditing.
- Improves trust in automated toll/parking ecosystems.
- Demonstrates accountable machine-billed mobility infrastructure.

## Stability Notes

- Existing TIMEDIFF flow and receipt generation remain active.
- Blockchain is additive and does not block successful exit settlement.
