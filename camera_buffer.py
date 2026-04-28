"""Shared in-process buffer for gate_monitor → Flask MJPEG stream."""
import threading

_lock  = threading.Lock()
_frame = None   # bytes: latest JPEG-encoded camera frame (with QR overlay drawn)

def set_frame(jpeg_bytes: bytes):
    global _frame
    with _lock:
        _frame = jpeg_bytes

def get_frame() -> bytes | None:
    with _lock:
        return _frame
