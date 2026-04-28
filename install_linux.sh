#!/usr/bin/env bash
# Marrow Linux install script
# Installs the Python backend (headless mode, no Swift UI on Linux).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

echo "=== Marrow Linux Install ==="

# ── System deps ──────────────────────────────────────────────────────────────
echo "Installing system dependencies..."
if command -v apt-get &>/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq \
    python3 python3-pip python3-venv \
    portaudio19-dev libportaudio2 \
    libsndfile1 ffmpeg \
    libgl1 libglib2.0-0 \
    scrot xdotool x11-utils \
    libxcb-xinerama0 libxkbcommon-x11-0 \
    2>/dev/null || true
elif command -v dnf &>/dev/null; then
  sudo dnf install -y \
    python3 python3-pip \
    portaudio-devel portaudio \
    libsndfile ffmpeg \
    scrot xdotool \
    2>/dev/null || true
elif command -v pacman &>/dev/null; then
  sudo pacman -Sy --noconfirm \
    python python-pip \
    portaudio \
    libsndfile ffmpeg \
    scrot xdotool \
    2>/dev/null || true
else
  echo "WARNING: Unknown package manager. Install portaudio and ffmpeg manually."
fi

# ── Python venv ──────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found."
  exit 1
fi

if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

pip install --upgrade pip wheel

# Core deps (no PyQt6 on Linux — headless backend)
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
  pyautogui \
  pystray

# kokoro-onnx for TTS (optional)
pip install kokoro-onnx 2>/dev/null || echo "kokoro-onnx install failed (optional TTS — continuing)"

echo "Python backend installed."

# ── Launcher ──────────────────────────────────────────────────────────────────
cat > "$SCRIPT_DIR/run_marrow.sh" <<'LAUNCHER'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate"

if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a; source "$SCRIPT_DIR/.env"; set +a
fi

export MARROW_API_PORT="${MARROW_API_PORT:-8888}"
export OMI_PYTHON_API_URL="http://localhost:${MARROW_API_PORT}/"

echo "Starting Marrow on Linux (headless backend, API on port $MARROW_API_PORT)..."
echo "API docs: http://localhost:$MARROW_API_PORT/docs"
exec python3 "$SCRIPT_DIR/main.py"
LAUNCHER

chmod +x "$SCRIPT_DIR/run_marrow.sh"

# ── Systemd service (optional) ────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/marrow.service"
if command -v systemctl &>/dev/null && [ -w "/etc/systemd/system" ]; then
  cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Marrow ambient intelligence backend
After=network.target sound.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/run_marrow.sh
Restart=on-failure
RestartSec=5
Environment=DISPLAY=:0
Environment=HOME=$HOME

[Install]
WantedBy=default.target
SERVICE
  systemctl daemon-reload
  echo "Systemd service installed. Enable with: sudo systemctl enable --now marrow"
fi

echo ""
echo "=== Install complete ==="
echo "Run Marrow:  $SCRIPT_DIR/run_marrow.sh"
echo ""
echo "Copy your .env file to $SCRIPT_DIR/.env with:"
echo "  ANTHROPIC_API_KEY=..."
echo "  AUDIO_ENABLED=1"
echo ""
echo "Note: On Linux, Marrow runs as a headless Python backend."
echo "      API is available at http://localhost:8888/docs"
echo ""
echo "=== Linux Permission Notes ==="
echo ""
echo "Execution (keyboard/mouse/window control via xdotool):"
echo "  - xdotool is already installed above. No special permission needed on most X11 setups."
echo "  - On Wayland: xdotool won't work. Use X11/XWayland or install ydotool."
echo "    sudo apt-get install ydotool   # then: sudo modprobe uinput"
echo ""
echo "Screen capture (mss/scrot):"
echo "  - Works on X11 by default."
echo "  - On Wayland you may need PipeWire screen capture or run with XWayland."
echo ""
echo "Microphone (sounddevice via PortAudio):"
echo "  - Ensure your user is in the 'audio' group:"
echo "    sudo usermod -aG audio \$USER   (logout/login to apply)"
echo "  - PulseAudio/PipeWire should be running. Check: pulseaudio --check"
echo ""
echo "Global hotkeys (keyboard library):"
echo "  - Needs root OR evdev group access:"
echo "    sudo usermod -aG input \$USER   (logout/login to apply)"
echo "  - Alternatively, use Marrow in wake-word or voice-PTT mode (no global hotkey needed)."
