"""Stateful browser automation using Browser-Use."""

from __future__ import annotations

import logging
import time
from typing import Any

import config

log = logging.getLogger(__name__)

_agent = None
_initialized = False
_state: dict[str, Any] = {
    "started_at": 0.0,
    "current_url": "",
    "tabs": [],
    "history": [],
    "active_tab_index": 0,
}


def _record(action: str, *, url: str = "", note: str = "") -> None:
    if url:
        _state["current_url"] = url
    _state["history"].append(
        {
            "ts": time.time(),
            "action": action,
            "url": url or _state.get("current_url", ""),
            "note": note[:180],
        }
    )
    _state["history"] = _state["history"][-20:]


def _sync_tab(url: str, note: str = "") -> None:
    if not url:
        return
    tabs = _state.setdefault("tabs", [])
    idx = int(_state.get("active_tab_index", 0) or 0)
    if not tabs:
        tabs.append({"url": url, "note": note[:120], "last_seen": time.time()})
        _state["active_tab_index"] = 0
        return
    idx = max(0, min(idx, len(tabs) - 1))
    tabs[idx] = {"url": url, "note": note[:120], "last_seen": time.time()}


async def _get_agent():
    global _agent, _initialized

    if _initialized and _agent is not None:
        return _agent

    try:
        from browser_use import Agent, Browser
        from browser_use.browser import BrowserConfig
    except ImportError:
        log.warning("browser-use not installed")
        return None

    try:
        browser = Browser(
            browser_config=BrowserConfig(
                headless=True,
                disable_security=False,
            ),
        )
        llm = config.get_browser_llm()
        _agent = Agent(llm=llm, browser=browser, initial_actions=[])
        _initialized = True
        _state["started_at"] = time.time()
        log.info("Browser-Use agent initialized")
        return _agent
    except Exception as e:
        log.error(f"Failed to initialize browser-use: {e}")
        return None


async def _run(task: str, *, url: str = "", note: str = "") -> str:
    agent = await _get_agent()
    if agent is None:
        return "[error] Browser not initialized"
    try:
        history = await agent.run(task)
        if url:
            _sync_tab(url, note=note or task)
        _record(task, url=url, note=note)
        return f"Done. {len(history)} steps taken."
    except Exception as e:
        return f"[error] {e}"


async def browser_navigate(url: str) -> str:
    result = await _run(f"Navigate to {url} and wait for it to fully load", url=url)
    return f"Navigated to {url}. {result}"


async def browser_goto(url: str) -> str:
    return await browser_navigate(url)


async def browser_open_tab(url: str) -> str:
    tabs = _state.setdefault("tabs", [])
    tabs.append({"url": url, "note": "new tab", "last_seen": time.time()})
    _state["active_tab_index"] = len(tabs) - 1
    return await _run(f"Open a new browser tab to {url} and wait for it to load", url=url, note="open_tab")


async def browser_switch_tab(index: int) -> str:
    tabs = _state.setdefault("tabs", [])
    if not tabs:
        return "[error] No tracked browser tabs"
    idx = max(0, min(int(index), len(tabs) - 1))
    tab = tabs[idx]
    _state["active_tab_index"] = idx
    url = str(tab.get("url", "") or "")
    if url:
        return await _run(
            f"Switch to the browser tab for {url}. If needed, navigate back to that page.",
            url=url,
            note=f"switch_tab:{idx}",
        )
    return f"Switched tracked tab index to {idx}"


async def browser_list_tabs() -> str:
    tabs = _state.get("tabs", [])
    if not tabs:
        return "No tracked browser tabs."
    lines = ["## Browser Tabs"]
    active = int(_state.get("active_tab_index", 0) or 0)
    for idx, tab in enumerate(tabs):
        marker = "*" if idx == active else "-"
        lines.append(f"{marker} {idx}: {str(tab.get('url', '') or '')[:180]}")
    return "\n".join(lines)


async def browser_session_state() -> str:
    lines = ["## Browser Session"]
    current = str(_state.get("current_url", "") or "").strip()
    if current:
        lines.append(f"Current URL: {current[:220]}")
    tabs = _state.get("tabs", [])
    lines.append(f"Tracked tabs: {len(tabs)}")
    history = _state.get("history", [])
    if history:
        lines.append("Recent actions:")
        for row in history[-5:]:
            lines.append(f"- {row.get('action', '')[:80]} | {str(row.get('url', '') or '')[:120]}")
    return "\n".join(lines)


async def browser_snapshot() -> str:
    agent = await _get_agent()
    if agent is None:
        return "[error] Browser not initialized"
    try:
        state = agent.browser.context.get_current_page_state()
        session = await browser_session_state()
        text = str(state)[:2600]
        return f"{session}\n\n## Current Page State\n{text}"
    except Exception as e:
        return f"[error] {e}"


async def browser_click(selector: str) -> str:
    return await _run(f"Click on the element that matches: {selector}", note=f"click:{selector}")


async def browser_type(selector: str, text: str) -> str:
    return await _run(
        f"Find input element matching '{selector}' and type: {text}",
        note=f"type:{selector}",
    )


async def browser_search(query: str) -> str:
    return await _run(f"Search for: {query}", note=f"search:{query}")


async def browser_screenshot() -> str:
    try:
        return await browser_snapshot()
    except Exception as e:
        return f"[error] {e}"


async def browser_close() -> str:
    global _agent, _initialized
    try:
        if _agent and _agent.browser:
            await _agent.browser.close()
        _agent = None
        _initialized = False
        _state["current_url"] = ""
        _state["tabs"] = []
        _state["history"] = []
        _state["active_tab_index"] = 0
        return "Browser closed"
    except Exception as e:
        return f"[error] {e}"
