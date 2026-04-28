# Marrow

Marrow is an ambient desktop AI assistant with proactive behavior, conversational control, and tool execution.

It continuously ingests screen context, maintains memory, reasons in the background, and decides whether to speak, act, or stay silent.

## Current State

- Built for "Jarvis-like" laptop operation: context awareness, proactive nudges, action execution, and persistent memory.
- Runs with graceful degradation: if audio or cloud services are missing, core screen + reasoning loops still run.
- Default runtime profile is now conversation-on + talkative proactive mode (applied at startup).

## What Marrow Can Do

- Watch desktop activity (app, title, focus hints, visual extraction).
- Build memory across observations, conversations, actions, and long-horizon patterns.
- Run a proactive reasoning loop with interruption policy and anti-spam dedupe.
- Execute real tasks through tool use (shell, file, web, browser, reminders, mission orchestration, adapters).
- Support conversational turns with follow-up memory and action-trigger handling.

## Architecture

- `capture/` - screen and audio ingestion pipelines.
- `brain/` - reasoning, conversation, interruption policy, world model, proactive behavior.
- `actions/` - tool executor, mission workflows, reminders, adapters, permissions, app control.
- `storage/` - SQLite + FTS memory for screenshots, transcripts, observations, actions, and conversations.
- `ui/` - orb/control bar, toast, overlay, approvals, settings bridge.

## Quick Start

Marrow starts without any API key. The server boots, memory and conversations work, but chat replies need at least one LLM key. Set `OPENAI_API_KEY` for the cheapest path — or `ANTHROPIC_API_KEY` if you have one.

### Step 1 — create your env file

```
~/.marrow/.env          ← recommended (works on all platforms)
omi/marrow/.env         ← fallback (project root)
```

Minimum to get chat working:

```env
OPENAI_API_KEY=sk-...your key here...
```

Full options → see `.env.example` in this folder.

---

### Windows

```powershell
# 1. Install dependencies (one-time)
cd C:\path\to\omi
python -m venv .venv
.venv\Scripts\pip install fastapi uvicorn httpx python-dotenv
.venv\Scripts\pip install -e marrow

# 2. Create env file
mkdir %USERPROFILE%\.marrow
copy marrow\.env.example %USERPROFILE%\.marrow\.env
notepad %USERPROFILE%\.marrow\.env    # add OPENAI_API_KEY

# 3. Smoke-test the server (no UI required)
cd marrow
..\\.venv\\Scripts\\python test_server.py

# 4. Run the full app (Windows system tray UI)
..\\.venv\\Scripts\\python main.py
```

Audio note: if microphone fails on startup set `AUDIO_INPUT_DEVICE=<index>` or `AUDIO_ENABLED=0` in your `.env`.

---

### macOS

```bash
# 1. Install system deps (one-time)
brew install portaudio ffmpeg

# 2. Install Python deps
cd /path/to/omi
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx python-dotenv
.venv/bin/pip install -e marrow

# 3. Create env file
mkdir -p ~/.marrow
cp marrow/.env.example ~/.marrow/.env
nano ~/.marrow/.env    # add OPENAI_API_KEY

# 4. Smoke-test the server
cd marrow
../.venv/bin/python test_server.py

# 5a. Run the Python backend only
../.venv/bin/python main.py

# 5b. Or run with the Swift macOS UI (requires Xcode)
#     Open marrow/desktop/Marrow.xcodeproj → Run
#     Backend starts automatically when the app launches
```

macOS permissions needed on first run: Microphone, Screen Recording, Accessibility.
Go to System Settings → Privacy & Security to grant them.

---

### Linux

```bash
# 1. Install system deps (one-time, Ubuntu/Debian)
sudo apt install python3-dev portaudio19-dev ffmpeg xdotool

# 2. Install Python deps
cd /path/to/omi
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx python-dotenv
.venv/bin/pip install -e marrow

# 3. Create env file
mkdir -p ~/.marrow
cp marrow/.env.example ~/.marrow/.env
nano ~/.marrow/.env    # add OPENAI_API_KEY

# 4. Add yourself to audio/input groups (one-time, then log out/in)
sudo usermod -aG audio,input $USER

# 5. Smoke-test the server
cd marrow
../.venv/bin/python test_server.py

# 6. Run the full app
../.venv/bin/python main.py
```

---

### Smoke Test (all platforms)

```bash
cd marrow
python test_server.py
```

Expected: `9/9 checks passed`. If chat shows `FAIL` your LLM key is missing or wrong — everything else still works.

What each check means:

| Check | What it verifies |
|---|---|
| `/v1/me` | Server is up, identity works |
| `/v1/conversations` | SQLite database is live |
| `/v1/memories` | Memory read/write works |
| `/v1/config/api-keys` | Key distribution to macOS UI works |
| `/v1/users/me/subscription` | Subscription stub (always active) |
| `POST /v1/chat/messages` | LLM routing works end-to-end |
| `/v2/desktop/appcast.xml` | Sparkle update stub works |
| `/v99/whatever` | Catch-all handles unknown routes |

## System Diagram

```text
             +-----------------------+
             |   Screen / Audio I/O  |
             +-----------+-----------+
                         |
                 capture/screen.py
                 capture/audio.py
                         |
                         v
               +--------------------+
               | storage/db.py      |
               | SQLite + FTS       |
               +--------------------+
                         |
             +-----------+-----------+
             |                       |
             v                       v
   brain/reasoning.py        brain/conversation.py
   gate -> generate ->       low-latency turn loop
   critic -> interrupt       follow-up memory
             |                       |
             +-----------+-----------+
                         |
                         v
                 actions/executor.py
                 tools / mission / adapters
                         |
                         v
                      ui/bridge
          orb/controlbar/toast/overlay/approvals
```

## Runtime Model Providers

Set `LLM_PROVIDER` in env:

- `auto` (recommended): OpenAI -> Anthropic -> Ollama -> none.
- `openai`
- `anthropic`
- `ollama`
- `none` (capture/memory only)

Marrow boots even without keys.

## Default Behavior Profile (Important)

On startup, Marrow enforces a default profile so it behaves conversationally and proactively without manual setup:

- `CONVERSATION_ENABLED=1`
- `CONVERSATION_RESPONSE_STYLE=detailed`
- `CONVERSATION_MODEL_TYPE=reasoning`
- `CONVERSATION_MODE_TIMEOUT_SECONDS=120`
- `CONVERSATION_MAX_TURNS=20`
- `CONVERSATION_MAX_TOKENS=420`
- `PROACTIVE_FREQUENCY=4`
- `PROACTIVE_SPEECH_MIN_URGENCY=2`
- `PROACTIVE_AUTO_SPEAK_MIN_URGENCY=2`
- `PROACTIVE_SPEECH_MIN_GAP_SECONDS=30`
- `PROACTIVE_SIGNAL_DEDUP_SECONDS=180`
- `LIVE_KICKOFF_ENABLED=1`
- `LIVE_KICKOFF_DELAY_SECONDS=12`
- `MENTOR_PROACTIVE_ENABLED=1` (buffered gate/generate/critic lane)

This means you should not need to run `/proactive talkative` or `/conversation on` manually every launch.

## Recommended `.env`

```env
LLM_PROVIDER=auto

# Optional cloud keys
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
RETAINDB_API_KEY=
RETAINDB_PROJECT=marrow
RETAINDB_CONTEXT_REFRESH_SECONDS=75
RETAINDB_PROFILE_REFRESH_SECONDS=300

# Local models
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_REASONING_MODEL=llama3.2
OLLAMA_SCORING_MODEL=llama3.2
OLLAMA_VISION_MODEL=llava

# Audio input
AUDIO_ENABLED=1
AUDIO_INPUT_DEVICE=
WAKE_WORD_ENABLED=1
AUDIO_STT_BACKEND=auto
WHISPER_MODEL=small

# Deepgram (optional): STT + better TTS voices
DEEPGRAM_API_KEY=
DEEPGRAM_MODEL=nova-3
DEEPGRAM_LANGUAGE=en
DEEPGRAM_ENDPOINTING_MS=180
DEEPGRAM_UTTERANCE_END_MS=700
DEEPGRAM_TTS_ENABLED=1
DEEPGRAM_TTS_MODEL=aura-2-luna-en
DEEPGRAM_TTS_VOICE=

# Conversation
CONVERSATION_ENABLED=1
CONVERSATION_MODE_TIMEOUT_SECONDS=120
CONVERSATION_MAX_TURNS=20
CONVERSATION_MAX_TOKENS=420
CONVERSATION_MODEL_TYPE=reasoning
CONVERSATION_RESPONSE_STYLE=detailed

# Screen / reasoning cadence
SCREENSHOT_INTERVAL=3
REASONING_INTERVAL=30
CONTEXT_WINDOW_SECONDS=120

# Proactive policy
PROACTIVE_FREQUENCY=4
PROACTIVE_SPEECH_MIN_URGENCY=2
PROACTIVE_AUTO_SPEAK_MIN_URGENCY=2
PROACTIVE_SPEECH_MIN_GAP_SECONDS=30
PROACTIVE_SIGNAL_DEDUP_SECONDS=180
PROACTIVE_TOAST_MIN_URGENCY=1
PROACTIVE_FORCE_TOAST_WHEN_AUDIO_UNAVAILABLE=1
PROACTIVE_STARTUP_DELAY_SECONDS=8
PROACTIVE_BACKOFF_MAX_SECONDS=300

# Mentor proactive lane
MENTOR_PROACTIVE_ENABLED=1
MENTOR_CONTEXT_WINDOW_SECONDS=240
MENTOR_MAX_BUFFER_MESSAGES=50
MENTOR_MIN_NEW_SEGMENTS_FOR_ANALYSIS=6
MENTOR_SILENCE_RESET_SECONDS=120
MENTOR_MIN_WORDS_AFTER_SILENCE=5
MENTOR_MIN_TRANSCRIPT_CHARS=3
MENTOR_RATE_LIMIT_SECONDS=120
MENTOR_MAX_DAILY_NOTIFICATIONS=36

# UI mode
UI_MODE=orb
CONTROL_BAR_AUTO_SHOW=0
```

## Slash Commands

From chat/control bar:

- `/help`
- `/models`
- `/provider <auto|openai|anthropic|ollama|none>`
- `/model <reasoning|scoring|vision> <name>`
- `/capabilities`
- `/selfcheck`
- `/doctor`
- `/chatstyle <short|balanced|detailed|status>`
- `/proactive <quiet|normal|talkative|status>`
- `/conversation <on|off|status>`
- `/mission <start|pause|resume|rollback|status> [goal]`
- `/swarm <run|status> [goal]`
- `/audio <on|off|status>`
- `/hotkey <on|off>`
- `/wakeword <on|off>`

## Behavioral Pipeline

Marrow uses a layered decision flow:

1. Gate: decide if interruption is worth it now.
2. Generate: produce candidate speak/action output.
3. Critic: validate quality/timing.
4. Interrupt policy: meeting/flow/cooldown/dedup checks.

Additionally, Marrow runs a buffered mentor-style proactive lane (ported from Omi-style pattern):

- segment buffer with silence reset
- min-new-segments trigger before analysis
- gate -> generate -> critic sequence
- rate-limit and daily-cap controls

`/doctor` now reports mentor proactive stage counters (runs, gate/critic rejections, sends, buffer state).
It also reports proactive decision-stage counts and AGI ingest retry-queue depth.

The goal is high-signal interruptions without dead silence.

Startup behavior includes a deterministic live kickoff guidance message (non-LLM), so Marrow gives immediate instruction/opinion once context is available.

## Troubleshooting

### It starts, but stays too silent

- Run `/doctor` and check runtime health.
- Confirm screen capture is active (`capture.screen: Screen capture loop started` in logs).
- Confirm provider is healthy (`/models`, startup provider logs).
- If you see stale-screen behavior, check OS permissions (screen recording on macOS).

### It says microphone/device invalid

If logs show `No valid microphone input device detected`, conversation listening is paused.

- Set `AUDIO_INPUT_DEVICE` explicitly, or
- Set `AUDIO_ENABLED=0` to run screen-only mode.

Proactive speaking does not require microphone input.

### Duplicate toasts/messages

Recent updates removed duplicate spoken message surfacing from patched `speak()` path. If duplicates remain, restart and retest once.

### Dependency conflicts

Use a clean virtual environment:

```bash
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
python -m pip check
```

## Common Failure Signatures

| Log/error signature | Likely cause | What to do |
|---|---|---|
| `OpenBLAS error: Memory allocation still failed` | BLAS thread/memory pressure at startup | Restart with latest code (thread guards added); close heavy apps; use project venv |
| `No valid microphone input device detected. Audio capture paused.` | Invalid default mic device or missing input hardware | Set `AUDIO_INPUT_DEVICE=<index_or_name>` or `AUDIO_ENABLED=0` |
| Runs but feels silent | Gate/critic suppressions, stale context, or meeting/flow suppression | Run `/doctor`; verify screen loop logs; test `/proactive status`; check permissions |
| Says it is not watching continuously | Screen dedupe without fresh persisted context or permission issue | Ensure latest runtime (keepalive capture path), verify screen permission, switch apps and retest |
| Same message shown twice | Duplicate surfacing path in UI wiring | Restart on latest code; verify single toast per spoken message |
| Repeats question after user says `yes` | Weak follow-up resolution | Ensure latest runtime (affirmative handling improved in conversation + executor) |
| Frequent `HTTP Request: POST ...` but no user-visible output | Reasoning calls happening but candidate suppressed before surfacing | Check interrupt policy via `/proactive status`; test with talkative profile; inspect stale-screen notices |
| `Provider ... none` or model unavailable | Missing API keys/provider misconfigured/Ollama not running | Set `LLM_PROVIDER` + keys; run Ollama if local; verify with `/models` |

## Local Adapters

Marrow can persist reusable local adapter tools:

- `create_local_adapter`
- `list_local_adapters`
- `verify_local_adapter`

Adapters are stored in `~/.marrow/adapters/` and auto-registered on next run as `adapter_<name>`.

## Mission Mode

Mission primitives are available:

- create plans
- execute step-by-step
- pause/resume
- rollback via rollback actions
- recover from failures with alternate strategy path

Use `/mission ...` commands from chat.

## Security and Safety

- Irreversible or high-risk actions should go through approval paths.
- Keep API keys in env files only (`~/.marrow/.env` recommended).
- Rotate keys if exposed.

## Known Gaps

- Full universal autonomy still depends on model quality and runtime capability availability.
- Audio continuity depends on local device/permissions.
- Proactive quality still benefits from per-user threshold tuning and continued prompt iteration.

## Roadmap Priorities

- Better conversational naturalness and interruption taste.
- Stronger deterministic verification for tool outcomes.
- Enhanced mission UI and confidence reporting.
- Deeper ambient context quality with fewer false suppressions.
