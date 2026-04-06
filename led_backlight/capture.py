"""Video capture thread per UVC device.

Each CaptureThread opens a V4L2/UVC device via OpenCV, requests the
configured FOURCC / resolution / FPS, and continuously reads frames into
a ``queue.Queue(maxsize=1)``.  When the queue is full the oldest frame is
discarded so the consumer always receives the freshest available frame —
this is the key mechanism that keeps analysis latency bounded regardless of
how fast (or slow) the consumer is.

Usage::

    thread = CaptureThread(device_config)
    thread.start()
    frame = thread.get_frame(timeout=0.1)  # returns None on timeout
    thread.stop()
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

import cv2
import numpy as np

from led_backlight.config import CaptureDeviceConfig

log = logging.getLogger(__name__)

# Type alias
Frame = np.ndarray


class CaptureThread(threading.Thread):
    """Background thread that keeps a single-slot queue stocked with the
    latest decoded frame from a UVC capture device."""

    def __init__(self, config: CaptureDeviceConfig) -> None:
        super().__init__(
            name=f"capture-{config.id}",
            daemon=True,
        )
        self._config = config
        # maxsize=1 — put() on a full queue raises queue.Full immediately;
        # we catch it and discard the stale frame.
        self._queue: queue.Queue[Frame] = queue.Queue(maxsize=1)
        self._stop_event = threading.Event()
        self._cap: "cv2.VideoCapture | None" = None
        # Set to True by _open_device when MJPG mode is active; tells
        # _capture_loop to decode the raw JPEG buffer via cv2.imdecode.
        self._mjpeg_mode: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_frame(self, timeout: float = 0.1) -> "Frame | None":
        """Return the latest frame, or None if no frame is available within
        *timeout* seconds."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        """Signal the thread to stop and release the capture device."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Thread body
    # ------------------------------------------------------------------

    def run(self) -> None:
        cfg = self._config
        log.info("[%s] Opening capture device %s", cfg.id, cfg.device)

        cap = self._open_device()
        if cap is None:
            log.error("[%s] Failed to open capture device — thread exiting", cfg.id)
            return

        self._cap = cap
        frame_interval = 1.0 / cfg.fps  # minimum seconds between captures

        try:
            self._capture_loop(cap, frame_interval)
        finally:
            cap.release()
            log.info("[%s] Capture device released", cfg.id)

    def _open_device(self) -> Optional[cv2.VideoCapture]:
        cfg = self._config
        device = cfg.device  # str path or int index

        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not cap.isOpened():
            # Fallback: let OpenCV pick the backend
            cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            return None

        # Request FOURCC
        fourcc = cv2.VideoWriter_fourcc(*cfg.fourcc.upper().ljust(4)[:4])
        cap.set(cv2.CAP_PROP_FOURCC, fourcc)

        # Request resolution and FPS
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        cap.set(cv2.CAP_PROP_FPS, cfg.fps)

        # For MJPEG the V4L2 backend delivers a raw JPEG buffer; disabling the
        # internal RGB conversion prevents OpenCV from misinterpreting it as a
        # raw pixel array and causing frame-read failures.  We decode manually
        # via cv2.imdecode() in the capture loop instead.
        self._mjpeg_mode = cfg.fourcc.upper() == "MJPG"
        if self._mjpeg_mode:
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)

        # Read back negotiated values and warn on mismatch
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        actual_fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC))
        actual_fourcc = (
            chr(actual_fourcc_int & 0xFF)
            + chr((actual_fourcc_int >> 8) & 0xFF)
            + chr((actual_fourcc_int >> 16) & 0xFF)
            + chr((actual_fourcc_int >> 24) & 0xFF)
        ).strip()

        log.info(
            "[%s] Negotiated: %dx%d @ %.1f fps FOURCC=%s",
            cfg.id,
            actual_w,
            actual_h,
            actual_fps,
            actual_fourcc,
        )

        if actual_w != cfg.width or actual_h != cfg.height:
            log.warning(
                "[%s] Resolution mismatch: requested %dx%d, got %dx%d",
                cfg.id,
                cfg.width,
                cfg.height,
                actual_w,
                actual_h,
            )
        if actual_fps > 0 and abs(actual_fps - cfg.fps) > 1.0:
            log.warning(
                "[%s] FPS mismatch: requested %.1f, got %.1f",
                cfg.id,
                cfg.fps,
                actual_fps,
            )
        if actual_fourcc and actual_fourcc.upper() != cfg.fourcc.upper():
            log.warning(
                "[%s] FOURCC mismatch: requested %s, got %s",
                cfg.id,
                cfg.fourcc,
                actual_fourcc,
            )

        return cap

    def _capture_loop(self, cap: cv2.VideoCapture, frame_interval: float) -> None:
        cfg = self._config
        consecutive_failures = 0
        max_failures = 10

        while not self._stop_event.is_set():
            t0 = time.monotonic()

            try:
                ret, raw = cap.read()
            except cv2.error as exc:
                log.debug("[%s] cap.read() raised cv2.error: %s", cfg.id, exc)
                ret, raw = False, None

            if not ret or raw is None:
                consecutive_failures += 1
                log.warning(
                    "[%s] Frame read failed (%d/%d)",
                    cfg.id,
                    consecutive_failures,
                    max_failures,
                )
                if consecutive_failures >= max_failures:
                    log.error(
                        "[%s] Too many consecutive failures — stopping capture thread",
                        cfg.id,
                    )
                    break
                time.sleep(0.05)
                continue

            # In MJPEG mode cap.read() returns a 1-D buffer of JPEG bytes;
            # decode it to a standard BGR frame via imdecode.
            if self._mjpeg_mode:
                frame = cv2.imdecode(raw, cv2.IMREAD_COLOR)
                if frame is None:
                    consecutive_failures += 1
                    log.warning(
                        "[%s] MJPEG decode failed (%d/%d)",
                        cfg.id,
                        consecutive_failures,
                        max_failures,
                    )
                    if consecutive_failures >= max_failures:
                        log.error(
                            "[%s] Too many consecutive failures — stopping capture thread",
                            cfg.id,
                        )
                        break
                    time.sleep(0.05)
                    continue
            else:
                frame = raw

            consecutive_failures = 0

            # Drop stale frame if consumer hasn't picked up the previous one
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                try:
                    self._queue.get_nowait()  # discard old
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(frame)
                except queue.Full:
                    pass  # extremely unlikely; skip this frame

            # Throttle to avoid hammering the device faster than requested
            elapsed = time.monotonic() - t0
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
