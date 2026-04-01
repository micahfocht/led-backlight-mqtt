"""Entry point: python -m led_backlight --config /path/to/config.yaml"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from led_backlight.config import load_config
from led_backlight.pipeline import Pipeline


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="led-backlight",
        description="TV LED ambilight controller — WLED + Home Assistant MQTT",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    _setup_logging(args.verbose)
    log = logging.getLogger("led_backlight")

    log.info("Loading configuration from %s", args.config)
    try:
        config = load_config(args.config)
    except Exception as exc:
        log.error("Failed to load configuration: %s", exc)
        sys.exit(1)

    pipeline = Pipeline(config)

    # Graceful shutdown on SIGINT / SIGTERM
    def _shutdown(signum, frame):  # noqa: ANN001
        log.info("Received signal %s — shutting down", signal.Signals(signum).name)
        pipeline.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Starting pipeline")
    try:
        pipeline.start()
        pipeline.join()
    except Exception as exc:
        log.exception("Pipeline error: %s", exc)
        pipeline.stop()
        sys.exit(1)

    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
