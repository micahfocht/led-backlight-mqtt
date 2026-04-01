# led-backlight-mqtt

TV ambilight controller for WLED-powered LED strips.  Captures video from
USB UVC capture cards, samples edge colors in real time, and drives WLED
devices over UDP.  Exposes each output to Home Assistant via MQTT discovery.

## Features

- MJPEG UVC capture cards (low USB bandwidth, hardware-decoded)
- Single-stream and quadrant-mode inputs (one frame containing 4 sub-images)
- Multiple capture cards and WLED outputs, all configurable in YAML
- UDP DRGB real-time protocol to WLED — minimal latency
- Auto-detects LED count from WLED `/json/info`; YAML fallback
- Home Assistant MQTT discovery: `light` (on/off + brightness) and `select`
  (input source) entity per WLED output
- Prioritises real-time response — always processes the newest frame,
  drops stale ones rather than buffering
- Targets Raspberry Pi 5 (Raspberry Pi OS 64-bit Bookworm)

---

## Requirements

| Dependency | Version |
|---|---|
| Python | ≥ 3.11 |
| OpenCV | ≥ 4.9 (see Pi 5 note) |
| NumPy | ≥ 1.26 |
| Pydantic | ≥ 2.0 |
| PyYAML | ≥ 6.0 |
| paho-mqtt | ≥ 2.0 |

---

## Raspberry Pi 5 Setup

### 1. Build OpenCV from source (recommended)

The pip `opencv-python-headless` binary does not include NEON SIMD
optimisations.  Build from source for best performance:

```bash
bash scripts/build_opencv.sh
```

Build time is approximately 45 minutes on a Pi 5.  The script:

- Installs all build dependencies via `apt`
- Clones OpenCV 4.9.0 + opencv_contrib
- Configures CMake with `ENABLE_NEON=ON`, `ENABLE_VFPV3=ON`, libjpeg-turbo,
  V4L2 backend
- Disables unused modules (dnn, ml, stitching, viz) to reduce build time
- Installs system-wide to `/usr/local`

### 2. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate

# On Pi 5: do NOT install opencv-python-headless — use the system build above
pip install pydantic pyyaml paho-mqtt numpy
```

On a non-Pi development machine:

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

See [Configuration Reference](#configuration-reference) below.

### 4. Run

```bash
python -m led_backlight --config config.yaml
# or with the installed script:
led-backlight --config config.yaml
```

Add `-v` / `--verbose` for debug logging.

---

## Systemd Service

### Create a dedicated user

```bash
sudo useradd -r -s /usr/sbin/nologin led-backlight
sudo usermod -aG video led-backlight
```

### Install config

```bash
sudo mkdir -p /etc/led-backlight
sudo cp config.yaml /etc/led-backlight/config.yaml
sudo chown root:led-backlight /etc/led-backlight/config.yaml
sudo chmod 640 /etc/led-backlight/config.yaml
```

If you use environment variables for secrets, create
`/etc/led-backlight/env`:

```
MQTT_PASSWORD=your_password_here
```

Then uncomment `EnvironmentFile=` in the service file.

### Install and enable the service

```bash
sudo cp systemd/led-backlight.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now led-backlight
```

### Check status

```bash
sudo systemctl status led-backlight
sudo journalctl -u led-backlight -f
```

---

## Home Assistant Integration

The service uses MQTT discovery.  Once running, entities appear automatically
in Home Assistant under **Settings → Devices & Services → MQTT**.

For each configured WLED output (e.g. `tv1`) two entities are created:

| Entity type | Entity ID example | Purpose |
|---|---|---|
| `light` | `light.led_backlight_tv1` | On/off + brightness slider |
| `select` | `select.led_backlight_tv1_input` | Choose video input source |

No manual HA configuration is needed beyond an MQTT integration pointed at
your broker.

---

## Configuration Reference

```yaml
# Global defaults — overridable per-input or per-output
global:
  max_fps: 30                    # Analysis FPS ceiling (per input)
  analysis_resolution: [320, 180]  # Downscale before edge sampling
  border_pct: 0.08               # Border strip depth (fraction of shorter dim)

mqtt:
  host: 192.168.1.10
  port: 1883
  username: mqtt_user
  password: ${MQTT_PASSWORD}     # or plain string
  discovery_prefix: homeassistant
  client_id: led-backlight

capture_devices:
  - id: living_room_card
    device: /dev/video0          # V4L2 path or integer index (0, 1…)
    fourcc: MJPG                 # MJPG (recommended) | YUYV
    width: 1920
    height: 1080
    fps: 30
    mode: single                 # single | quadrant

inputs:
  - id: living_room
    source_device: living_room_card
    quadrant: ~                  # null for single-mode; top_left/top_right/
                                 # bottom_left/bottom_right for quadrant-mode
    max_fps: ~                   # override global.max_fps (optional)
    analysis_resolution: ~       # override global.analysis_resolution (optional)

wled_outputs:
  - id: tv1
    host: 192.168.1.50
    led_count: ~                 # null = auto-fetch from WLED /json/info
    brightness: 200              # 0–255 default
    input: living_room           # default input at startup
    border_pct: ~                # override global.border_pct (optional)
```

### LED mapping

LEDs are distributed **clockwise from the top-left corner**:

```
←─────────── top (L→R) ───────────→
↑                                  ↓
left                             right
(B→T)                            (T→B)
↑                                  ↓
←────────── bottom (R→L) ──────────→
```

LED count per edge is proportional to that edge's pixel length at the
analysis resolution.

### Quadrant mode

When `mode: quadrant`, the capture card delivers a single frame containing
four sub-images arranged in a 2×2 grid.  Each `input` entry references one
quadrant:

```yaml
capture_devices:
  - id: quad_card
    device: /dev/video2
    mode: quadrant
    ...

inputs:
  - id: bedroom_tv
    source_device: quad_card
    quadrant: top_left
  - id: office_tv
    source_device: quad_card
    quadrant: top_right
```

---

## USB Bandwidth Notes

MJPEG capture cards are strongly recommended.  Approximate USB 3.0 bandwidth
usage per card:

| Resolution | FPS | MJPG (~10:1) | Raw YUYV |
|---|---|---|---|
| 1080p | 30 | ~150 Mbps | ~1.5 Gbps |
| 720p  | 30 | ~66 Mbps  | ~665 Mbps |

The Pi 5 has two USB 3.0 ports (shared ~5 Gbps).  **2× 1080p30 MJPEG cards
is the tested and supported configuration.**  3–4 cards may be feasible from
a bandwidth perspective but may saturate the CPU.  For more than 2 cards
simultaneously, a more capable x86 Linux machine is recommended.

---

## Development

```bash
# Install dev dependencies
pip install -r requirements.txt pytest

# Run tests (requires numpy and opencv — not on Pi: use the system OpenCV)
pytest tests/ -v
```

### Project structure

```
led_backlight/
├── __init__.py       Package marker + version
├── __main__.py       CLI entry point
├── config.py         Pydantic v2 config models + YAML loader
├── capture.py        Per-device OpenCV capture thread
├── frame_analyzer.py Quadrant splitting + border-strip color sampling
├── wled.py           WLED LED-count fetch + UDP DRGB sender
├── mqtt_ha.py        MQTT HA discovery + command/state handling
└── pipeline.py       Main orchestration loop
scripts/
└── build_opencv.sh   Pi 5 OpenCV NEON build script
systemd/
└── led-backlight.service
tests/
└── test_frame_analyzer.py
```

---

## License

MIT
