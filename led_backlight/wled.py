"""WLED interface: LED count discovery and UDP DRGB real-time output.

WLED UDP Real-Time protocol (DRGB)
-----------------------------------
Packet format::

    byte 0: protocol  = 0x02  (DRGB)
    byte 1: timeout   = seconds before WLED reverts to its normal mode
                        (2 is a safe default — we refresh faster than that)
    bytes 2…end: R G B  R G B  …  (3 bytes per LED, up to 490 LEDs per packet)

The packet is sent over UDP to port 21324 (WLED default).

LED count auto-discovery
-------------------------
At startup we attempt ``GET http://<host>/json/info`` and read
``leds.count``.  If the request fails, the configured ``led_count`` is used
instead.  If neither is available, a ``RuntimeError`` is raised.
"""

from __future__ import annotations

import logging
import socket
import urllib.error
import urllib.request
import json
from typing import Optional

from led_backlight.frame_analyzer import RGBColor

log = logging.getLogger(__name__)

_WLED_UDP_PORT = 21324
_DRGB_PROTOCOL = 0x02
_DRGB_TIMEOUT  = 2        # seconds before WLED reverts; we refresh faster
_MAX_LEDS_PER_PACKET = 490  # WLED firmware limit per UDP packet
_HTTP_TIMEOUT = 3.0        # seconds


# ---------------------------------------------------------------------------
# LED count discovery
# ---------------------------------------------------------------------------

def fetch_led_count(host: str, fallback: Optional[int] = None) -> int:
    """Return the LED count for *host*.

    Tries ``GET http://<host>/json/info`` first.  Falls back to *fallback*
    if the request fails.  Raises ``RuntimeError`` if neither source works.
    """
    url = f"http://{host}/json/info"
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
        count = int(data["leds"]["count"])
        log.info("[wled:%s] Auto-detected %d LEDs from /json/info", host, count)
        return count
    except Exception as exc:
        log.warning(
            "[wled:%s] Could not fetch LED count from %s: %s",
            host,
            url,
            exc,
        )

    if fallback is not None:
        log.info("[wled:%s] Using configured led_count=%d", host, fallback)
        return fallback

    raise RuntimeError(
        f"WLED device '{host}': LED count could not be auto-detected and no "
        "fallback led_count is configured. Set led_count in the YAML config."
    )


# ---------------------------------------------------------------------------
# UDP sender
# ---------------------------------------------------------------------------

class WledUdpSender:
    """Sends RGB color data to a single WLED device over UDP DRGB protocol.

    The socket is opened once at construction and reused for all subsequent
    sends to minimise per-frame overhead.
    """

    def __init__(self, host: str, led_count: int) -> None:
        self.host = host
        self.led_count = led_count
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Resolve hostname once
        self._addr = (socket.gethostbyname(host), _WLED_UDP_PORT)
        log.info(
            "[wled:%s] UDP sender ready — %d LEDs → %s:%d",
            host,
            led_count,
            self._addr[0],
            _WLED_UDP_PORT,
        )

    def send(self, colors: list[RGBColor], brightness: int = 255) -> None:
        """Send *colors* to the WLED device.

        Parameters
        ----------
        colors:
            List of ``(R, G, B)`` tuples.  Must have ``len == self.led_count``.
            If shorter, remaining LEDs are set to black.  Extra entries are
            truncated.
        brightness:
            0–255 scalar applied to all channels before sending.
        """
        n = self.led_count
        scale = brightness / 255.0

        # Build flat RGB byte array, padded/truncated to led_count
        flat = bytearray(n * 3)
        for i, (r, g, b) in enumerate(colors[:n]):
            base = i * 3
            flat[base]     = min(255, int(r * scale))
            flat[base + 1] = min(255, int(g * scale))
            flat[base + 2] = min(255, int(b * scale))
        # Remaining LEDs stay 0 (black) — bytearray is zero-initialised

        # WLED limits each UDP packet to 490 LEDs; split if needed
        for chunk_start in range(0, n, _MAX_LEDS_PER_PACKET):
            chunk_end = min(chunk_start + _MAX_LEDS_PER_PACKET, n)
            led_offset = chunk_start

            # For multi-packet we use the WARLS protocol which supports
            # per-LED addressing.  For single-packet we use DRGB (simpler).
            if n <= _MAX_LEDS_PER_PACKET:
                header = bytes([_DRGB_PROTOCOL, _DRGB_TIMEOUT])
                payload = header + bytes(flat)
            else:
                # WARLS (0x01): header + [index, R, G, B] per LED
                payload = self._build_warls_packet(flat, led_offset, chunk_end)

            try:
                self._sock.sendto(payload, self._addr)
            except OSError as exc:
                log.error("[wled:%s] UDP send error: %s", self.host, exc)

    def send_black(self) -> None:
        """Turn off all LEDs (send all-zero colors)."""
        self.send([(0, 0, 0)] * self.led_count, brightness=255)

    def close(self) -> None:
        """Release the UDP socket."""
        try:
            self._sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_warls_packet(
        flat: bytearray, start: int, end: int
    ) -> bytes:
        """Build a WARLS packet for LEDs [start, end)."""
        # WARLS protocol: byte0=0x01, byte1=timeout, then [idx, R, G, B] per LED
        header = bytearray([0x01, _DRGB_TIMEOUT])
        body   = bytearray()
        for i in range(start, end):
            base = i * 3
            body.append(i & 0xFF)        # LED index (low byte)
            body.append(flat[base])      # R
            body.append(flat[base + 1])  # G
            body.append(flat[base + 2])  # B
        return bytes(header + body)
