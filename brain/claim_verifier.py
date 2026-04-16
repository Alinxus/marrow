"""
Real-time claim verification pipeline.

When screen OCR or audio transcripts contain a verifiable factual claim,
this module extracts it, searches the web for evidence, and stores a
structured verdict so the reasoning loop can surface it immediately.

Pipeline:
  1. screen OCR / transcript → _extract_claims() via LLM
  2. Each new claim → _verify_claim() via web search + LLM synthesis
  3. Verified claim stored in DB as claim_event with sources
  4. build_high_signal_context() in context_awareness picks it up
  5. Reasoning loop surfaces verdict to user with sources

Designed to run as a lightweight background task every capture cycle.
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from storage import db

log = logging.getLogger(__name__)

# Don't re-verify the same claim within this window
_CLAIM_DEDUP_WINDOW = 3600  # 1 hour
_seen_claims: dict[str, float] = {}  # claim_hash → timestamp

# Queue of unverified claims (populated by detect_claims_from_context)
_pending_claims: asyncio.Queue = asyncio.Queue(maxsize=20)


@dataclass
class Claim:
    text: str          # The exact claim as stated
    topic: str         # One-word topic label (e.g. "epstein", "vaccines")
    source: str        # "screen" | "audio"
    confidence: float  # How confident we are this is a verifiable claim


@dataclass
class VerificationResult:
    claim: str
    verdict: str          # "false" | "true" | "misleading" | "unverified"
    explanation: str      # 1-3 sentence explanation
    sources: list[str]    # List of URLs
    confidence: float


_CLAIM_EXTRACT_PROMPT = """\
You are analyzing text from a user's screen or audio transcript.
Extract any factual claims that can be independently verified on the internet.

A verifiable claim:
- States something as a fact that could be true or false
- Is specific enough to search for (not vague opinions)
- Would be surprising or notable if false

Examples:
- "Epstein is alive" → verifiable claim
- "the moon landing was faked" → verifiable claim
- "I like pizza" → NOT a claim
- "the weather looks nice" → NOT a claim

Return JSON array (empty if no claims):
[{"text": "the exact claim", "topic": "one-word-topic", "confidence": 0.0-1.0}]

Text to analyze:
{text}"""


_VERIFY_PROMPT = """\
You are a fact-checker. A claim has been made and web search results are provided.

Claim: {claim}

Web search results:
{search_results}

Analyze the claim against the evidence. Return JSON:
{{
  "verdict": "false|true|misleading|unverified",
  "explanation": "1-3 sentences stating the facts clearly and citing which sources confirm/deny",
  "confidence": 0.0-1.0
}}

Be direct. If evidence is clear, say so. If evidence is thin, say "unverified"."""


async def detect_claims_from_context(
    ocr_text: str,
    transcript_text: str,
    source: str = "screen",
) -> None:
    """
    Extract verifiable claims from screen OCR or audio transcript.
    Queues new (unseen) claims for async verification.
    Called from the capture pipeline every cycle.
    """
    if not ocr_text and not transcript_text:
        return

    combined = f"{transcript_text}\n{ocr_text}".strip()
    if len(combined) < 20:
        return

    try:
        from brain.llm import get_client

        llm = get_client()
        if llm.provider == "none":
            return

        response = await llm.create(
            messages=[
                {
                    "role": "user",
                    "content": _CLAIM_EXTRACT_PROMPT.format(text=combined[:1200]),
                }
            ],
            max_tokens=200,
            model_type="scoring",
        )
        raw = response.text.strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return

        claims_data = json.loads(raw[start:end])
        for cd in claims_data:
            text = (cd.get("text") or "").strip()
            topic = (cd.get("topic") or "general").strip().lower()
            conf = float(cd.get("confidence", 0.5))

            if not text or conf < 0.5:
                continue

            # Dedup: skip if we've seen this claim recently
            claim_hash = hashlib.md5(text.lower().encode(), usedforsecurity=False).hexdigest()
            last_seen = _seen_claims.get(claim_hash, 0)
            if time.time() - last_seen < _CLAIM_DEDUP_WINDOW:
                continue

            _seen_claims[claim_hash] = time.time()
            claim = Claim(text=text, topic=topic, source=source, confidence=conf)

            try:
                _pending_claims.put_nowait(claim)
                log.info(f"Claim queued for verification: {text[:80]}")
            except asyncio.QueueFull:
                pass  # Drop if backlogged

    except Exception as e:
        log.debug(f"Claim extraction error: {e}")


async def _verify_claim(claim: Claim) -> Optional[VerificationResult]:
    """
    Verify a single claim via web search + LLM synthesis.
    Returns a VerificationResult or None on failure.
    """
    try:
        from actions.web import web_search

        query = f'"{claim.text}" fact check site:reuters.com OR site:snopes.com OR site:apnews.com OR site:bbc.com OR site:nytimes.com'
        search_results = await web_search(query, limit=5)

        if not search_results or "[error" in search_results.lower():
            # Fallback: plain search
            search_results = await web_search(f"fact check: {claim.text}", limit=5)

        from brain.llm import get_client

        llm = get_client()
        response = await llm.create(
            messages=[
                {
                    "role": "user",
                    "content": _VERIFY_PROMPT.format(
                        claim=claim.text,
                        search_results=search_results[:2000],
                    ),
                }
            ],
            max_tokens=250,
            model_type="scoring",
        )
        raw = response.text.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return None

        data = json.loads(raw[start:end])
        verdict = data.get("verdict", "unverified")
        explanation = data.get("explanation", "")
        confidence = float(data.get("confidence", 0.5))

        # Extract source URLs from search results
        sources = []
        for line in search_results.splitlines():
            line = line.strip()
            if line.startswith("http") or "://" in line:
                url = line.split()[0].rstrip(".,)")
                if url not in sources:
                    sources.append(url)
            if len(sources) >= 3:
                break

        return VerificationResult(
            claim=claim.text,
            verdict=verdict,
            explanation=explanation,
            sources=sources,
            confidence=confidence,
        )

    except Exception as e:
        log.debug(f"Claim verification error: {e}")
        return None


async def claim_verification_loop() -> None:
    """
    Background task: drains _pending_claims queue and verifies each one.
    Stores results in DB for reasoning loop to pick up.
    Runs forever alongside the main reasoning loop.
    """
    log.info("Claim verification loop started")
    while True:
        try:
            # Wait for a pending claim (with timeout to allow shutdown)
            try:
                claim = await asyncio.wait_for(_pending_claims.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

            log.info(f"Verifying claim: {claim.text[:80]}")
            result = await _verify_claim(claim)

            if result:
                verdict_label = {
                    "false": "FALSE",
                    "true": "CONFIRMED",
                    "misleading": "MISLEADING",
                    "unverified": "UNVERIFIED",
                }.get(result.verdict, result.verdict.upper())

                sources_str = " | ".join(result.sources[:3]) if result.sources else "no sources"
                verdict_text = f"[{verdict_label}] {result.explanation} Sources: {sources_str}"

                db.insert_claim_event(
                    ts=time.time(),
                    topic=claim.topic,
                    claim=claim.text,
                    verdict=verdict_text,
                    source_app=claim.source,
                    evidence=claim.text,
                    confidence=result.confidence,
                )
                log.info(f"Claim verified [{verdict_label}]: {claim.text[:60]}")

                # Always emit rich claim card signal for UI
                try:
                    from ui.bridge import get_bridge

                    payload = json.dumps({
                        "claim": claim.text,
                        "verdict": result.verdict,
                        "explanation": result.explanation,
                        "sources": result.sources[:3],
                        "confidence": result.confidence,
                    })
                    get_bridge().claim_verified.emit(payload)
                except Exception:
                    pass

                # Also emit toast for false/misleading as secondary alert
                if result.verdict in ("false", "misleading") and result.confidence >= 0.65:
                    try:
                        from ui.bridge import get_bridge

                        name = getattr(config, "MARROW_NAME", "Marrow")
                        short = f"[{verdict_label}] {claim.text[:60]} — {result.explanation[:100]}"
                        get_bridge().toast_requested.emit(name, short, 2)
                    except Exception:
                        pass

        except Exception as e:
            log.debug(f"Claim verification loop error: {e}")
            await asyncio.sleep(2)
