# Marrow

Marrow is an ambient AI that runs on your laptop, watches screen/audio context, builds memory over time, and interrupts only when it has high-value insight.

It is designed for proactive behavior (not just chat-response), local-first operation, and graceful degradation when APIs or devices are missing.

## What Marrow Does

- Captures live desktop context (active app, window title, focused UI, screenshot OCR/vision)
- Captures microphone audio and transcribes speech (Whisper local fallback, optional Deepgram)
- Maintains durable memory (observations, actions, conversation traces, world model)
- Runs a periodic reasoning loop to decide whether to:
  - speak an insight
  - execute an action
  - stay silent
- Applies interruption gating (gate -> generate -> critic, urgency, meeting detection, flow-state, cooldown, dedupe)
- Supports long-horizon context awareness (patterns over days/weeks)

## Key Architecture

- `capture/`: screen + audio ingestion
- `brain/`: reasoning, interrupt policy, world model, context awareness, LLM abstraction
- `actions/`: tools/executor/delegation/approval
- `storage/`: SQLite + FTS memory + history tables
- `ui/`: floating panel bridge (PyQt6)

## LLM Provider Modes

Set `LLM_PROVIDER` in `.env`:

- `auto` (recommended): OpenAI key -> Anthropic key -> local Ollama -> no-LLM mode
- `openai`
- `anthropic`
- `ollama`
- `none` (capture + memory only; no reasoning calls)

Marrow now starts even if keys are missing.

## Installation Guide

### Windows (PowerShell)

```powershell
cd C:\Users\user\Downloads\omi\marrow

py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .

copy .env.example .env
python main.py
```

### macOS (zsh/bash)

```bash
cd ~/Downloads/omi/marrow

python3.11 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .

cp .env.example .env
python main.py
```

Optional macOS audio dependencies:

```bash
brew install portaudio ffmpeg
```

## Dependency Conflicts (Important)

If installs fail due to package conflicts, use this clean-room flow:

1. Delete old virtual env (`.venv`) and recreate it.
2. Upgrade packaging tools before installing (`pip`, `setuptools`, `wheel`).
3. Install Marrow with `pip install -e .`.

If conflict persists, run:

```bash
python -m pip check
python -m pip freeze > pip-lock-debug.txt
```

Then reinstall from a fresh env again.

Notes:

- Marrow supports missing optional services (cloud keys, audio, etc.) and should still boot.
- For local-only runs, set `LLM_PROVIDER=ollama` (or `none` for capture-only mode).
- On Windows, if mic device errors appear, set `AUDIO_INPUT_DEVICE` explicitly or `AUDIO_ENABLED=0`.

## Recommended `.env` Baseline

```env
LLM_PROVIDER=auto

# Optional keys
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Local models (Ollama)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_REASONING_MODEL=llama3.2
OLLAMA_SCORING_MODEL=llama3.2
OLLAMA_VISION_MODEL=llava

# Audio
AUDIO_ENABLED=1
AUDIO_INPUT_DEVICE=
WAKE_WORD_ENABLED=1
WHISPER_MODEL=small

# Screen vision quality
SCREEN_VISION_MAX_SIZE=1920
SCREEN_VISION_JPEG_QUALITY=85

# Timing
REASONING_INTERVAL=30
INTERRUPT_COOLDOWN=90
SCREENSHOT_INTERVAL=3
CONTEXT_WINDOW_SECONDS=120

# Proactive policy
PROACTIVE_FREQUENCY=3
MAX_DAILY_INTERRUPTS=12
```

## Proactive Decision Pipeline

Marrow uses a three-stage interruption policy inspired by Omi's production behavior:

1. Gate: score whether this moment is worth interrupting at all.
2. Generate: produce candidate speak/action output.
3. Critic: final quality check before surfacing.

This reduces spammy interruptions and pushes Marrow toward high-signal moments.

## Vision Quality Notes

If screen understanding feels weak:

- Increase `SCREEN_VISION_MAX_SIZE` (e.g. `2240`)
- Increase `SCREEN_VISION_JPEG_QUALITY` (e.g. `90`)
- Lower `SCREENSHOT_INTERVAL` (e.g. `2`) for denser updates
- Keep browser/app zoom readable (tiny text hurts OCR)

When no vision backend is available, Marrow falls back to local window metadata so capture pipeline still works.

## Audio Device Troubleshooting

If you see `Error querying device -1`:

1. Set `AUDIO_ENABLED=0` to run without mic
2. Or set `AUDIO_INPUT_DEVICE=<index_or_name>` in `.env`
3. Restart Marrow

Marrow now avoids infinite crash loops when input devices are invalid.

## Context Awareness Behavior

Marrow stores long-horizon interaction and media signals as durable memory and lets reasoning infer what matters. It should surface the insight directly (not internal mechanism labels).

Examples of proactive outcomes:

- repeated outbound outreach with no response -> communication strategy warning
- suspicious/high-risk media claims -> factual caution
- active-call participant/presence shift signals -> attention nudge

## Security and Safety

- Action execution supports approvals/guardrails
- Destructive operations should be confirmed via approval paths
- API keys should be kept in `.env` and rotated if exposed

## Current Limitations

- Pixel-level person/face tracking in live calls is heuristic unless a dedicated CV pipeline is added
- Visual extraction quality depends on model/provider availability and text legibility
- On some systems, global hotkey registration can require elevated permissions

## Next Recommended Upgrades

- Add local CV module (person count / face change) for stronger video-call awareness
- Add per-app strategy plugins (email, meetings, coding, browsing)
- Add unit tests for provider fallback + context signal extraction
