"""
Quick smoke-test for the marrow server on Windows.
Run from the marrow/ directory:

    cd omi/marrow
    ..\\.venv\\Scripts\\python test_server.py

Starts only the FastAPI server (no PyQt6, no audio, no screen capture).
Tests every critical endpoint and prints PASS / FAIL per check.
"""

import asyncio
import sys
import time
import threading
import urllib.request
import urllib.error
import json
import os


def _start_server():
    """Boot just the FastAPI layer without the full marrow main loop."""
    import config  # loads .env
    from server import _build_app
    import uvicorn

    app, _ = _build_app()

    async def _serve():
        cfg = uvicorn.Config(app=app, host="127.0.0.1", port=8888,
                             log_level="warning", access_log=False)
        srv = uvicorn.Server(cfg)
        await srv.serve()

    asyncio.run(_serve())


def _wait_for_server(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:8888/v1/me", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8888{path}", timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_error": str(e)}


def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:8888{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "detail": e.read().decode()}
    except Exception as e:
        return {"_error": str(e)}


def check(label, result, expect_key=None, expect_value=None):
    if "_error" in result:
        print(f"  FAIL  {label}: {result['_error']}")
        return False
    if expect_key and expect_key not in result:
        print(f"  FAIL  {label}: missing '{expect_key}' in {list(result.keys())}")
        return False
    if expect_key and expect_value is not None and result.get(expect_key) != expect_value:
        print(f"  FAIL  {label}: {expect_key}={result[expect_key]!r} (wanted {expect_value!r})")
        return False
    val = result.get(expect_key, "ok") if expect_key else "ok"
    print(f"  PASS  {label}  ->  {expect_key}={val!r}" if expect_key else f"  PASS  {label}")
    return True


def main():
    print("=" * 60)
    print("marrow server smoke-test")
    print("=" * 60)

    # Start server in background thread
    t = threading.Thread(target=_start_server, daemon=True)
    t.start()

    print("\nWaiting for server to start...")
    if not _wait_for_server():
        print("FAIL  Server did not start within 15 seconds.")
        sys.exit(1)
    print("Server is up.\n")

    results = []

    # ── Identity ──────────────────────────────────────────────────────────────
    print("[ Identity ]")
    results.append(check("/v1/me", get("/v1/me"), "id", "local-user"))

    # ── Conversations ─────────────────────────────────────────────────────────
    print("\n[ Conversations ]")
    r = get("/v1/conversations")
    results.append(check("/v1/conversations", r if isinstance(r, dict) else {"conversations": r},
                         "conversations" if isinstance(r, dict) else None))

    # ── Memories ──────────────────────────────────────────────────────────────
    print("\n[ Memories ]")
    results.append(check("GET /v1/memories", get("/v1/memories")))
    m = post("/v1/memories", {"content": "test memory from smoke-test", "structured": {}})
    results.append(check("POST /v1/memories", m))

    # ── Config / API keys ─────────────────────────────────────────────────────
    print("\n[ Config ]")
    keys = get("/v1/config/api-keys")
    results.append(check("/v1/config/api-keys", keys))
    anthropic_configured = bool(keys.get("anthropicApiKey"))
    openai_configured = bool(keys.get("openaiApiKey") or os.environ.get("OPENAI_API_KEY"))
    if anthropic_configured:
        print("        ANTHROPIC_API_KEY is set — will test live LLM call")
    elif openai_configured:
        print("        OPENAI_API_KEY is set — will test live LLM call")
    else:
        print("        No LLM key set — chat will use marrow brain fallback")

    # ── Subscription stub ─────────────────────────────────────────────────────
    print("\n[ Subscription ]")
    results.append(check("/v1/users/me/subscription",
                         get("/v1/users/me/subscription"), "is_active", True))

    # ── Chat ──────────────────────────────────────────────────────────────────
    print("\n[ Chat ]")
    chat_r = post("/v1/chat/messages", {"text": "say the word PONG and nothing else"})
    results.append(check("POST /v1/chat/messages returns text",
                         chat_r, "text"))
    if not chat_r.get("_error"):
        reply = chat_r.get("text", "")
        print(f"        LLM reply: {reply[:120]!r}")
        if "PONG" in reply.upper():
            print("        Chat is using a real LLM (got expected word)")

    # ── Appcast ───────────────────────────────────────────────────────────────
    print("\n[ Sparkle appcast ]")
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8888/v2/desktop/appcast.xml?platform=macos", timeout=5
        ) as r:
            body = r.read().decode()
            results.append(check("/v2/desktop/appcast.xml", {"ok": True}))
    except Exception as e:
        results.append(check("/v2/desktop/appcast.xml", {"_error": str(e)}))

    # ── Catch-all ─────────────────────────────────────────────────────────────
    print("\n[ Catch-all / unknown endpoint ]")
    results.append(check("/v99/whatever", get("/v99/whatever"), "status", "ok"))

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Result: {passed}/{total} checks passed")
    if passed == total:
        print("All good — server works.")
    else:
        print("Some checks failed — see FAIL lines above.")
    print("=" * 60)

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
