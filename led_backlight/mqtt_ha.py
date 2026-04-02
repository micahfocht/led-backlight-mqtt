"""Home Assistant MQTT discovery and runtime state management.

For each configured WLED output we publish two HA MQTT discovery payloads:

1. **light** entity
   - Supports on/off and brightness (0–255)
   - ``on`` enables color output to WLED; ``off`` sends all-black
   - Brightness is passed to ``WledUdpSender.send()`` as a scalar

2. **select** entity
   - Options = all configured input IDs
   - Allows the user to switch which video source drives this output

Topic layout (discovery_prefix = "homeassistant")::

    homeassistant/light/led_backlight_<output_id>/config
    homeassistant/select/led_backlight_<output_id>_input/config

State / command topics::

    led_backlight/<output_id>/light/state       {"state":"ON","brightness":200}
    led_backlight/<output_id>/light/set         {"state":"ON","brightness":200}
    led_backlight/<output_id>/input/state       "living_room"
    led_backlight/<output_id>/input/set         "living_room"
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from led_backlight.config import AppConfig, WledOutputConfig

log = logging.getLogger(__name__)

# Callback types
OnLightChange = Callable[[str, bool, int], None]   # (output_id, is_on, brightness)
OnInputChange = Callable[[str, str], None]          # (output_id, input_id)


# ---------------------------------------------------------------------------
# Per-output state
# ---------------------------------------------------------------------------

class OutputState:
    """Mutable runtime state for one WLED output."""

    def __init__(self, output: WledOutputConfig) -> None:
        self.output_id  = output.id
        self.is_on      = True
        self.brightness = output.brightness
        self.input_id   = output.input
        self._lock      = threading.Lock()

    def update_light(self, is_on: bool, brightness: int) -> None:
        with self._lock:
            self.is_on      = is_on
            self.brightness = max(0, min(255, brightness))

    def update_input(self, input_id: str) -> None:
        with self._lock:
            self.input_id = input_id

    def snapshot(self) -> tuple[bool, int, str]:
        """Return (is_on, brightness, input_id) atomically."""
        with self._lock:
            return self.is_on, self.brightness, self.input_id


# ---------------------------------------------------------------------------
# MQTT manager
# ---------------------------------------------------------------------------

class MqttManager:
    """Manages the MQTT connection, discovery publication, and command routing.

    After calling ``start()``, the manager publishes discovery payloads for
    all outputs, subscribes to command topics, and delivers state changes to
    the provided callbacks.
    """

    def __init__(
        self,
        config: AppConfig,
        on_light_change: OnLightChange,
        on_input_change: OnInputChange,
    ) -> None:
        self._config          = config
        self._on_light_change = on_light_change
        self._on_input_change = on_input_change
        self._input_ids       = [i.id for i in config.inputs]

        mc = config.mqtt
        self._discovery_prefix = mc.discovery_prefix

        self._states: dict[str, OutputState] = {
            o.id: OutputState(o) for o in config.wled_outputs
        }

        # Build topic → output_id lookup tables
        self._light_cmd_topics: dict[str, str] = {}
        self._input_cmd_topics: dict[str, str] = {}
        for o in config.wled_outputs:
            self._light_cmd_topics[self._light_set_topic(o.id)]  = o.id
            self._input_cmd_topics[self._input_set_topic(o.id)]  = o.id

        # Make the client_id unique per host so two instances on different
        # machines never trigger broker-side session takeover of each other.
        hostname = socket.gethostname().split(".")[0]  # short hostname only
        unique_client_id = f"{mc.client_id}-{hostname}"
        log.debug("MQTT client_id: %s", unique_client_id)

        # Paho client — use callback API v2 to avoid deprecation warnings
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=unique_client_id,
            protocol=mqtt.MQTTv5,
        )
        if mc.username:
            self._client.username_pw_set(mc.username, mc.password)

        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect

        self._host = mc.host
        self._port = mc.port

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect to the broker (non-blocking loop)."""
        log.info("Connecting to MQTT broker %s:%d", self._host, self._port)
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        """Disconnect cleanly."""
        self._client.loop_stop()
        self._client.disconnect()
        log.info("MQTT disconnected")

    # ------------------------------------------------------------------
    # State publishing (called by pipeline after each frame)
    # ------------------------------------------------------------------

    def publish_light_state(self, output_id: str) -> None:
        state = self._states[output_id]
        is_on, brightness, _ = state.snapshot()
        payload = json.dumps({
            "state":      "ON" if is_on else "OFF",
            "brightness": brightness,
        })
        self._client.publish(
            self._light_state_topic(output_id),
            payload,
            retain=True,
        )

    def publish_input_state(self, output_id: str) -> None:
        state = self._states[output_id]
        _, _, input_id = state.snapshot()
        self._client.publish(
            self._input_state_topic(output_id),
            input_id,
            retain=True,
        )

    def get_state(self, output_id: str) -> OutputState:
        return self._states[output_id]

    # ------------------------------------------------------------------
    # Paho callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        if reason_code != 0:
            log.error("MQTT connect failed, reason_code=%s", reason_code)
            return
        log.info("MQTT connected to %s:%d", self._host, self._port)
        self._publish_discovery()
        self._subscribe_commands()
        # Publish initial state for all outputs
        for output_id in self._states:
            self.publish_light_state(output_id)
            self.publish_input_state(output_id)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if reason_code != 0:
            log.warning("MQTT unexpectedly disconnected (reason_code=%s) — will reconnect", reason_code)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        topic   = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()

        if topic in self._light_cmd_topics:
            output_id = self._light_cmd_topics[topic]
            self._handle_light_command(output_id, payload)
        elif topic in self._input_cmd_topics:
            output_id = self._input_cmd_topics[topic]
            self._handle_input_command(output_id, payload)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_light_command(self, output_id: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # Accept plain "ON"/"OFF" strings too
            data = {"state": payload}

        state      = self._states[output_id]
        is_on      = state.is_on
        brightness = state.brightness

        if "state" in data:
            is_on = str(data["state"]).upper() == "ON"
        if "brightness" in data:
            brightness = int(data["brightness"])

        state.update_light(is_on, brightness)
        self.publish_light_state(output_id)
        log.debug("[%s] light → on=%s brightness=%d", output_id, is_on, brightness)
        self._on_light_change(output_id, is_on, brightness)

    def _handle_input_command(self, output_id: str, payload: str) -> None:
        input_id = payload.strip()
        if input_id not in self._input_ids:
            log.warning(
                "[%s] Unknown input '%s' — ignoring command", output_id, input_id
            )
            return
        self._states[output_id].update_input(input_id)
        self.publish_input_state(output_id)
        log.info("[%s] input → %s", output_id, input_id)
        self._on_input_change(output_id, input_id)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _publish_discovery(self) -> None:
        for output in self._config.wled_outputs:
            self._publish_light_discovery(output)
            self._publish_select_discovery(output)
        log.info("Published MQTT discovery payloads for %d outputs", len(self._config.wled_outputs))

    def _publish_light_discovery(self, output: WledOutputConfig) -> None:
        oid   = output.id
        topic = f"{self._discovery_prefix}/light/led_backlight_{oid}/config"
        payload = {
            "name":              f"LED Backlight {oid}",
            "unique_id":         f"led_backlight_{oid}_light",
            "schema":            "json",
            "state_topic":       self._light_state_topic(oid),
            "command_topic":     self._light_set_topic(oid),
            "brightness":        True,
            "brightness_scale":  255,
            "retain":            True,
            "device": {
                "identifiers":   [f"led_backlight_{oid}"],
                "name":          f"LED Backlight {oid}",
                "model":         "led-backlight-mqtt",
                "manufacturer":  "led-backlight-mqtt",
            },
        }
        self._client.publish(topic, json.dumps(payload), retain=True)

    def _publish_select_discovery(self, output: WledOutputConfig) -> None:
        oid   = output.id
        topic = f"{self._discovery_prefix}/select/led_backlight_{oid}_input/config"
        payload = {
            "name":          f"LED Backlight {oid} Input",
            "unique_id":     f"led_backlight_{oid}_input",
            "state_topic":   self._input_state_topic(oid),
            "command_topic": self._input_set_topic(oid),
            "options":       self._input_ids,
            "retain":        True,
            "device": {
                "identifiers": [f"led_backlight_{oid}"],
                "name":        f"LED Backlight {oid}",
                "model":       "led-backlight-mqtt",
                "manufacturer":"led-backlight-mqtt",
            },
        }
        self._client.publish(topic, json.dumps(payload), retain=True)

    def _subscribe_commands(self) -> None:
        for topic in list(self._light_cmd_topics) + list(self._input_cmd_topics):
            self._client.subscribe(topic)
            log.debug("Subscribed: %s", topic)

    # ------------------------------------------------------------------
    # Topic helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _light_state_topic(oid: str) -> str:
        return f"led_backlight/{oid}/light/state"

    @staticmethod
    def _light_set_topic(oid: str) -> str:
        return f"led_backlight/{oid}/light/set"

    @staticmethod
    def _input_state_topic(oid: str) -> str:
        return f"led_backlight/{oid}/input/state"

    @staticmethod
    def _input_set_topic(oid: str) -> str:
        return f"led_backlight/{oid}/input/set"
