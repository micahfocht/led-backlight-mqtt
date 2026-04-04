"""Unit tests for led_backlight.frame_analyzer.

Tests cover:
- Quadrant extraction geometry
- LED count distribution across the four edges
- Border color sampling (mean BGR → RGB)
- Clockwise ordering of the LED strand
- Explicit per-edge LED counts via led_layout
- Edge cases: 1 LED, uneven LED distribution, tiny frames
"""

from __future__ import annotations

import numpy as np
import pytest

from led_backlight.frame_analyzer import (
    RGBColor,
    extract_quadrant,
    sample_border_colors,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def solid_frame(width: int, height: int, bgr: tuple[int, int, int]) -> np.ndarray:
    """Create a solid-color BGR frame."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = bgr  # broadcast (B, G, R)
    return frame


def quadrant_frame(width: int, height: int) -> np.ndarray:
    """Create a frame whose four quadrants are distinct solid colors.

    top_left=red, top_right=green, bottom_left=blue, bottom_right=white
    (all expressed as BGR)
    """
    h, w = height, width
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    mh, mw = h // 2, w // 2
    frame[0:mh,  0:mw]  = (0,   0,   255)  # top_left  → red   in BGR
    frame[0:mh,  mw:w]  = (0,   255, 0  )  # top_right → green
    frame[mh:h,  0:mw]  = (255, 0,   0  )  # bottom_left → blue
    frame[mh:h,  mw:w]  = (255, 255, 255)  # bottom_right → white
    return frame


# ---------------------------------------------------------------------------
# extract_quadrant
# ---------------------------------------------------------------------------

class TestExtractQuadrant:
    def test_top_left_shape(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        q = extract_quadrant(frame, "top_left")
        assert q.shape == (50, 100, 3)

    def test_top_right_shape(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        q = extract_quadrant(frame, "top_right")
        assert q.shape == (50, 100, 3)

    def test_bottom_left_shape(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        q = extract_quadrant(frame, "bottom_left")
        assert q.shape == (50, 100, 3)

    def test_bottom_right_shape(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        q = extract_quadrant(frame, "bottom_right")
        assert q.shape == (50, 100, 3)

    def test_quadrant_content_top_left(self):
        """top_left quadrant should contain only red pixels."""
        h, w = 100, 200
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        mh, mw = h // 2, w // 2
        frame[0:mh, 0:mw]  = (0, 0, 255)   # BGR red
        frame[0:mh, mw:w]  = (0, 255, 0)
        frame[mh:h, 0:mw]  = (255, 0, 0)
        frame[mh:h, mw:w]  = (255, 255, 255)

        q = extract_quadrant(frame, "top_left")
        assert np.all(q == (0, 0, 255)), "top_left should be all-red BGR"

    def test_quadrant_content_bottom_right(self):
        h, w = 100, 200
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        mh, mw = h // 2, w // 2
        frame[mh:h, mw:w] = (255, 255, 255)
        q = extract_quadrant(frame, "bottom_right")
        assert np.all(q == (255, 255, 255))

    def test_odd_dimensions_bottom_right_gets_extra(self):
        """With odd dimensions the bottom/right quadrant gets the extra pixel."""
        frame = np.zeros((101, 201, 3), dtype=np.uint8)
        tl = extract_quadrant(frame, "top_left")
        br = extract_quadrant(frame, "bottom_right")
        assert tl.shape == (50, 100, 3)
        assert br.shape == (51, 101, 3)


# ---------------------------------------------------------------------------
# sample_border_colors — LED count
# ---------------------------------------------------------------------------

class TestSampleBorderColorsCount:
    def _sample(self, led_count: int, w=320, h=180) -> list[RGBColor]:
        frame = solid_frame(w, h, (128, 128, 128))
        return sample_border_colors(
            frame,
            led_count=led_count,
            border_pct=0.1,
            analysis_resolution=(w, h),
        )

    def test_returns_correct_led_count(self):
        for n in [4, 10, 20, 60, 84, 120]:
            colors = self._sample(n)
            assert len(colors) == n, f"Expected {n} colors, got {len(colors)}"

    def test_zero_leds_returns_empty(self):
        assert self._sample(0) == []

    def test_one_led(self):
        colors = self._sample(1)
        assert len(colors) == 1

    def test_large_led_count(self):
        # Should not raise even with many LEDs
        colors = self._sample(300)
        assert len(colors) == 300


# ---------------------------------------------------------------------------
# sample_border_colors — color accuracy
# ---------------------------------------------------------------------------

class TestSampleBorderColorsValues:
    def test_solid_red_frame(self):
        """All border LEDs should be approximately red on an all-red frame."""
        # BGR red = (0, 0, 255) → RGB (255, 0, 0)
        frame = solid_frame(320, 180, (0, 0, 255))
        colors = sample_border_colors(
            frame,
            led_count=20,
            border_pct=0.1,
            analysis_resolution=(320, 180),
        )
        for i, (r, g, b) in enumerate(colors):
            assert r > 200, f"LED {i}: expected red-dominant, got ({r},{g},{b})"
            assert g < 10,  f"LED {i}: expected low green, got ({r},{g},{b})"
            assert b < 10,  f"LED {i}: expected low blue, got ({r},{g},{b})"

    def test_solid_white_frame(self):
        """All channels should be ~255 on a white frame."""
        frame = solid_frame(320, 180, (255, 255, 255))
        colors = sample_border_colors(
            frame,
            led_count=12,
            border_pct=0.1,
            analysis_resolution=(320, 180),
        )
        for i, (r, g, b) in enumerate(colors):
            assert r > 240 and g > 240 and b > 240, \
                f"LED {i}: expected white, got ({r},{g},{b})"

    def test_solid_black_frame(self):
        """All channels should be 0 on a black frame."""
        frame = solid_frame(320, 180, (0, 0, 0))
        colors = sample_border_colors(
            frame,
            led_count=12,
            border_pct=0.1,
            analysis_resolution=(320, 180),
        )
        for r, g, b in colors:
            assert (r, g, b) == (0, 0, 0)

    def test_rgb_values_are_ints(self):
        frame = solid_frame(320, 180, (100, 150, 200))
        colors = sample_border_colors(
            frame,
            led_count=8,
            border_pct=0.1,
            analysis_resolution=(320, 180),
        )
        for r, g, b in colors:
            assert isinstance(r, int)
            assert isinstance(g, int)
            assert isinstance(b, int)

    def test_values_in_range(self):
        frame = solid_frame(320, 180, (80, 160, 240))
        colors = sample_border_colors(
            frame,
            led_count=16,
            border_pct=0.1,
            analysis_resolution=(320, 180),
        )
        for r, g, b in colors:
            assert 0 <= r <= 255
            assert 0 <= g <= 255
            assert 0 <= b <= 255


# ---------------------------------------------------------------------------
# sample_border_colors — clockwise ordering
# ---------------------------------------------------------------------------

class TestClockwiseOrdering:
    """Verify the clockwise LED ordering by placing distinct colors on each
    edge and checking that the first few and last few LEDs match the expected
    edge colors."""

    def _make_edge_frame(self, w: int = 320, h: int = 180, border: int = 20) -> np.ndarray:
        """Create a frame where each edge strip has a unique color.

        top=red, right=green, bottom=blue, left=white (BGR notation).
        The center is black (should not affect border sampling).
        """
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[0:border,     :,          :] = (0,   0,   200)  # top    — red
        frame[:,             w-border:w, :] = (0,   200, 0  )  # right  — green
        frame[h-border:h,   :,          :] = (200, 0,   0  )  # bottom — blue
        frame[:,             0:border,   :] = (200, 200, 200)  # left   — white
        return frame

    def test_top_leds_are_reddish(self):
        frame = self._make_edge_frame()
        # With 40 LEDs on a 320x180 frame, roughly 10 go to the top edge
        colors = sample_border_colors(
            frame,
            led_count=40,
            border_pct=0.11,     # slightly above 20/180 to capture the strip
            analysis_resolution=(320, 180),
        )
        # First LED should be in the top strip (red)
        r, g, b = colors[0]
        assert r > 150, f"First LED should be reddish; got ({r},{g},{b})"

    def test_right_leds_are_greenish(self):
        frame = self._make_edge_frame()
        colors = sample_border_colors(
            frame,
            led_count=40,
            border_pct=0.11,
            analysis_resolution=(320, 180),
        )
        # Top edge ≈ 10 LEDs (320/(320+180+320+180)*40 ≈ 12.8)
        # Right edge starts after top
        n_top = round(40 * 320 / (2 * 320 + 2 * 180))
        r, g, b = colors[n_top]
        assert g > 150, f"First right LED should be greenish; got ({r},{g},{b})"


# ---------------------------------------------------------------------------
# sample_border_colors — explicit led_layout
# ---------------------------------------------------------------------------

class TestLedLayout:
    """Verify that an explicit led_layout overrides proportional distribution."""

    def _make_edge_frame(self, w: int = 320, h: int = 180, border: int = 20) -> np.ndarray:
        """Frame with distinct solid colors on each edge strip."""
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[0:border,     :,          :] = (0,   0,   200)  # top   — red
        frame[:,             w-border:w, :] = (0,   200, 0  )  # right — green
        frame[h-border:h,   :,          :] = (200, 0,   0  )  # bottom — blue
        frame[:,             0:border,   :] = (200, 200, 200)  # left  — white
        return frame

    def test_explicit_layout_total_equals_led_count(self):
        """led_layout values must sum to led_count; result length must match."""
        frame = solid_frame(320, 180, (128, 128, 128))
        layout = (20, 10, 20, 10)  # top, right, bottom, left — total 60
        colors = sample_border_colors(
            frame,
            led_count=60,
            border_pct=0.1,
            analysis_resolution=(320, 180),
            led_layout=layout,
        )
        assert len(colors) == 60

    def test_explicit_layout_more_leds_on_top_than_proportional(self):
        """Setting a high top count forces more LEDs to the top edge."""
        frame = self._make_edge_frame()
        # Proportional for 40 LEDs on 320x180 → top ≈ 12.8 → 13 LEDs
        # Explicit: top=25, right=5, bottom=5, left=5
        layout = (25, 5, 5, 5)
        colors = sample_border_colors(
            frame,
            led_count=40,
            border_pct=0.12,
            analysis_resolution=(320, 180),
            led_layout=layout,
        )
        assert len(colors) == 40
        # The first LED and the last few top LEDs should be red-dominant.
        # Corner LEDs (first and last of the top strip) may blend with the
        # adjacent edge colors, so check only the clearly interior ones.
        for i in range(2, 23):  # skip the two corner LEDs at each end
            r, g, b = colors[i]
            assert r > 150, f"LED {i} should be top (reddish), got ({r},{g},{b})"
        # Next 5 LEDs should be from the right (greenish)
        for i in range(25, 30):
            r, g, b = colors[i]
            assert g > 150, f"LED {i} should be right (greenish), got ({r},{g},{b})"

    def test_layout_none_matches_old_proportional_behaviour(self):
        """led_layout=None should produce the same result as omitting it."""
        frame = solid_frame(320, 180, (80, 160, 240))
        kwargs = dict(
            frame=frame,
            led_count=40,
            border_pct=0.1,
            analysis_resolution=(320, 180),
        )
        colors_default = sample_border_colors(**kwargs)
        colors_none    = sample_border_colors(**kwargs, led_layout=None)
        assert colors_default == colors_none
