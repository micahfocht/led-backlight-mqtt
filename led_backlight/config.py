"""Configuration models and YAML loader.

Uses Pydantic v2 for validation.  Environment variable substitution is
supported in string values using the ${ENV_VAR} syntax.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Environment variable interpolation
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate(value: str) -> str:
    """Replace ${VAR} tokens with environment variable values."""

    def _replace(m: re.Match) -> str:
        var = m.group(1)
        result = os.environ.get(var)
        if result is None:
            raise ValueError(f"Environment variable '{var}' is not set")
        return result

    return _ENV_RE.sub(_replace, value)


def _interpolate_obj(obj: object) -> object:
    """Recursively interpolate environment variables in parsed YAML."""
    if isinstance(obj, str):
        return _interpolate(obj)
    if isinstance(obj, dict):
        return {k: _interpolate_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_obj(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class GlobalConfig(BaseModel):
    max_fps: float = Field(default=30.0, gt=0, description="Global max FPS ceiling")
    analysis_resolution: tuple[int, int] = Field(
        default=(320, 180),
        description="(width, height) to downscale frames before edge sampling",
    )
    border_pct: float = Field(
        default=0.08,
        gt=0,
        le=0.5,
        description="Border strip depth as a fraction of the shorter dimension",
    )


class MqttConfig(BaseModel):
    host: str
    port: int = Field(default=1883, ge=1, le=65535)
    username: Optional[str] = None
    password: Optional[str] = None
    discovery_prefix: str = "homeassistant"
    client_id: str = "led-backlight"


class CaptureDeviceConfig(BaseModel):
    id: str
    device: str | int = Field(
        description="V4L2 device path (e.g. /dev/video0) or integer index"
    )
    fourcc: str = Field(default="MJPG", description="FOURCC format string, e.g. MJPG or YUYV")
    width: int = Field(default=1920, gt=0)
    height: int = Field(default=1080, gt=0)
    fps: float = Field(default=30.0, gt=0)
    mode: Literal["single", "quadrant"] = "single"

    @model_validator(mode="after")
    def _coerce_device(self) -> "CaptureDeviceConfig":
        # Allow integer-string device indices ("0", "1") from YAML
        if isinstance(self.device, str) and self.device.isdigit():
            self.device = int(self.device)
        # Convert /dev/videoN paths to integer indices.
        # OpenCV's V4L2 backend does not support opening by device path;
        # it requires an integer index derived from the device node number.
        elif isinstance(self.device, str):
            import re as _re
            m = _re.fullmatch(r"/dev/video(\d+)", self.device)
            if m:
                self.device = int(m.group(1))
        return self


class InputConfig(BaseModel):
    id: str
    source_device: str = Field(description="ID of a capture_device entry")
    quadrant: Optional[Literal["top_left", "top_right", "bottom_left", "bottom_right"]] = None
    # Per-input overrides — None means inherit from global
    max_fps: Optional[float] = Field(default=None, gt=0)
    analysis_resolution: Optional[tuple[int, int]] = None

    @model_validator(mode="after")
    def _validate_quadrant(self) -> "InputConfig":
        # Quadrant validation against the device mode is deferred to AppConfig
        return self


class LedLayout(BaseModel):
    """Explicit per-edge LED counts (all four must be provided together).

    LEDs are assigned clockwise starting from the top-left corner:
    top (L→R) → right (T→B) → bottom (R→L) → left (B→T).

    The sum of all four values must equal the resolved ``led_count`` for the
    output (whether auto-detected from WLED or set explicitly in config).
    This validation is deferred to pipeline start-up time when the
    auto-detected count is known.
    """

    top: int = Field(gt=0, description="Number of LEDs on the top edge")
    right: int = Field(gt=0, description="Number of LEDs on the right edge")
    bottom: int = Field(gt=0, description="Number of LEDs on the bottom edge")
    left: int = Field(gt=0, description="Number of LEDs on the left edge")

    @property
    def total(self) -> int:
        return self.top + self.right + self.bottom + self.left


class WledOutputConfig(BaseModel):
    id: str
    host: str
    led_count: Optional[int] = Field(default=None, gt=0)
    brightness: int = Field(default=200, ge=0, le=255)
    input: str = Field(description="ID of an input entry — the default at startup")
    border_pct: Optional[float] = Field(default=None, gt=0, le=0.5)
    led_layout: Optional[LedLayout] = Field(
        default=None,
        description=(
            "Explicit per-edge LED counts. "
            "When set, overrides the proportional distribution. "
            "top + right + bottom + left must equal the resolved led_count."
        ),
    )


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    globals: GlobalConfig = Field(default_factory=GlobalConfig, alias="global")
    mqtt: MqttConfig
    capture_devices: list[CaptureDeviceConfig]
    inputs: list[InputConfig]
    wled_outputs: list[WledOutputConfig]

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _cross_validate(self) -> "AppConfig":
        device_ids = {d.id for d in self.capture_devices}
        device_modes = {d.id: d.mode for d in self.capture_devices}
        input_ids = {i.id for i in self.inputs}

        for inp in self.inputs:
            if inp.source_device not in device_ids:
                raise ValueError(
                    f"Input '{inp.id}' references unknown capture_device '{inp.source_device}'"
                )
            mode = device_modes[inp.source_device]
            if mode == "quadrant" and inp.quadrant is None:
                raise ValueError(
                    f"Input '{inp.id}' uses quadrant device '{inp.source_device}' "
                    "but does not specify a quadrant"
                )
            if mode == "single" and inp.quadrant is not None:
                raise ValueError(
                    f"Input '{inp.id}' specifies a quadrant but device "
                    f"'{inp.source_device}' is in single mode"
                )

        for out in self.wled_outputs:
            if out.input not in input_ids:
                raise ValueError(
                    f"WLED output '{out.id}' references unknown input '{out.input}'"
                )

        return self

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def get_device(self, device_id: str) -> CaptureDeviceConfig:
        for d in self.capture_devices:
            if d.id == device_id:
                return d
        raise KeyError(device_id)

    def get_input(self, input_id: str) -> InputConfig:
        for i in self.inputs:
            if i.id == input_id:
                return i
        raise KeyError(input_id)

    def get_wled_output(self, output_id: str) -> WledOutputConfig:
        for o in self.wled_outputs:
            if o.id == output_id:
                return o
        raise KeyError(output_id)

    def effective_max_fps(self, input_id: str) -> float:
        inp = self.get_input(input_id)
        return inp.max_fps if inp.max_fps is not None else self.globals.max_fps

    def effective_analysis_resolution(self, input_id: str) -> tuple[int, int]:
        inp = self.get_input(input_id)
        return (
            inp.analysis_resolution
            if inp.analysis_resolution is not None
            else self.globals.analysis_resolution
        )

    def effective_border_pct(self, output_id: str) -> float:
        out = self.get_wled_output(output_id)
        return out.border_pct if out.border_pct is not None else self.globals.border_pct

    def effective_led_layout(
        self, output_id: str, led_count: int
    ) -> "tuple[int, int, int, int] | None":
        """Return ``(n_top, n_right, n_bottom, n_left)`` if ``led_layout`` is
        configured for *output_id*, else ``None`` (use proportional distribution).

        Raises ``ValueError`` if the layout total does not match *led_count*.
        """
        out = self.get_wled_output(output_id)
        if out.led_layout is None:
            return None
        layout = out.led_layout
        total = layout.total
        if total != led_count:
            raise ValueError(
                f"Output '{output_id}': led_layout total ({total}) does not match "
                f"resolved led_count ({led_count}). "
                f"Adjust top/right/bottom/left so they sum to {led_count}."
            )
        return (layout.top, layout.right, layout.bottom, layout.left)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(path: str | Path) -> AppConfig:
    """Load, interpolate, and validate the YAML configuration file."""
    raw = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    data = _interpolate_obj(data)
    return AppConfig.model_validate(data)
