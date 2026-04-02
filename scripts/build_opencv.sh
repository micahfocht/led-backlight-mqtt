#!/usr/bin/env bash
# =============================================================================
# build_opencv.sh — Build OpenCV from source on Raspberry Pi 5
#                   with NEON SIMD and libjpeg-turbo acceleration.
#
# Tested on: Raspberry Pi 5, Raspberry Pi OS 64-bit (Trixie)
# Build time: ~15 minutes on Pi 5 (all 4 cores)
#
# Usage:
#   bash scripts/build_opencv.sh
#
# After the build, OpenCV is installed system-wide and will be picked up
# by any Python 3 environment.  You do NOT need to pip install
# opencv-python-headless on the Pi.
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
OPENCV_VERSION="4.9.0"
BUILD_DIR="${HOME}/opencv_build"
PYTHON_BIN="$(which python3)"
PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
NPROC="$(nproc)"

echo "============================================================"
echo " OpenCV ${OPENCV_VERSION} build for Raspberry Pi 5"
echo " Python: ${PYTHON_BIN} (${PYTHON_VERSION})"
echo " Cores:  ${NPROC}"
echo " Build:  ${BUILD_DIR}"
echo "============================================================"

# ── 1. System dependencies ────────────────────────────────────────────────────
echo "[1/6] Installing system dependencies..."
sudo apt-get update -y
# Headless build — no GUI deps needed
sudo apt-get install -y \
    build-essential cmake git pkg-config \
    libjpeg62-turbo-dev libjpeg-dev \
    libpng-dev libtiff-dev \
    libavcodec-dev libavformat-dev libswscale-dev libavutil-dev \
    libv4l-dev v4l-utils \
    libxvidcore-dev libx264-dev \
    libopenblas-dev liblapacke-dev gfortran \
    python3-dev python3-numpy \
    libhdf5-dev

# ── 2. Clone sources ──────────────────────────────────────────────────────────
echo "[2/6] Cloning OpenCV ${OPENCV_VERSION}..."
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

if [ ! -d opencv ]; then
    git clone --depth 1 --branch "${OPENCV_VERSION}" \
        https://github.com/opencv/opencv.git opencv
fi
if [ ! -d opencv_contrib ]; then
    git clone --depth 1 --branch "${OPENCV_VERSION}" \
        https://github.com/opencv/opencv_contrib.git opencv_contrib
fi

# ── 3. Configure ──────────────────────────────────────────────────────────────
echo "[3/6] Configuring CMake..."
# Wipe any stale CMake cache so previous flag changes take effect cleanly
rm -rf "${BUILD_DIR}/opencv/build"
mkdir -p "${BUILD_DIR}/opencv/build"
cd "${BUILD_DIR}/opencv/build"

# Python binding
# NEON / ARM optimisations (Cortex-A76 on Pi 5)
# Use libjpeg-turbo for fast MJPEG decode
# V4L2 capture backend
# Disable heavy / unused modules to save build time
# No GUI
# Extra modules (optional — provides additional algorithms)
cmake \
    -D CMAKE_BUILD_TYPE=Release \
    -D CMAKE_INSTALL_PREFIX=/usr/local \
    -D BUILD_opencv_python3=ON \
    -D PYTHON3_EXECUTABLE="${PYTHON_BIN}" \
    -D ENABLE_NEON=ON \
    -D CPU_BASELINE="NEON" \
    -D WITH_JPEG=ON \
    -D BUILD_JPEG=OFF \
    -D WITH_V4L=ON \
    -D WITH_LIBV4L=ON \
    -D BUILD_opencv_dnn=OFF \
    -D BUILD_opencv_ml=OFF \
    -D BUILD_opencv_stitching=OFF \
    -D BUILD_opencv_viz=OFF \
    -D BUILD_opencv_gapi=OFF \
    -D BUILD_EXAMPLES=OFF \
    -D BUILD_TESTS=OFF \
    -D BUILD_PERF_TESTS=OFF \
    -D WITH_GTK=OFF \
    -D WITH_QT=OFF \
    -D WITH_OPENGL=OFF \
    -D OPENCV_EXTRA_MODULES_PATH="${BUILD_DIR}/opencv_contrib/modules" \
    ..

# ── 4. Build ──────────────────────────────────────────────────────────────────
echo "[4/6] Building (using ${NPROC} cores — ~15 min on Pi 5)..."
make -j"${NPROC}"

# ── 5. Install ────────────────────────────────────────────────────────────────
echo "[5/6] Installing to /usr/local..."
sudo make install
sudo ldconfig

# ── 6. Verify ─────────────────────────────────────────────────────────────────
echo "[6/6] Verifying installation..."
python3 - <<'PYCHECK'
import cv2
print(f"  OpenCV version : {cv2.__version__}")
info = cv2.getBuildInformation()
neon_line = [l for l in info.splitlines() if "NEON" in l]
print(f"  NEON support   : {neon_line[0].strip() if neon_line else 'not found in build info'}")
PYCHECK

echo ""
echo "============================================================"
echo " Build complete!  OpenCV ${OPENCV_VERSION} installed."
echo " Run 'python3 -c \"import cv2; print(cv2.__version__)\"' to verify."
echo "============================================================"
