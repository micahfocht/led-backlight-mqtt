"""Main pipeline: orchestrates capture threads, frame analysis, and WLED output.

Architecture
------------

::

    CaptureThread(device A) ──┐  queue[A] (maxsize=1)
    CaptureThread(device B) ──┤  queue[B] (maxsize=1)
                              │
                    AnalysisLoop (this module, single thread)
                              │
                  ┌───────────┴──────────────┐
              WledUdpSender(tv1)     WledUdpSender(tv2)
                  │                         │
              WLED device 1          WLED device 2
                              │
                         MqttManager (Paho background thread)

The analysis loop:

1. Round-robins over all active capture device queues.
2. For each fresh frame, determines which WLED outputs are subscribed to
   inputs sourced from that device.
3. For each such output:
   a. Extracts the correct quadrant (if applicable).
   b. Samples border colors via ``frame_analyzer``.
   c. If a volume overlay is active (volume changed within the last 3 s),
      replaces the bottom-edge LED colors with a proportional bar.
   d. Sends colors via the output's ``WledUdpSender`` (respecting brightness
      and on/off state from ``MqttManager``).
4. Respects per-input ``max_fps`` by tracking last-analysis timestamps and
   skipping if the minimum inter-frame interval has not elapsed.

Shutdown is cooperative: ``stop()`` sets an event that the analysis loop
checks each iteration, then joins capture threads.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from led_backlight.capture import CaptureThread, Frame
from led_backlight.config import AppConfig, InputConfig
from led_backlight.frame_analyzer import extract_quadrant, sample_border_colors
from led_backlight.mqtt_ha import MqttManager
from led_backlight.wled import WledUdpSender, fetch_led_count

log = logging.getLogger(__name__)

# How long the volume bar stays visible after the last volume change (seconds)
_VOLUME_BAR_DURATION = 3.0


class Pipeline:
    """Top-level controller — create one instance per process."""

    def __init__(self, config: AppConfig) -> None:
        self._config      = config
        self._stop_event  = threading.Event()

        # --- Capture threads (one per device) --------------------------------
        self._capture_threads: dict[str, CaptureThread] = {
            dev.id: CaptureThread(dev) for dev in config.capture_devices
        }

        # --- WLED senders (one per output) -----------------------------------
        self._senders: dict[str, WledUdpSender] = {}
        self._led_counts: dict[str, int] = {}
        for output in config.wled_outputs:
            led_count = fetch_led_count(output.host, fallback=output.led_count)
            self._led_counts[output.id] = led_count
            self._senders[output.id] = WledUdpSender(output.host, led_count)

        # --- MQTT manager ----------------------------------------------------
        self._mqtt = MqttManager(
            config,
            on_light_change=self._on_light_change,
            on_input_change=self._on_input_change,
            on_volume_change=self._on_volume_change,
        )

        # --- Analysis loop thread --------------------------------------------
        self._analysis_thread = threading.Thread(
            target=self._analysis_loop,
            name="analysis-loop",
            daemon=True,
        )

        # Per-input last-analyzed timestamps for FPS throttling
        self._last_analyzed: dict[str, float] = {
            inp.id: 0.0 for inp in config.inputs
        }

        # Per-output volume overlay expiry timestamps (0 = inactive)
        self._volume_active_until: dict[str, float] = {
            o.id: 0.0 for o in config.wled_outputs
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all capture threads, MQTT, and the analysis loop."""
        log.info("Starting %d capture thread(s)", len(self._capture_threads))
        for thread in self._capture_threads.values():
            thread.start()

        self._mqtt.start()
        self._analysis_thread.start()

    def stop(self) -> None:
        """Signal all threads to stop."""
        log.info("Pipeline stop requested")
        self._stop_event.set()

        for thread in self._capture_threads.values():
            thread.stop()

        # Turn off all WLED outputs
        for output_id, sender in self._senders.items():
            try:
                sender.send_black()
                sender.close()
            except Exception as exc:
                log.warning("[%s] Error during shutdown send: %s", output_id, exc)

        self._mqtt.stop()

    def join(self) -> None:
        """Block until the analysis loop exits (i.e. until stop() is called)."""
        self._analysis_thread.join()

    # ------------------------------------------------------------------
    # MQTT callbacks (called from Paho background thread)
    # ------------------------------------------------------------------

    def _on_light_change(self, output_id: str, is_on: bool, brightness: int) -> None:
        if not is_on:
            sender = self._senders.get(output_id)
            if sender:
                sender.send_black()

    def _on_input_change(self, output_id: str, input_id: str) -> None:
        # Reset last-analyzed so the new input gets a frame ASAP
        self._last_analyzed[input_id] = 0.0

    def _on_volume_change(self, output_id: str, volume: int, color: tuple[int, int, int]) -> None:
        """Called from the Paho thread when volume or volume color changes."""
        self._volume_active_until[output_id] = time.monotonic() + _VOLUME_BAR_DURATION
        log.debug("[%s] volume overlay activated: %d%% color=%s", output_id, volume, color)

    # ------------------------------------------------------------------
    # Analysis loop
    # ------------------------------------------------------------------

    def _analysis_loop(self) -> None:
        log.info("Analysis loop started")
        config = self._config

        # Pre-compute: for each input_id, which outputs subscribe to it?
        # This is rebuilt on input changes via MQTT (dynamic).
        while not self._stop_event.is_set():
            any_work = False

            for inp in config.inputs:
                if self._stop_event.is_set():
                    break

                # FPS throttle check
                max_fps    = config.effective_max_fps(inp.id)
                min_interval = 1.0 / max_fps
                now        = time.monotonic()
                if now - self._last_analyzed[inp.id] < min_interval:
                    continue

                # Get the capture thread for this input's device
                capture = self._capture_threads.get(inp.source_device)
                if capture is None:
                    continue

                frame: "Frame | None" = capture.get_frame(timeout=0.0)
                if frame is None:
                    continue

                any_work = True
                self._last_analyzed[inp.id] = time.monotonic()

                # Extract quadrant if needed
                if inp.quadrant is not None:
                    try:
                        frame = extract_quadrant(frame, inp.quadrant)
                    except Exception as exc:
                        log.error("[%s] Quadrant extraction failed: %s", inp.id, exc)
                        continue

                # Send to all outputs currently subscribed to this input
                self._dispatch_frame(inp, frame)

            if not any_work:
                # No frames available from any device — yield briefly
                time.sleep(0.001)

        log.info("Analysis loop stopped")

    def _dispatch_frame(self, inp: InputConfig, frame: Frame) -> None:
        """Analyze *frame* and push colors to all outputs subscribed to *inp*."""
        config = self._config

        # Find all outputs whose current input matches inp.id
        subscribed_outputs = [
            output
            for output in config.wled_outputs
            if self._mqtt.get_state(output.id).snapshot()[2] == inp.id
        ]

        if not subscribed_outputs:
            return

        # Each output may have a different border_pct and led_count,
        # so we analyze per output.  In the common case (all outputs share
        # the same settings) this adds minor redundancy; correctness wins.
        for output in subscribed_outputs:
            state = self._mqtt.get_state(output.id)
            is_on, brightness, _ = state.snapshot()

            if not is_on:
                continue

            sender    = self._senders.get(output.id)
            led_count = self._led_counts.get(output.id)
            if sender is None or led_count is None:
                continue

            border_pct          = config.effective_border_pct(output.id)
            analysis_resolution = config.effective_analysis_resolution(inp.id)
            led_layout          = config.effective_led_layout(output.id, led_count)

            try:
                colors = sample_border_colors(
                    frame,
                    led_count=led_count,
                    border_pct=border_pct,
                    analysis_resolution=analysis_resolution,
                    led_layout=led_layout,
                )

                # Apply volume bar overlay if active
                if time.monotonic() < self._volume_active_until.get(output.id, 0.0):
                    volume, bar_color = state.snapshot_volume()
                    layout_tuple = _resolve_led_layout(
                        led_layout, led_count, analysis_resolution
                    )
                    _apply_volume_overlay(colors, layout_tuple, volume, bar_color)

                sender.send(colors, brightness=brightness)
            except Exception as exc:
                log.error(
                    "[%s] Frame dispatch error: %s", output.id, exc, exc_info=True
                )


# ---------------------------------------------------------------------------
# Volume overlay helpers (module-level so they can be unit-tested independently)
# ---------------------------------------------------------------------------

def _resolve_led_layout(
    led_layout: "tuple[int, int, int, int] | None",
    led_count: int,
    analysis_resolution: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Return ``(n_top, n_right, n_bottom, n_left)`` for a given output.

    If *led_layout* is already resolved (from config), return it directly.
    Otherwise mirror the proportional distribution logic from
    ``frame_analyzer.py`` using the analysis resolution's aspect ratio.
    """
    if led_layout is not None:
        return led_layout

    aw, ah = analysis_resolution
    w, h = aw, ah
    perimeter = 2 * w + 2 * h
    fractions = [w, h, w, h]  # top, right, bottom, left
    raw_counts = [led_count * f / perimeter for f in fractions]
    counts = [int(c) for c in raw_counts]
    remainders = [(raw_counts[i] - counts[i], i) for i in range(4)]
    deficit = led_count - sum(counts)
    for _, i in sorted(remainders, reverse=True)[:deficit]:
        counts[i] += 1
    n_top, n_right, n_bottom, n_left = counts
    return (n_top, n_right, n_bottom, n_left)


def _apply_volume_overlay(
    colors: "list[tuple[int, int, int]]",
    layout: tuple[int, int, int, int],
    volume: int,
    bar_color: tuple[int, int, int],
) -> None:
    """Mutate *colors* in-place to show a left-to-right volume bar on the bottom edge.

    The bottom edge LEDs run **right→left** in the clockwise LED array.  To
    produce a bar that fills from the left side of the TV, the lit portion
    occupies the **last** ``n_lit`` entries of the bottom slice (those closest
    to the left side of the TV).

    Parameters
    ----------
    colors:
        Full LED color list, modified in-place.
    layout:
        ``(n_top, n_right, n_bottom, n_left)`` LED counts.
    volume:
        0–100 percentage fill.
    bar_color:
        RGB tuple for the lit portion of the bar.
    """
    n_top, n_right, n_bottom, _ = layout
    start = n_top + n_right  # first index of the bottom edge in the array

    n_lit = round(volume / 100 * n_bottom)
    n_dark = n_bottom - n_lit

    # Dark portion: from the right side of the TV (start of the bottom slice)
    for i in range(start, start + n_dark):
        colors[i] = (0, 0, 0)

    # Lit portion: toward the left side of the TV (end of the bottom slice)
    for i in range(start + n_dark, start + n_bottom):
        colors[i] = bar_color
