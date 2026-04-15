"""
Web search and content extraction.

Priority chain:
  1. Firecrawl v2 — best quality, needs FIRECRAWL_API_KEY
  2. DuckDuckGo   — free, no key, works for most queries
  3. httpx raw    — last-resort URL extraction (strips HTML)

Firecrawl v2 API (firecrawl-py >= 4.x):
  - Class:   Firecrawl(api_key=...)
  - Search:  client.search(query, limit=N)  → SearchData with .web list
  - Scrape:  client.scrape(url, formats=["markdown"])  → Document
  - Crawl:   client.crawl(url, ...)  → CrawlResponse
"""

import html as html_mod
import logging
import os
import re
import urllib.parse
from typing import Optional

log = logging.getLogger(__name__)

_firecrawl_client = None


# ─── Firecrawl v2 client ─────────────────────────────────────────────────────

def _get_firecrawl():
    global _firecrawl_client
    if _firecrawl_client is not None:
        return _firecrawl_client

    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        return None

    try:
        from firecrawl import Firecrawl
        _firecrawl_client = Firecrawl(api_key=api_key)
        return _firecrawl_client
    except Exception as e:
        log.warning(f"Firecrawl init error: {e}")
        return None


def _format_firecrawl_search(data) -> str:
    """Format Firecrawl v2 SearchData into plain text."""
    lines = []
    # SearchData has .web, .news, .images (each is a list of SearchResultWeb / Document)
    items = []
    try:
        if hasattr(data, "web") and data.web:
            items.extend(data.web)
        if hasattr(data, "news") and data.news:
            items.extend(data.news)
    except Exception:
        pass

    # Also handle legacy dict format
    if isinstance(data, dict):
        raw = data.get("data") or data.get("results") or []
        if isinstance(raw, list):
            items = raw

    for item in items[:8]:
        # v2 objects have attributes; legacy dicts have keys
        if hasattr(item, "url"):
            url     = getattr(item, "url", "") or ""
            title   = getattr(item, "title", "") or ""
            snippet = getattr(item, "description", "") or getattr(item, "markdown", "") or ""
        elif isinstance(item, dict):
            url     = item.get("url", "")
            title   = item.get("title", "")
            snippet = item.get("description") or item.get("markdown", "")
        else:
            continue
        lines.append(f"- {title}\n  {url}\n  {str(snippet)[:200]}")

    return "\n\n".join(lines) if lines else ""


# ─── DuckDuckGo fallback (zero API key needed) ───────────────────────────────

async def _ddg_search(query: str, limit: int = 5) -> str:
    """POST to DuckDuckGo HTML search, scrape results."""
    try:
        import httpx

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers=headers,
            )
            body = resp.text

        def _clean(s: str) -> str:
            s = re.sub(r'<[^>]+>', '', s)
            return html_mod.unescape(s).strip()

        links    = re.findall(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            body, re.DOTALL,
        )
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|span)>',
            body, re.DOTALL,
        )

        results = []
        for i, (href, title_raw) in enumerate(links[:limit]):
            m = re.search(r'uddg=([^&"]+)', href)
            url = urllib.parse.unquote(m.group(1)) if m else href
            snippet = _clean(snippets[i]) if i < len(snippets) else ""
            results.append(f"- {_clean(title_raw)}\n  {url}\n  {snippet[:200]}")

        return "\n\n".join(results) if results else f"No results for '{query}'"

    except Exception as e:
        log.warning(f"DuckDuckGo search error: {e}")
        return f"[search unavailable: {e}]"


# ─── Raw URL fetch ────────────────────────────────────────────────────────────

async def _fetch_url(url: str, max_chars: int = 6000) -> str:
    """Fetch a URL and return stripped plain text via httpx."""
    try:
        import httpx

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=headers)
            raw  = resp.text

        raw  = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', raw,
                      flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', raw)
        text = html_mod.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]

    except Exception as e:
        return f"[fetch error: {e}]"


# ─── Public API ───────────────────────────────────────────────────────────────

async def web_search(query: str, limit: int = 5) -> str:
    """
    Search the web. Firecrawl first, DuckDuckGo fallback.
    Always returns something useful.
    """
    client = _get_firecrawl()

    if client:
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: client.search(query, limit=limit)
            )
            formatted = _format_firecrawl_search(result)
            if formatted:
                return formatted
            log.warning("Firecrawl search returned empty — trying DuckDuckGo")
        except Exception as e:
            log.warning(f"Firecrawl search failed ({e}) — trying DuckDuckGo")

    return await _ddg_search(query, limit)


async def web_extract(url: str, prompt: str = "Extract all text content") -> str:
    """
    Extract content from a URL. Firecrawl scrape first, raw httpx fallback.
    """
    client = _get_firecrawl()

    if client:
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: client.scrape(url, formats=["markdown"])   # v2 method + param
            )
            # v2 returns a Document with .markdown attribute
            if hasattr(result, "markdown") and result.markdown:
                return result.markdown[:8000]
            if isinstance(result, dict):
                content = result.get("markdown") or result.get("content", "")
                if content:
                    return content[:8000]
        except Exception as e:
            log.warning(f"Firecrawl scrape failed ({e}) — using httpx")

    return await _fetch_url(url)


async def web_crawl(url: str, instruction: str = "Get all visible text") -> str:
    """
    Crawl a site. Firecrawl crawl first, single-page fallback.
    """
    client = _get_firecrawl()

    if client:
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: client.crawl(url, limit=5)
            )
            pages = []
            # v2 CrawlResponse has .data list
            items = getattr(result, "data", None) or (
                result.get("data", []) if isinstance(result, dict) else []
            )
            for page in items[:3]:
                pg_url = getattr(page, "url", "") or page.get("url", "") if isinstance(page, dict) else ""
                pg_md  = (
                    getattr(page, "markdown", "")
                    or (page.get("markdown", "") if isinstance(page, dict) else "")
                )[:2000]
                pages.append(f"[{pg_url}]\n{pg_md}")
            if pages:
                return "\n\n---\n\n".join(pages)
        except Exception as e:
            log.warning(f"Firecrawl crawl failed ({e}) — using httpx")

    return await _fetch_url(url)
