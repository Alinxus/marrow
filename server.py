"""
Marrow local API server — bridges the macOS Swift frontend to marrow's Python brain.

Endpoints matching what omi desktop expects:
  WS  /v4/listen                          — live audio → faster-whisper → transcript segments
  WS  /v2/voice-message/transcribe-stream — PTT streaming transcription
  POST /v2/voice-message/transcribe        — PTT batch transcription
  GET  /v1/me                              — user identity
  GET  /v1/conversations                   — recent conversations
  GET  /v1/memories                        — memories/observations
  POST /v1/memories                        — create memory
  DELETE /v1/memories/{id}                 — delete memory
  POST /v1/chat/messages                   — chat with marrow brain
  WS  /v1/chat/messages/stream             — streaming chat

The server runs in a background thread alongside marrow's asyncio loop.
Audio from Swift (16kHz 16-bit PCM) is forwarded to faster-whisper locally —
no cloud, no DeepGram API key required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

_server_thread: Optional[threading.Thread] = None
_asyncio_loop: Optional[asyncio.AbstractEventLoop] = None

PORT = int(os.environ.get("MARROW_API_PORT", "8888"))

# ─── Lazy imports so server module can be imported even if deps missing ────────

def _import_fastapi():
    try:
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
        from fastapi.middleware.cors import CORSMiddleware
        import uvicorn
        return FastAPI, WebSocket, WebSocketDisconnect, HTTPException, CORSMiddleware, uvicorn, Response
    except ImportError as e:
        raise ImportError(f"fastapi/uvicorn not installed: {e}. Run: pip install fastapi uvicorn") from e


# ─── Build the app ─────────────────────────────────────────────────────────────

def _build_app():
    FastAPI, WebSocket, WebSocketDisconnect, HTTPException, CORSMiddleware, uvicorn, Response = _import_fastapi()

    app = FastAPI(title="Marrow Local API", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── User identity ──────────────────────────────────────────────────────────

    @app.get("/v1/me")
    async def get_me():
        from storage import db
        return {
            "id": "local-user",
            "email": "local@marrow.ai",
            "name": "Marrow User",
            "created_at": time.time(),
        }

    # ── Conversations ──────────────────────────────────────────────────────────

    @app.get("/v1/conversations")
    async def list_conversations(limit: int = 50, offset: int = 0):
        try:
            from storage import db
            rows = db.get_recent_conversations(limit=limit)
            convs = []
            for r in rows:
                convs.append({
                    "id": str(r.get("id", uuid.uuid4())),
                    "created_at": r.get("ts", time.time()),
                    "transcript_segments": _parse_transcript(r.get("transcript_text", "")),
                    "summary": r.get("summary", ""),
                    "title": r.get("title") or _snippet(r.get("transcript_text", ""), 60),
                    "structured": {},
                    "apps": [],
                    "plugins_results": [],
                })
            return {"conversations": convs, "total": len(convs)}
        except Exception as e:
            log.warning("conversations error: %s", e)
            return {"conversations": [], "total": 0}

    @app.get("/v1/conversations/{conv_id}")
    async def get_conversation(conv_id: str):
        raise HTTPException(status_code=404, detail="not found")

    # ── Memories ───────────────────────────────────────────────────────────────

    @app.get("/v1/memories")
    async def list_memories(limit: int = 100, offset: int = 0):
        try:
            from storage import db
            rows = db.get_observations(limit=limit)
            mems = [_row_to_memory(r) for r in rows]
            return {"memories": mems, "total": len(mems)}
        except Exception as e:
            log.warning("memories error: %s", e)
            return {"memories": [], "total": 0}

    @app.post("/v1/memories")
    async def create_memory(body: dict):
        try:
            from storage import db
            content = body.get("content", "")
            category = body.get("category", "manual")
            if content:
                db.insert_observation(type_=category, content=content)
                mid = int(time.time() * 1000)
                return {"id": str(mid), "content": content, "category": category,
                        "created_at": time.time()}
        except Exception as e:
            log.warning("create memory error: %s", e)
        raise HTTPException(status_code=500, detail="failed")

    @app.delete("/v1/memories/{memory_id}")
    async def delete_memory(memory_id: str):
        try:
            from storage import db
            # observations don't have a delete function yet — no-op gracefully
            pass
            return {"status": "ok"}
        except Exception as e:
            log.warning("delete memory error: %s", e)
            raise HTTPException(status_code=404, detail="not found")

    # ── Chat ───────────────────────────────────────────────────────────────────

    async def _llm_call(text: str, system: str, model: str) -> dict:
        """Route to OpenAI (default) or Anthropic. Both need an API key."""
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        base_url = os.environ.get("LLM_BASE_URL", "").strip()
        model = model or os.environ.get("LLM_MODEL", "").strip()

        # Anthropic wins only when explicitly requested via model name or env var
        prefer_anthropic = (
            (model and model.startswith("claude"))
            or os.environ.get("LLM_PROVIDER", "").lower() == "anthropic"
        )

        if prefer_anthropic and anthropic_key:
            return await _call_anthropic(text, system, model or "claude-sonnet-4-5-20251022", anthropic_key)

        # Default: OpenAI-compatible (OpenAI, Ollama, any local server)
        if openai_key or base_url:
            return await _call_openai_compat(
                text, system, model or "gpt-4o-mini",
                openai_key, base_url or "https://api.openai.com/v1"
            )

        # Anthropic fallback if no OpenAI key but Anthropic key exists
        if anthropic_key:
            return await _call_anthropic(text, system, model or "claude-haiku-4-5", anthropic_key)

        raise RuntimeError(
            "No LLM API key configured. Set OPENAI_API_KEY (or ANTHROPIC_API_KEY) "
            "in ~/.marrow/.env and restart."
        )

    async def _call_anthropic(text: str, system: str, model: str, api_key: str) -> dict:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx not installed — run: pip install httpx")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict = {
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": text}],
        }
        if system:
            payload["system"] = system
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
            if resp.status_code == 401:
                raise ValueError("Anthropic API key is invalid or expired (401). Set a valid ANTHROPIC_API_KEY.")
            resp.raise_for_status()
            data = resp.json()
        text_out = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        usage = data.get("usage", {})
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cost = (in_tok * 3.0 + out_tok * 15.0) / 1_000_000
        return {"text": text_out, "input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": cost}

    async def _call_openai_compat(text: str, system: str, model: str, api_key: str, base_url: str) -> dict:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx not installed — run: pip install httpx")
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": text})
        payload = {"model": model, "messages": messages, "max_tokens": 4096}
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        text_out = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        usage = data.get("usage", {})
        return {
            "text": text_out,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cost_usd": 0.0,
        }

    @app.post("/v1/chat/messages")
    async def chat(body: dict):
        text = (body.get("text") or body.get("prompt") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        system = body.get("system_prompt", "") or ""
        model = body.get("model", "") or ""
        try:
            result = await _llm_call(text, system, model)
            return {
                "id": str(uuid.uuid4()),
                "text": result.get("text") or "",
                "role": "assistant",
                "created_at": time.time(),
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
                "cost_usd": result.get("cost_usd", 0.0),
            }
        except RuntimeError as e:
            # Missing API key — return 200 with an error message so the UI shows it
            log.warning("chat: %s", e)
            return {
                "id": str(uuid.uuid4()),
                "text": str(e),
                "role": "assistant",
                "created_at": time.time(),
                "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            }
        except Exception as e:
            log.error("chat error: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.websocket("/v1/chat/messages/stream")
    async def chat_stream(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                data = await ws.receive_json()
                text = (data.get("text") or data.get("prompt") or "").strip()
                if not text:
                    continue
                system = data.get("system_prompt", "") or ""
                model = data.get("model", "") or ""
                result = await _llm_call(text, system, model)
                await ws.send_json({
                    "id": str(uuid.uuid4()),
                    "text": result["text"],
                    "role": "assistant",
                    "created_at": time.time(),
                    "input_tokens": result.get("input_tokens", 0),
                    "output_tokens": result.get("output_tokens", 0),
                    "cost_usd": result.get("cost_usd", 0.0),
                    "done": True,
                })
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.warning("chat_stream error: %s", e)
            try:
                await ws.send_json({"error": str(e)})
            except Exception:
                pass

    # ── Live audio → transcription (omi /v4/listen protocol) ──────────────────

    @app.websocket("/v4/listen")
    async def listen_ws(ws: WebSocket):
        """
        Receives 16kHz 16-bit PCM audio chunks from Swift AudioCaptureService.
        Accumulates ~3s of audio, runs faster-whisper locally, returns BackendSegment JSON.
        Also triggers marrow's conversation/reasoning loop on each transcript.
        """
        await ws.accept()
        log.info("WS /v4/listen: client connected")
        audio_buf = bytearray()
        CHUNK_BYTES = 16000 * 2 * 3  # 3 seconds of 16kHz 16-bit mono = 96000 bytes

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=30.0)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "ping"})
                    continue

                if msg["type"] == "websocket.disconnect":
                    break

                if msg["type"] == "websocket.receive":
                    if "bytes" in msg and msg["bytes"]:
                        audio_buf.extend(msg["bytes"])
                    elif "text" in msg and msg["text"]:
                        # Control messages (e.g. language change)
                        try:
                            ctrl = json.loads(msg["text"])
                            log.debug("listen_ws control: %s", ctrl)
                        except Exception:
                            pass
                        continue

                # Process when we have enough audio
                while len(audio_buf) >= CHUNK_BYTES:
                    chunk = bytes(audio_buf[:CHUNK_BYTES])
                    audio_buf = audio_buf[CHUNK_BYTES:]
                    segments = await _transcribe_chunk(chunk, sample_rate=16000)
                    if segments:
                        await ws.send_json(segments)
                        # Feed transcript into marrow's reasoning context
                        _feed_transcript_to_marrow(segments)

        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.warning("listen_ws error: %s", e)
        finally:
            # Process any remaining audio
            if len(audio_buf) > 3200:  # at least 100ms
                try:
                    segments = await _transcribe_chunk(bytes(audio_buf), sample_rate=16000)
                    if segments:
                        await ws.send_json(segments)
                except Exception:
                    pass
            log.info("WS /v4/listen: client disconnected")

    # ── PTT streaming transcription (/v2/voice-message/transcribe-stream) ─────

    @app.websocket("/v2/voice-message/transcribe-stream")
    async def ptt_stream_ws(ws: WebSocket):
        await ws.accept()
        audio_buf = bytearray()
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=30.0)
                except asyncio.TimeoutError:
                    continue

                if msg["type"] == "websocket.disconnect":
                    break
                if msg["type"] != "websocket.receive":
                    continue

                if "bytes" in msg and msg["bytes"]:
                    audio_buf.extend(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    if msg["text"] == "finalize":
                        # Transcribe everything accumulated
                        if audio_buf:
                            segments = await _transcribe_chunk(bytes(audio_buf), sample_rate=16000)
                            if segments:
                                await ws.send_json(segments)
                            audio_buf.clear()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.warning("ptt_stream error: %s", e)

    # ── PTT batch transcription (/v2/voice-message/transcribe) ────────────────

    @app.post("/v2/voice-message/transcribe")
    async def ptt_batch(body: dict):
        # Body: { "audio": "<base64 pcm>" } or raw bytes via content-type
        import base64
        audio_b64 = body.get("audio", "")
        if not audio_b64:
            raise HTTPException(status_code=400, detail="audio required")
        try:
            audio_bytes = base64.b64decode(audio_b64)
            segments = await _transcribe_chunk(audio_bytes, sample_rate=16000)
            text = " ".join(s.get("text", "") for s in (segments or []))
            return {"text": text, "segments": segments or []}
        except Exception as e:
            log.error("ptt_batch error: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # ── Subscription/tier (stub — all features unlocked locally) ──────────────

    @app.get("/v1/users/me/subscription")
    async def get_subscription():
        return {"plan": {"id": "local", "name": "Local", "monthly_price": 0},
                "status": "active", "is_active": True}

    @app.get("/v1/users/me/usage")
    async def get_usage():
        return {"storage_mb": 0, "storage_limit_mb": 99999}

    # ── Goals (stub) ───────────────────────────────────────────────────────────

    @app.get("/v1/users/me/goals")
    async def get_goals():
        return {"goals": []}

    @app.post("/v1/users/me/goals")
    async def create_goal(body: dict):
        return {"id": str(uuid.uuid4()), **body}

    # ── Apps marketplace (stub) ────────────────────────────────────────────────

    @app.get("/v1/apps")
    async def list_apps(limit: int = 50, offset: int = 0, enabled: bool = False):
        return {"apps": [], "total": 0}

    # ── Chat sessions (omi v2 format) ─────────────────────────────────────────

    _sessions: dict = {}

    @app.post("/v2/chat-sessions")
    async def create_session(body: dict):
        sid = str(uuid.uuid4())
        _sessions[sid] = {"id": sid, "title": body.get("title", "New Chat"),
                          "messages": [], "created_at": time.time()}
        return _sessions[sid]

    @app.get("/v2/chat-sessions")
    async def list_sessions(limit: int = 50, offset: int = 0):
        vals = list(_sessions.values())[offset:offset+limit]
        return {"sessions": vals, "total": len(_sessions)}

    @app.get("/v2/chat-sessions/{sid}")
    async def get_session(sid: str):
        s = _sessions.get(sid)
        if not s:
            raise HTTPException(status_code=404, detail="not found")
        return s

    @app.patch("/v2/chat-sessions/{sid}")
    async def update_session(sid: str, body: dict):
        if sid not in _sessions:
            raise HTTPException(status_code=404, detail="not found")
        _sessions[sid].update(body)
        return _sessions[sid]

    @app.delete("/v2/chat-sessions/{sid}")
    async def delete_session(sid: str):
        _sessions.pop(sid, None)
        return {"status": "ok"}

    @app.post("/v2/chat/initial-message")
    async def initial_message(body: dict):
        text = body.get("text", body.get("message", "")).strip()
        if not text:
            return {"id": str(uuid.uuid4()), "text": "Hello! How can I help?",
                    "role": "assistant", "created_at": time.time()}
        try:
            from brain.conversation import handle_turn
            reply = await handle_turn(text)
        except Exception as e:
            log.warning("initial_message brain error: %s", e)
            reply = "I'm ready to help. What would you like to know?"
        msg = {"id": str(uuid.uuid4()), "text": reply,
               "role": "assistant", "created_at": time.time()}
        # attach to session if provided
        sid = body.get("session_id")
        if sid and sid in _sessions:
            _sessions[sid]["messages"].append(msg)
        return msg

    @app.post("/v2/chat/generate-title")
    async def generate_title(body: dict):
        msgs = body.get("messages", [])
        first = next((m.get("text", m.get("content", "")) for m in msgs
                      if m.get("role") == "user"), "")
        title = (first[:50] + "...") if len(first) > 50 else first or "Chat"
        return {"title": title}

    # ── Gemini proxy → Claude (for ProactiveAssistants screen analysis) ────────
    # omi desktop's GeminiClient calls OMI_API_URL/v1/proxy/gemini/*
    # We proxy those to Claude so no Gemini key is needed.

    @app.post("/v1/proxy/gemini/models/{model_path:path}")
    async def gemini_proxy(model_path: str, body: dict):
        """Translate Gemini generateContent request → Claude → return Gemini-compatible response."""
        return await _gemini_to_claude(body)

    @app.post("/v1/proxy/gemini-stream/models/{model_path:path}")
    async def gemini_stream_proxy(model_path: str, body: dict):
        return await _gemini_to_claude(body)

    @app.post("/v1/proxy/gemini/models/{model_path:path}/embedContent")
    async def embed_content(model_path: str, body: dict):
        """Stub embeddings — return zero vector (knowledge graph won't break)."""
        return {"embedding": {"values": [0.0] * 768}}

    @app.post("/v1/proxy/gemini/models/{model_path:path}/batchEmbedContents")
    async def batch_embed(model_path: str, body: dict):
        requests = body.get("requests", [])
        return {"embeddings": [{"values": [0.0] * 768} for _ in requests]}

    # ── Proactive notification stream (Python → Swift) ────────────────────────
    # Python reasoning_loop calls push_proactive_event() to broadcast to all
    # connected Swift clients. Swift subscribes here and shows FloatingBarNotifications.

    @app.websocket("/v1/proactive/stream")
    async def proactive_stream(ws: WebSocket):
        await ws.accept()
        _proactive_clients.add(ws)
        log.info("Proactive stream client connected (total: %d)", len(_proactive_clients))
        try:
            while True:
                # Keep alive — client sends pings
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=30.0)
                    if msg.get("type") == "websocket.disconnect":
                        break
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "ping"})
        except Exception:
            pass
        finally:
            _proactive_clients.discard(ws)
            log.info("Proactive stream client disconnected (total: %d)", len(_proactive_clients))

    # ── Execution layer (Swift → Python execution_engine) ────────────────────

    @app.post("/v1/execute")
    async def execute_action(body: dict):
        """Execute a computer action via marrow's execution_engine."""
        action = body.get("action", "").strip()
        context = body.get("context", "")
        if not action:
            raise HTTPException(status_code=400, detail="action required")
        try:
            from actions.executor import execute_action as _exec
            result = await _exec(action, context=context)
            return {"status": "ok", "result": str(result)}
        except Exception as e:
            log.warning("execute_action error: %s", e)
            return {"status": "error", "result": str(e)}

    # ── Desktop chat messages (ChatProvider save/load) ─────────────────────────

    _message_store: list = []  # in-memory fallback

    @app.post("/v2/desktop/messages")
    async def save_desktop_message(body: dict):
        """Store a chat message; returns id + created_at for ChatProvider."""
        msg_id = str(uuid.uuid4())
        ts = time.time()
        try:
            from storage import db
            if hasattr(db, "insert_transcript"):
                db.insert_transcript(ts=ts, text=body.get("text", ""))
        except Exception:
            pass
        _message_store.append({
            "id": msg_id,
            "text": body.get("text", ""),
            "sender": body.get("sender", "human"),
            "app_id": body.get("app_id"),
            "session_id": body.get("session_id"),
            "created_at": ts,
        })
        return {"id": msg_id, "created_at": ts}

    @app.get("/v2/desktop/messages")
    async def get_desktop_messages(
        session_id: str = "", app_id: str = "", limit: int = 100, offset: int = 0
    ):
        """Return stored messages for a session."""
        msgs = _message_store
        if session_id:
            msgs = [m for m in msgs if m.get("session_id") == session_id]
        if app_id:
            msgs = [m for m in msgs if m.get("app_id") == app_id]
        return msgs[offset:offset + limit]

    @app.delete("/v2/desktop/messages")
    async def delete_desktop_messages(app_id: str = ""):
        count = len(_message_store)
        if app_id:
            before = len(_message_store)
            _message_store[:] = [m for m in _message_store if m.get("app_id") != app_id]
            count = before - len(_message_store)
        else:
            _message_store.clear()
        return {"status": "ok", "deleted_count": count}

    @app.post("/v2/desktop/messages/{message_id}/rate")
    async def rate_desktop_message(message_id: str, body: dict):
        return {"status": "ok"}

    @app.post("/v2/chat/generate-title")
    async def generate_chat_title(body: dict):
        """Generate a session title from messages using the brain."""
        messages = body.get("messages", [])
        if not messages:
            return {"title": "New Chat"}
        try:
            from brain import conversation as conv
            # Build a short prompt from the first user message
            first_user = next((m.get("text", "") for m in messages if m.get("sender") == "human"), "")
            if first_user:
                title = first_user[:60].strip()
                if len(first_user) > 60:
                    title += "…"
                return {"title": title}
        except Exception:
            pass
        return {"title": "Chat"}

    @app.post("/v1/llm-usage")
    async def record_llm_usage(body: dict):
        return {"status": "ok"}

    @app.get("/v1/users/me/llm-usage/cost")
    async def llm_cost():
        return {"total_cost_usd": 0.0}

    # ── Tasks ──────────────────────────────────────────────────────────────────

    @app.get("/v1/users/me/tasks")
    async def list_tasks():
        try:
            from storage import db
            rows = db.get_recent_todos(limit=100) if hasattr(db, "get_recent_todos") else []
            return {"tasks": [_row_to_task(r) for r in rows]}
        except Exception:
            return {"tasks": []}

    @app.post("/v1/users/me/tasks")
    async def create_task(body: dict):
        try:
            from storage import db
            tid = db.insert_todo(
                ts=time.time(),
                text=body.get("text", ""),
                source="swift",
                priority=body.get("priority", 0),
            )
            return {"id": str(tid), **body, "created_at": time.time(), "completed": False}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.patch("/v1/users/me/tasks/{tid}")
    async def update_task(tid: str, body: dict):
        return {"id": tid, **body}

    @app.delete("/v1/users/me/tasks/{tid}")
    async def delete_task(tid: str):
        return {"status": "ok"}

    # ── Agent/VM stubs (omi desktop uses these for task agent feature) ─────────

    @app.get("/v1/agents")
    async def list_agents():
        return {"agents": [{"id": "marrow", "name": "Marrow", "status": "ready"}]}

    @app.post("/v1/agents/{agent_id}/chat")
    async def agent_chat(agent_id: str, body: dict):
        return await chat(body)

    # ── Permission checks (called by Swift PermissionsPage) ───────────────────

    @app.get("/v1/permissions/check")
    async def permissions_check():
        """Return human-readable permission checklist from Python side."""
        try:
            from actions.permissions import check_permissions
            report = check_permissions(detailed=True)
            return {"status": "ok", "report": report}
        except Exception as e:
            return {"status": "error", "report": str(e)}

    @app.post("/v1/permissions/open")
    async def permissions_open():
        """Open OS permission panels (Screen Recording, Microphone, Accessibility)."""
        try:
            from actions.permissions import open_permission_panels
            result = open_permission_panels()
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "result": str(e)}

    # ── Feedback endpoint ──────────────────────────────────────────────────────

    @app.post("/v1/feedback")
    async def feedback(body: dict):
        msg = body.get("message", "").strip()
        name = body.get("name", "").strip()
        email = body.get("email", "").strip()
        log.info("Feedback received from %s <%s>: %s", name or "anonymous", email or "no email", msg[:200] or "(no message)")
        try:
            from storage import db
            if hasattr(db, "insert_observation"):
                db.insert_observation(type_="feedback", content=f"[{name}] {msg}" if name else msg)
        except Exception:
            pass
        return {"status": "ok"}

    # ── Chat messages for ChatLab rating view ─────────────────────────────────

    @app.get("/v2/messages")
    async def list_messages(limit: int = 500):
        """Return recent chat messages with ratings for ChatLab view."""
        try:
            from storage import db
            sessions = db.get_recent_sessions(limit=min(limit, 500)) if hasattr(db, "get_recent_sessions") else []
            messages = []
            for s in sessions:
                messages.append({
                    "id": str(s.get("id", "")),
                    "text": s.get("text", s.get("content", "")),
                    "created_at": s.get("ts", s.get("created_at", 0)),
                    "rating": s.get("rating", None),
                    "session_id": str(s.get("session_id", "")),
                })
            return messages
        except Exception:
            return []

    # ── People / speaker identification stubs ─────────────────────────────────

    @app.get("/v1/users/people")
    async def list_people():
        return {"people": []}

    @app.post("/v1/users/people")
    async def create_person(body: dict):
        return {"id": str(uuid.uuid4()), **body}

    @app.patch("/v1/users/people/{person_id}/name")
    async def update_person_name(person_id: str, value: str = ""):
        return {"id": person_id, "name": value}

    # ── Goals stubs ────────────────────────────────────────────────────────────

    @app.get("/v1/goals")
    async def list_goals():
        return {"goals": []}

    @app.post("/v1/goals")
    async def create_goal(body: dict):
        return {"id": str(uuid.uuid4()), **body}

    @app.delete("/v1/goals/{goal_id}")
    async def delete_goal(goal_id: str):
        return {"status": "ok"}

    @app.patch("/v1/goals/{goal_id}/progress")
    async def update_goal_progress(goal_id: str, current_value: float = 0):
        return {"id": goal_id, "current_value": current_value}

    # ── Folder stubs ───────────────────────────────────────────────────────────

    @app.get("/v1/conversations/folders")
    async def list_folders():
        return {"folders": []}

    @app.post("/v1/conversations/folders")
    async def create_folder(body: dict):
        return {"id": str(uuid.uuid4()), **body}

    # ── Recording / privacy settings stubs ────────────────────────────────────

    @app.post("/v1/users/store-recording-permission")
    async def store_recording_permission(value: bool = True):
        return {"status": "ok", "value": value}

    @app.post("/v1/users/private-cloud-sync")
    async def private_cloud_sync(value: bool = False):
        return {"status": "ok", "value": value}

    # ── Conversation segments / share stubs ───────────────────────────────────

    @app.post("/v1/conversations/{cid}/segments/assign-bulk")
    async def assign_segments_bulk(cid: str, body: dict):
        return {"status": "ok"}

    @app.post("/v1/conversations/{cid}/folder")
    async def set_conversation_folder(cid: str, body: dict):
        return {"status": "ok"}

    @app.post("/v1/conversations/{cid}/starred")
    async def star_conversation(cid: str, starred: bool = True):
        return {"status": "ok"}

    @app.post("/v1/conversations/{cid}/visibility")
    async def set_conversation_visibility(cid: str, value: str = "shared", visibility: str = "shared"):
        return {"status": "ok"}

    # ── Appcast stub (Sparkle update check — no-op in marrow) ─────────────────

    @app.get("/v2/desktop/appcast.xml")
    async def appcast():
        """Return empty appcast — marrow doesn't use Sparkle auto-update."""
        return Response(content='<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>',
                        media_type="application/xml")

    # ── API key distribution (serves env vars to Swift clients) ───────────────

    @app.get("/v1/config/api-keys")
    async def api_keys():
        """Serve configured API keys to Swift clients (ElevenLabs TTS, etc)."""
        import os
        return {
            "elevenLabsApiKey": os.environ.get("ELEVENLABS_API_KEY") or None,
            "openaiApiKey": os.environ.get("OPENAI_API_KEY") or None,
            "anthropicApiKey": os.environ.get("ANTHROPIC_API_KEY") or None,
            "geminiApiKey": None,
            "firebaseApiKey": None,
            "googleCalendarApiKey": os.environ.get("GOOGLE_CALENDAR_API_KEY") or None,
        }

    # ── Catch-all for unimplemented endpoints ──────────────────────────────────

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def catch_all(path: str):
        return {"status": "ok", "path": path, "note": "marrow local api"}

    return app, uvicorn


# ─── Proactive event broadcast ────────────────────────────────────────────────

# Connected Swift WebSocket clients for proactive notifications
_proactive_clients: set = set()


def push_proactive_event(title: str, message: str, assistant_id: str = "marrow",
                         context: dict | None = None) -> None:
    """
    Called by marrow's reasoning_loop when a proactive notification fires.
    Broadcasts to all connected Swift FloatingControlBar clients.
    Thread-safe — can be called from any thread.
    """
    if not _proactive_clients:
        return
    payload = {
        "type": "notification",
        "title": title,
        "message": message,
        "assistant_id": assistant_id,
        "context": context or {},
        "ts": time.time(),
    }
    # Schedule on the event loop that owns the FastAPI app
    loop = _get_server_loop()
    if loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(_broadcast_proactive(payload), loop)


async def _broadcast_proactive(payload: dict) -> None:
    dead = set()
    for ws in list(_proactive_clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    _proactive_clients.difference_update(dead)


_server_loop: asyncio.AbstractEventLoop | None = None


def _get_server_loop() -> asyncio.AbstractEventLoop | None:
    return _server_loop


# ─── Gemini → Claude proxy ────────────────────────────────────────────────────

async def _gemini_to_claude(body: dict) -> dict:
    """
    Translate a Gemini generateContent request to a Claude call and return
    a Gemini-compatible response. Allows omi's ProactiveAssistants (FocusAssistant,
    InsightAssistant, MemoryAssistant) to work with marrow's local Claude backend.
    """
    try:
        import anthropic
        import config

        # Extract text from Gemini contents format
        contents = body.get("contents", [])
        system_parts = (body.get("systemInstruction") or body.get("system_instruction") or {}).get("parts", [])
        system_text = " ".join(p.get("text", "") for p in system_parts if p.get("text"))

        # Build messages for Claude
        messages = []
        for c in contents:
            role = c.get("role", "user")
            claude_role = "user" if role in ("user", "model") and role != "model" else "assistant"
            parts = c.get("parts", [])
            text_parts = [p.get("text", "") for p in parts if p.get("text")]
            # Handle inline image data
            content_blocks = []
            for p in parts:
                if p.get("text"):
                    content_blocks.append({"type": "text", "text": p["text"]})
                elif p.get("inlineData") or p.get("inline_data"):
                    idata = p.get("inlineData") or p.get("inline_data")
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": idata.get("mimeType") or idata.get("mime_type", "image/jpeg"),
                            "data": idata.get("data", ""),
                        }
                    })
            if content_blocks:
                messages.append({"role": claude_role, "content": content_blocks})
            elif text_parts:
                messages.append({"role": claude_role, "content": " ".join(text_parts)})

        if not messages:
            return _gemini_empty_response()

        model = getattr(config, "ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        client = anthropic.Anthropic(api_key=getattr(config, "ANTHROPIC_API_KEY", ""))

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: client.messages.create(
            model=model,
            max_tokens=2048,
            system=system_text or "You are a helpful assistant.",
            messages=messages,
        ))

        text_out = response.content[0].text if response.content else ""
        return {
            "candidates": [{
                "content": {"parts": [{"text": text_out}], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }],
            "usageMetadata": {
                "promptTokenCount": response.usage.input_tokens,
                "candidatesTokenCount": response.usage.output_tokens,
                "totalTokenCount": response.usage.input_tokens + response.usage.output_tokens,
            }
        }
    except Exception as e:
        log.warning("gemini_to_claude error: %s", e)
        return _gemini_empty_response(str(e))


def _gemini_empty_response(error: str = "") -> dict:
    return {
        "candidates": [{
            "content": {"parts": [{"text": error or ""}], "role": "model"},
            "finishReason": "STOP", "index": 0,
        }],
        "usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0, "totalTokenCount": 0}
    }


def _row_to_task(r) -> dict:
    return {
        "id": str(r.get("id", uuid.uuid4())),
        "text": r.get("text", ""),
        "completed": bool(r.get("completed", False)),
        "priority": r.get("priority", 0),
        "created_at": r.get("ts", time.time()),
    }


# ─── Transcription helper ──────────────────────────────────────────────────────

_whisper_model = None
_whisper_lock = asyncio.Lock()

async def _transcribe_chunk(pcm_bytes: bytes, sample_rate: int = 16000) -> list | None:
    """Convert 16-bit PCM bytes → numpy float32 → faster-whisper segments."""
    global _whisper_model
    if not pcm_bytes:
        return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, pcm_bytes, sample_rate)


def _transcribe_sync(pcm_bytes: bytes, sample_rate: int) -> list | None:
    global _whisper_model
    try:
        import config
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            model_size = getattr(config, "WHISPER_MODEL", "base.en")
            device = getattr(config, "WHISPER_DEVICE", "cpu")
            compute = getattr(config, "WHISPER_COMPUTE_TYPE", "int8")
            log.info("Loading faster-whisper model: %s on %s", model_size, device)
            _whisper_model = WhisperModel(model_size, device=device, compute_type=compute)

        # 16-bit signed PCM → float32 [-1, 1]
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        segments_iter, info = _whisper_model.transcribe(
            audio,
            language=getattr(config, "LANGUAGE", None),
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
        )

        result = []
        seg_id = 0
        for seg in segments_iter:
            text = seg.text.strip()
            if not text:
                continue
            result.append({
                "id": str(seg_id),
                "text": text,
                "speaker": "SPEAKER_00",
                "speaker_id": 0,
                "is_user": True,
                "person_id": None,
                "start": float(seg.start),
                "end": float(seg.end),
                "translations": [],
            })
            seg_id += 1

        return result if result else None

    except Exception as e:
        log.error("transcribe_sync error: %s", e)
        return None


def _feed_transcript_to_marrow(segments: list) -> None:
    """Push transcript text into marrow's audio capture buffer for reasoning."""
    try:
        if not segments:
            return
        from storage import db
        full_text = " ".join(s.get("text", "") for s in segments if s.get("text"))
        if full_text.strip():
            db.insert_transcript(ts=time.time(), text=full_text.strip())
    except Exception as e:
        log.debug("feed_transcript error: %s", e)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _parse_transcript(text: str) -> list:
    if not text:
        return []
    return [{"id": "0", "text": text, "speaker": "SPEAKER_00", "is_user": True,
             "start": 0, "end": 0}]


def _snippet(text: str, length: int) -> str:
    if not text:
        return "Conversation"
    return text[:length] + ("..." if len(text) > length else "")


def _row_to_memory(r) -> dict:
    return {
        "id": str(r.get("id", uuid.uuid4())),
        "content": r.get("content", ""),
        "category": r.get("type", "general"),
        "created_at": r.get("ts", time.time()),
        "updated_at": r.get("ts", time.time()),
        "manually_added": r.get("obs_type") == "manual",
        "structured": {},
    }


# ─── Server lifecycle ──────────────────────────────────────────────────────────

def start_server() -> None:
    """Start the FastAPI server in a background daemon thread."""
    global _server_thread
    if _server_thread and _server_thread.is_alive():
        log.info("Marrow API server already running on port %d", PORT)
        return

    def _run():
        global _server_loop
        try:
            app, uvicorn = _build_app()
            import uvicorn as uv

            async def _serve():
                global _server_loop
                _server_loop = asyncio.get_event_loop()
                cfg = uv.Config(app=app, host="127.0.0.1", port=PORT,
                                log_level="warning", access_log=False)
                srv = uv.Server(cfg)
                await srv.serve()

            asyncio.run(_serve())
        except Exception as e:
            log.error("Marrow API server failed: %s", e)

    _server_thread = threading.Thread(target=_run, name="marrow-api-server", daemon=True)
    _server_thread.start()
    log.info("Marrow API server starting on http://127.0.0.1:%d", PORT)
