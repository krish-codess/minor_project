# README_LATENCY_OPTIMIZATION

## Objective

Task 2 introduces a simulated edge inference optimization layer (TensorRT/OpenVINO style) for occupancy detection and benchmarks latency against standard inference.

## Implemented Components

- Updated `camera_monitor.py`
  - Added `OptimizedInferenceEngine` wrapper with two backends:
    - `standard`: existing detection path.
    - `optimized`: reduced-resolution fused path to simulate edge acceleration.
  - Added `benchmark_inference(...)` to compare standard vs optimized latency.
  - Added `log_performance(...)` to store metrics in `performance_logs` table.
  - Added CLI options:
    - `--backend {standard,optimized}`
    - `--benchmark`
    - `--benchmark-frames`
- Updated DB schema via `app.py:init_db`
  - Added `performance_logs` table.

## Benchmark Usage

- Example:
  - `python camera_monitor.py --slot 1 --backend optimized --benchmark --benchmark-frames 100`
- Logged metrics include:
  - standard latency avg
  - optimized latency avg
  - speedup factor
  - frame count
  - notes (P95 values)

## Research Significance (Smart City)

- Validates edge latency gains for real-time parking intelligence.
- Supports reduced queueing and faster occupancy telemetry.
- Emulates deployment migration from CPU-only nodes to accelerated inference stacks.

## Stability Notes

- Existing camera update API integration and simulation mode remain compatible.
- Occupancy behavior remains functionally aligned with prior logic.
