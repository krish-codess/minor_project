"""
camera_monitor.py
OpenCV-based slot occupancy detection using laptop camera.
Runs as a background thread and PATCHes the parking API when status changes.

Usage:
  python camera_monitor.py [--slot 1] [--camera 0] [--api http://localhost:5000]

This simulates what hardware ultrasonic/IR sensors would do in the production system.
The camera watches a defined region-of-interest (ROI) for each slot and detects
whether a vehicle is present by measuring pixel intensity change vs a baseline.
"""

import cv2
import numpy as np
import time
import argparse
import requests
import threading
import sqlite3
import statistics

# ─── Configuration ──────────────────────────────────────────────────────────
MOTION_THRESHOLD   = 25     # pixel difference to count as "occupied"
OCCUPIED_RATIO     = 0.08   # fraction of pixels that must differ
STABLE_FRAMES      = 8      # consecutive frames before state change
POLL_INTERVAL      = 0.5    # seconds between frames
BASELINE_FRAMES    = 20     # frames to build baseline on startup
DB_PATH            = "parking.db"


def detect_occupancy(frame: np.ndarray, baseline: np.ndarray) -> bool:
    """
    TIMEDIFF companion: returns True if slot appears occupied.
    Compares current frame to baseline using absolute pixel difference.
    """
    gray_now  = cv2.cvtColor(frame,    cv2.COLOR_BGR2GRAY)
    gray_base = cv2.cvtColor(baseline, cv2.COLOR_BGR2GRAY)

    diff  = cv2.absdiff(gray_now, gray_base)
    _, thresh = cv2.threshold(diff, MOTION_THRESHOLD, 255, cv2.THRESH_BINARY)

    changed_pixels = np.count_nonzero(thresh)
    total_pixels   = thresh.shape[0] * thresh.shape[1]
    ratio = changed_pixels / total_pixels

    return ratio > OCCUPIED_RATIO


class OptimizedInferenceEngine:
    """
    Simulates backend choices:
    - standard: original OpenCV path
    - optimized: lower-resolution + fused threshold behavior (TensorRT/OpenVINO style)
    """

    def __init__(self, backend: str = "standard"):
        self.backend = backend.lower()

    def infer(self, frame: np.ndarray, baseline: np.ndarray) -> bool:
        if self.backend == "optimized":
            return self._optimized_infer(frame, baseline)
        return detect_occupancy(frame, baseline)

    def _optimized_infer(self, frame: np.ndarray, baseline: np.ndarray) -> bool:
        small_now = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        small_base = cv2.resize(baseline, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        gray_now = cv2.cvtColor(small_now, cv2.COLOR_BGR2GRAY)
        gray_base = cv2.cvtColor(small_base, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray_now, gray_base)
        _, thresh = cv2.threshold(diff, MOTION_THRESHOLD - 2, 255, cv2.THRESH_BINARY)
        ratio = np.count_nonzero(thresh) / max(1, (thresh.shape[0] * thresh.shape[1]))
        return ratio > (OCCUPIED_RATIO * 0.9)


def _ensure_performance_table(db_path: str = DB_PATH):
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS performance_logs (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                component             TEXT NOT NULL,
                mode                  TEXT NOT NULL,
                slot_id               TEXT,
                standard_latency_ms   REAL,
                optimized_latency_ms  REAL,
                speedup_factor        REAL,
                frames                INTEGER,
                notes                 TEXT,
                created_at            TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.commit()


def log_performance(slot_id: int, standard_ms: float, optimized_ms: float, frames: int, notes: str = ""):
    _ensure_performance_table(DB_PATH)
    speedup = (standard_ms / optimized_ms) if optimized_ms > 0 else None
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute(
            """
            INSERT INTO performance_logs(
                component, mode, slot_id, standard_latency_ms,
                optimized_latency_ms, speedup_factor, frames, notes
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "camera_monitor",
                "benchmark",
                str(slot_id),
                float(round(standard_ms, 4)),
                float(round(optimized_ms, 4)),
                float(round(speedup, 4)) if speedup else None,
                int(frames),
                notes,
            ),
        )
        conn.commit()


def benchmark_inference(cap: cv2.VideoCapture, baseline: np.ndarray, frames: int = 80) -> dict:
    std_engine = OptimizedInferenceEngine("standard")
    opt_engine = OptimizedInferenceEngine("optimized")

    std_lat = []
    opt_lat = []

    captured = 0
    while captured < frames:
        ret, frame = cap.read()
        if not ret:
            continue

        t0 = time.perf_counter()
        std_engine.infer(frame, baseline)
        std_lat.append((time.perf_counter() - t0) * 1000.0)

        t1 = time.perf_counter()
        opt_engine.infer(frame, baseline)
        opt_lat.append((time.perf_counter() - t1) * 1000.0)

        captured += 1

    return {
        "standard_avg_ms": statistics.mean(std_lat) if std_lat else 0.0,
        "optimized_avg_ms": statistics.mean(opt_lat) if opt_lat else 0.0,
        "standard_p95_ms": float(np.percentile(std_lat, 95)) if std_lat else 0.0,
        "optimized_p95_ms": float(np.percentile(opt_lat, 95)) if opt_lat else 0.0,
        "frames": captured,
    }


class SlotMonitor:
    """Monitors a single parking slot via camera ROI."""

    def __init__(self, slot_id: int, camera_index: int = 0, api_base: str = "http://localhost:5000", backend: str = "standard"):
        self.slot_id      = slot_id
        self.api_base     = api_base
        self.cap          = cv2.VideoCapture(camera_index)
        self.baseline     = None
        self.current_state = False   # False = free, True = occupied
        self.stable_count  = 0
        self.running       = False
        self.engine        = OptimizedInferenceEngine(backend)

        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera {camera_index}")

    def _build_baseline(self):
        """Capture baseline frames (empty slot)."""
        print(f"[Slot {self.slot_id}] Building baseline ({BASELINE_FRAMES} frames)...")
        frames = []
        for _ in range(BASELINE_FRAMES):
            ret, frame = self.cap.read()
            if ret:
                frames.append(frame)
            time.sleep(0.1)
        self.baseline = np.mean(frames, axis=0).astype(np.uint8)
        print(f"[Slot {self.slot_id}] Baseline ready.")

    def _notify_api(self, occupied: bool):
        """Push state change to the Flask API."""
        # In a real system this would auto-trigger entry/exit
        # For demo: just log it
        status = "OCCUPIED" if occupied else "FREE"
        print(f"[Slot {self.slot_id}] Camera detected: {status}")
        try:
            requests.post(f"{self.api_base}/api/camera/update",
                          json={"slot_id": str(self.slot_id), "occupied": occupied},
                          timeout=2)
        except Exception as e:
            print(f"[Slot {self.slot_id}] API notify failed: {e}")

    def run(self):
        self.running = True
        self._build_baseline()

        print(f"[Slot {self.slot_id}] Monitoring started. Press Ctrl-C to stop.")

        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(POLL_INTERVAL)
                continue

            detected = self.engine.infer(frame, self.baseline)

            if detected == self.current_state:
                self.stable_count = 0
            else:
                self.stable_count += 1
                if self.stable_count >= STABLE_FRAMES:
                    self.current_state = detected
                    self.stable_count  = 0
                    self._notify_api(detected)

            # ── Live preview with overlay ──────────────────────────────────
            display = frame.copy()
            color   = (0, 0, 255) if self.current_state else (0, 255, 0)
            label   = f"P{self.slot_id} [{self.engine.backend.upper()}]: {'OCCUPIED' if self.current_state else 'FREE'}"
            cv2.putText(display, label, (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3)
            cv2.imshow(f"Slot {self.slot_id} Monitor", display)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

            time.sleep(POLL_INTERVAL)

        self.cap.release()
        cv2.destroyAllWindows()

    def stop(self):
        self.running = False


# ── Demo: simulate camera readings without real hardware ──────────────────────
def run_simulation(api_base: str):
    """
    Simulate camera detections for demo/review purposes.
    Randomly toggles slot status and prints what the camera would report.
    """
    import random
    print("\n[SIMULATION MODE] — No camera required")
    print("Simulating occupancy detection for all 10 slots...\n")

    states = {str(i): False for i in range(1, 11)}

    for cycle in range(10):
        slot = str(random.randint(1, 10))
        states[slot] = not states[slot]
        status = "🔴 OCCUPIED" if states[slot] else "🟢 FREE"
        print(f"  Cycle {cycle+1:02d} | Slot P{slot}: Camera → {status}")
        try:
            requests.post(f'{api_base}/api/camera/update',
                          json={'slot_id': slot, 'occupied': states[slot]},
                          timeout=2)
        except Exception:
            pass
        time.sleep(1.5)

    print("\n[SIMULATION] Done. In production: connect to physical camera.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RFID Parking Camera Monitor")
    parser.add_argument("--slot",    type=int, default=1,                    help="Slot ID to monitor")
    parser.add_argument("--camera",  type=int, default=0,                    help="Camera device index")
    parser.add_argument("--api",     type=str, default="http://localhost:5000", help="API base URL")
    parser.add_argument("--simulate",action="store_true",                    help="Run in simulation mode")
    parser.add_argument("--backend", type=str, default="standard", choices=["standard", "optimized"], help="Inference backend simulation")
    parser.add_argument("--benchmark", action="store_true", help="Benchmark standard vs optimized inference")
    parser.add_argument("--benchmark-frames", type=int, default=80, help="Frames used for latency benchmarking")
    args = parser.parse_args()

    if args.simulate:
        run_simulation(args.api)
    else:
        monitor = SlotMonitor(args.slot, args.camera, args.api, args.backend)
        try:
            if args.benchmark:
                monitor._build_baseline()
                metrics = benchmark_inference(monitor.cap, monitor.baseline, frames=max(20, args.benchmark_frames))
                log_performance(
                    slot_id=args.slot,
                    standard_ms=metrics["standard_avg_ms"],
                    optimized_ms=metrics["optimized_avg_ms"],
                    frames=metrics["frames"],
                    notes=(
                        f"p95 standard={metrics['standard_p95_ms']:.2f}ms, "
                        f"p95 optimized={metrics['optimized_p95_ms']:.2f}ms"
                    ),
                )
                print("\n[Benchmark Results]")
                print(f"  Standard avg:  {metrics['standard_avg_ms']:.3f} ms")
                print(f"  Optimized avg: {metrics['optimized_avg_ms']:.3f} ms")
                speedup = metrics['standard_avg_ms'] / max(1e-9, metrics['optimized_avg_ms'])
                print(f"  Speedup:       {speedup:.2f}x")
                monitor.cap.release()
            else:
                monitor.run()
        except KeyboardInterrupt:
            monitor.stop()
            print("\nMonitor stopped.")
