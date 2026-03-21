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

# ─── Configuration ──────────────────────────────────────────────────────────
MOTION_THRESHOLD   = 25     # pixel difference to count as "occupied"
OCCUPIED_RATIO     = 0.08   # fraction of pixels that must differ
STABLE_FRAMES      = 8      # consecutive frames before state change
POLL_INTERVAL      = 0.5    # seconds between frames
BASELINE_FRAMES    = 20     # frames to build baseline on startup


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


class SlotMonitor:
    """Monitors a single parking slot via camera ROI."""

    def __init__(self, slot_id: int, camera_index: int = 0, api_base: str = "http://localhost:5000"):
        self.slot_id      = slot_id
        self.api_base     = api_base
        self.cap          = cv2.VideoCapture(camera_index)
        self.baseline     = None
        self.current_state = False   # False = free, True = occupied
        self.stable_count  = 0
        self.running       = False

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

            detected = detect_occupancy(frame, self.baseline)

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
            label   = f"P{self.slot_id}: {'OCCUPIED' if self.current_state else 'FREE'}"
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
    args = parser.parse_args()

    if args.simulate:
        run_simulation(args.api)
    else:
        monitor = SlotMonitor(args.slot, args.camera, args.api)
        try:
            monitor.run()
        except KeyboardInterrupt:
            monitor.stop()
            print("\nMonitor stopped.")
