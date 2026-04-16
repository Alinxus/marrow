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
- Applies interruption gating (urgency, meeting detection, flow-state, cooldown, dedupe)
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

## Quick Start (Windows)

```powershell
cd C:\Users\user\Downloads\omi\marrow

python -m venv .venv
.venv\Scripts\activate

pip install -e .
copy .env.example .env

# then edit .env
python main.py
```

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
```

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
