"""
Browser automation using Browser-Use library.

Browser-Use provides:
- AI-powered element detection and interaction
- Accessibility tree parsing for smart element selection
- Built-in state management and history
- Support for multiple LLM providers (Anthropic, OpenAI, Google, etc.)

Local mode: runs headless Chromium on the machine (free)
Cloud mode: uses Browser Use's cloud (paid, more stealthy)
"""

import asyncio
import logging
import os
from typing import Optional
from pathlib import Path

import config

log = logging.getLogger(__name__)

_agent = None
_initialized = False


async def _get_agent():
    """Get or create Browser-Use agent."""
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

        _agent = Agent(
            llm=llm,
            browser=browser,
            initial_actions=[],
        )
        _initialized = True
        log.info("Browser-Use agent initialized")
        return _agent
    except Exception as e:
        log.error(f"Failed to initialize browser-use: {e}")
        return None


async def browser_navigate(url: str) -> str:
    """Navigate to a URL."""
    try:
        from browser_use import Agent, Browser
        from browser_use.browser import BrowserConfig

        browser = Browser(
            browser_config=BrowserConfig(headless=True),
        )

        llm = config.get_browser_llm()
        agent = Agent(llm=llm, browser=browser)

        history = await agent.run(f"Navigate to {url}")

        return f"Navigated to {url}. Actions taken: {len(history)}"
    except Exception as e:
        return f"[error] {e}"


async def browser_goto(url: str) -> str:
    """Go to a URL and wait for page to load."""
    try:
        agent = await _get_agent()
        if agent is None:
            return "[error] Browser not initialized"

        history = await agent.run(f"Navigate to {url} and wait for it to fully load")
        return f"Done. {len(history)} steps taken."
    except Exception as e:
        return f"[error] {e}"


async def browser_snapshot() -> str:
    """Get current page state."""
    try:
        agent = await _get_agent()
        if agent is None:
            return "[error] Browser not initialized"

        state = agent.browser.context.get_current_page_state()
        return str(state)[:4000]
    except Exception as e:
        return f"[error] {e}"


async def browser_click(selector: str) -> str:
    """Click an element."""
    try:
        agent = await _get_agent()
        if agent is None:
            return "[error] Browser not initialized"

        history = await agent.run(f"Click on the element that matches: {selector}")
        return f"Clicked. {len(history)} steps taken."
    except Exception as e:
        return f"[error] {e}"


async def browser_type(selector: str, text: str) -> str:
    """Type into an element."""
    try:
        agent = await _get_agent()
        if agent is None:
            return "[error] Browser not initialized"

        history = await agent.run(
            f"Find input element matching '{selector}' and type: {text}"
        )
        return f"Typed. {len(history)} steps taken."
    except Exception as e:
        return f"[error] {e}"


async def browser_search(query: str) -> str:
    """Search the web using the browser."""
    try:
        agent = await _get_agent()
        if agent is None:
            return "[error] Browser not initialized"

        history = await agent.run(f"Search for: {query}")
        return f"Searched for '{query}'. {len(history)} actions taken."
    except Exception as e:
        return f"[error] {e}"


async def browser_screenshot() -> str:
    """Take a screenshot."""
    try:
        agent = await _get_agent()
        if agent is None:
            return "[error] Browser not initialized"

        # Get screenshot via CDP
        return "[screenshot available via browser UI]"
    except Exception as e:
        return f"[error] {e}"


async def browser_close() -> str:
    """Close the browser."""
    global _agent, _initialized
    try:
        if _agent and _agent.browser:
            await _agent.browser.close()
        _agent = None
        _initialized = False
        return "Browser closed"
    except Exception as e:
        return f"[error] {e}"
