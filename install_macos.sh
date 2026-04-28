#!/usr/bin/env bash
# Marrow macOS install script
# Installs the Python backend and builds the Swift macOS app.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

echo "=== Marrow macOS Install ==="

# ── Python backend ──────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install from https://python.org"
  exit 1
fi

PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python $PYVER"

if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

# Install PortAudio (needed by sounddevice on macOS)
if command -v brew &>/dev/null; then
  brew install portaudio 2>/dev/null || true
else
  echo "WARNING: Homebrew not found. sounddevice may not work without PortAudio."
  echo "  Install Homebrew: https://brew.sh, then run: brew install portaudio"
fi

pip install --upgrade pip wheel
pip install -e "$SCRIPT_DIR" --no-deps 2>/dev/null || true
pip install \
  anthropic \
  faster-whisper \
  sounddevice \
  numpy \
  python-dotenv \
  fastapi \
  "uvicorn[standard]" \
  websockets \
  httpx \
  aiofiles \
  psutil \
  pillow \
  mss \
  apscheduler \
  openai \
  kokoro-onnx \
  pyautogui \
  pystray

echo "Python backend installed."

# ── Swift macOS app ─────────────────────────────────────────────────────────
DESKTOP_DIR="$SCRIPT_DIR/desktop"

if ! command -v swift &>/dev/null; then
  echo ""
  echo "Swift not found. Install Xcode from the App Store, then re-run this script."
  echo "Or build manually: cd $DESKTOP_DIR && swift build -c release"
  echo ""
  echo "The Python backend is ready. Start it with: $SCRIPT_DIR/run_backend.sh"
  exit 0
fi

echo "Building Swift macOS app..."
cd "$DESKTOP_DIR"
swift build -c release 2>&1 | tail -20

BUILD_PATH="$DESKTOP_DIR/.build/release/Marrow"
if [ -f "$BUILD_PATH" ]; then
  echo "Built: $BUILD_PATH"
  cp "$BUILD_PATH" "$SCRIPT_DIR/Marrow"
  echo "Copied to $SCRIPT_DIR/Marrow"
else
  echo "WARNING: Build output not found at $BUILD_PATH"
  echo "Try: cd $DESKTOP_DIR && swift build -c release"
fi

# ── Launcher script ──────────────────────────────────────────────────────────
cat > "$SCRIPT_DIR/run_marrow.sh" <<'LAUNCHER'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate"

# Load .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a; source "$SCRIPT_DIR/.env"; set +a
fi

export MARROW_API_PORT="${MARROW_API_PORT:-8888}"
# Both Swift API clients (APIClient + GeminiClient) route through marrow's local server
export OMI_PYTHON_API_URL="http://localhost:${MARROW_API_PORT}/"
export OMI_API_URL="http://localhost:${MARROW_API_PORT}/"

echo "Starting Marrow backend on port $MARROW_API_PORT..."
python3 "$SCRIPT_DIR/main.py" &
BACKEND_PID=$!

# Give backend 3s to start
sleep 3

# Launch Swift UI if built
MARROW_BIN="$SCRIPT_DIR/Marrow"
DESKTOP_BIN="$SCRIPT_DIR/desktop/.build/release/Marrow"

if [ -f "$MARROW_BIN" ]; then
  echo "Launching Marrow UI..."
  "$MARROW_BIN" &
elif [ -f "$DESKTOP_BIN" ]; then
  echo "Launching Marrow UI (from build)..."
  "$DESKTOP_BIN" &
else
  echo "Marrow UI not built. Run install_macos.sh first."
  echo "Backend is running (PID $BACKEND_PID). API at http://localhost:$MARROW_API_PORT"
fi

# Wait for backend
wait $BACKEND_PID
LAUNCHER

chmod +x "$SCRIPT_DIR/run_marrow.sh"

echo ""
echo "=== Install complete ==="
echo "Run Marrow:  $SCRIPT_DIR/run_marrow.sh"
echo ""
echo "Copy your .env file to $SCRIPT_DIR/.env with:"
echo "  ANTHROPIC_API_KEY=..."
echo "  ELEVENLABS_API_KEY=... (optional)"
echo ""
echo "=== macOS Permission Setup ==="
echo ""
echo "Grant the following in System Settings > Privacy & Security:"
echo ""
echo "  1. Microphone        — for voice recording and transcription"
echo "  2. Screen Recording  — for Marrow to see your screen (Rewind, context)"
echo "  3. Accessibility     — for execution actions (keyboard, mouse, app control)"
echo ""
echo "  open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone'"
echo "  open 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'"
echo "  open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'"
echo ""
echo "Or launch Marrow and use Settings > Permissions to open them from the UI."
