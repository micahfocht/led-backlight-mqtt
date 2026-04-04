"""Frame analysis: quadrant splitting and border-strip LED color sampling.

Algorithm
---------
1. Optionally split a frame into quadrants (for quadrant-mode capture cards).
2. Resize the (sub-)frame to ``analysis_resolution`` — this is the main CPU
   saving: all subsequent work operates on a small image.
3. Compute a border strip of depth ``border_px = max(1, round(border_pct *
   min(h, w)))``.
4. Distribute *led_count* LEDs clockwise around the border:
       top    row  left  → right   (indices 0 … n_top-1)
       right  col  top   → bottom  (indices n_top … n_top+n_right-1)
       bottom row  right → left    (indices …)
       left   col  bottom → top    (indices …)

   The per-edge counts come from ``led_layout`` when provided, otherwise they
   are computed proportionally to each edge's pixel length (largest-remainder
   method ensures the total equals ``led_count`` exactly).
5. For each LED, compute the mean BGR color of its assigned border region,
   then convert to (R, G, B) tuples.

All array operations use NumPy — no Python loops over pixels.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Public type aliases
RGBColor = tuple[int, int, int]
Quadrant = Literal["top_left", "top_right", "bottom_left", "bottom_right"]


# ---------------------------------------------------------------------------
# Quadrant extraction
# ---------------------------------------------------------------------------

def extract_quadrant(frame: np.ndarray, quadrant: Quadrant) -> np.ndarray:
    """Return the sub-image corresponding to *quadrant* of *frame*.

    The frame is split into four equal rectangles.  Any odd pixel on the
    right/bottom edge is allocated to the right/bottom quadrant.
    """
    h, w = frame.shape[:2]
    mid_y, mid_x = h // 2, w // 2

    slices: dict[str, tuple[slice, slice]] = {
        "top_left":     (slice(0, mid_y),    slice(0, mid_x)),
        "top_right":    (slice(0, mid_y),    slice(mid_x, w)),
        "bottom_left":  (slice(mid_y, h),    slice(0, mid_x)),
        "bottom_right": (slice(mid_y, h),    slice(mid_x, w)),
    }
    sy, sx = slices[quadrant]
    return frame[sy, sx]


# ---------------------------------------------------------------------------
# LED color sampling
# ---------------------------------------------------------------------------

def sample_border_colors(
    frame: np.ndarray,
    led_count: int,
    border_pct: float,
    analysis_resolution: tuple[int, int],
    led_layout: Optional[tuple[int, int, int, int]] = None,
) -> list[RGBColor]:
    """Return a list of ``led_count`` (R, G, B) tuples sampled from the
    border of *frame*.

    LEDs are distributed clockwise starting from the top-left corner:
    top (L→R), right (T→B), bottom (R→L), left (B→T).

    Parameters
    ----------
    frame:
        BGR image (as returned by OpenCV).
    led_count:
        Total number of LEDs in the strand.
    border_pct:
        Border strip depth as a fraction of ``min(h, w)`` of the *analysis*
        frame.
    analysis_resolution:
        ``(width, height)`` to resize the frame to before sampling.
    led_layout:
        Optional ``(n_top, n_right, n_bottom, n_left)`` explicit per-edge
        counts.  When provided the proportional distribution is skipped and
        these values are used directly.  The caller is responsible for
        ensuring ``n_top + n_right + n_bottom + n_left == led_count``.
    """
    if led_count <= 0:
        return []

    # --- 1. Resize -----------------------------------------------------------
    aw, ah = analysis_resolution
    small = cv2.resize(frame, (aw, ah), interpolation=cv2.INTER_AREA)
    h, w = small.shape[:2]

    # --- 2. Border depth -----------------------------------------------------
    border_px = max(1, round(border_pct * min(h, w)))

    # --- 3. Distribute LEDs around the perimeter ----------------------------
    if led_layout is not None:
        n_top, n_right, n_bottom, n_left = led_layout
    else:
        # Proportional to each edge's pixel length (largest-remainder method)
        top_len    = w
        right_len  = h
        bottom_len = w
        left_len   = h
        perimeter  = top_len + right_len + bottom_len + left_len

        fractions   = [top_len, right_len, bottom_len, left_len]
        raw_counts  = [led_count * f / perimeter for f in fractions]
        # Floor all, then add remaining 1s to the largest remainders
        counts      = [int(c) for c in raw_counts]
        remainders  = [(raw_counts[i] - counts[i], i) for i in range(4)]
        deficit     = led_count - sum(counts)
        for _, i in sorted(remainders, reverse=True)[:deficit]:
            counts[i] += 1

        n_top, n_right, n_bottom, n_left = counts

    # --- 4. Sample each LED's region ----------------------------------------
    colors: list[RGBColor] = []

    # Top edge: left → right
    colors.extend(_sample_horizontal(small, 0, border_px, w, n_top, left_to_right=True))
    # Right edge: top → bottom
    colors.extend(_sample_vertical(small, w - border_px, w, h, n_right, top_to_bottom=True))
    # Bottom edge: right → left
    colors.extend(_sample_horizontal(small, h - border_px, h, w, n_bottom, left_to_right=False))
    # Left edge: bottom → top
    colors.extend(_sample_vertical(small, 0, border_px, h, n_left, top_to_bottom=False))

    return colors


# ---------------------------------------------------------------------------
# Internal helpers — vectorised NumPy operations
# ---------------------------------------------------------------------------

def _sample_horizontal(
    img: np.ndarray,
    y0: int,
    y1: int,
    width: int,
    n_leds: int,
    left_to_right: bool,
) -> list[RGBColor]:
    """Sample *n_leds* colors from a horizontal border strip ``img[y0:y1, :]``."""
    if n_leds == 0:
        return []

    strip = img[y0:y1, :, :]  # shape: (border_px, width, 3)
    # Split strip into n_leds segments along the x axis
    segments = np.array_split(strip, n_leds, axis=1)
    colors = [_mean_bgr_to_rgb(seg) for seg in segments]

    if not left_to_right:
        colors = colors[::-1]
    return colors


def _sample_vertical(
    img: np.ndarray,
    x0: int,
    x1: int,
    height: int,
    n_leds: int,
    top_to_bottom: bool,
) -> list[RGBColor]:
    """Sample *n_leds* colors from a vertical border strip ``img[:, x0:x1]``."""
    if n_leds == 0:
        return []

    strip = img[:, x0:x1, :]  # shape: (height, border_px, 3)
    # Split along the y axis
    segments = np.array_split(strip, n_leds, axis=0)
    colors = [_mean_bgr_to_rgb(seg) for seg in segments]

    if not top_to_bottom:
        colors = colors[::-1]
    return colors


def _mean_bgr_to_rgb(segment: np.ndarray) -> RGBColor:
    """Compute the mean BGR color of a segment and return as (R, G, B) ints."""
    if segment.size == 0:
        return (0, 0, 0)
    mean = segment.reshape(-1, 3).mean(axis=0)  # [B, G, R]
    b, g, r = int(round(mean[0])), int(round(mean[1])), int(round(mean[2]))
    return (r, g, b)
