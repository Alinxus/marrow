"""
Action executor.

When the reasoning loop identifies something to DO (not just say),
this module runs Claude with action tools to complete the task.

Tools available to Claude:
  - run_command   : shell execution (PowerShell on Windows)
  - read_file     : read any file
  - write_file    : write/create file
  - append_file   : append to existing file
  - delete_file   : delete a file
  - list_files    : list files in directory
  - search_files  : search for text in files
  - web_search    : search the web
  - web_extract   : extract content from URL
  - web_crawl     : crawl a website
  - browser_navigate : navigate browser to URL
  - browser_click : click element in browser
  - browser_type  : type into element
  - browser_search : search via browser
  - clipboard_read : read clipboard
  - clipboard_write : write to clipboard
  - process_list  : list processes
  - process_kill  : kill process
  - window_list    : list windows
  - window_focus   : focus window
  - system_info   : get system stats
  - take_screenshot : capture screen
  - surface_to_user : show result to user

Ported from Hermes:
  - terminal_tool, file_tools, browser_tool, web_tools
  - Added Windows-specific: clipboard, process, window management
"""

import asyncio
import json
import logging
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

import config
from personality.marrow import ACTION_SYSTEM_PROMPT
from storage import db

log = logging.getLogger(__name__)

SURFACE_FILE = Path.home() / ".marrow" / "surface.json"
MAX_ITERATIONS = config.MAX_ACTION_ITERATIONS


# ─── Tool definitions ──────────────────────────────────────────────────────────

MARROW_TOOLS = [
    {
        "name": "run_command",
        "description": (
            "Run a shell command. Uses bash on macOS/Linux, PowerShell on Windows. "
            "Use for: file operations, launching apps, email/calendar via CLI or AppleScript (macOS) "
            "or Outlook COM (Windows), git operations, any terminal task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run (bash on macOS/Linux, PowerShell on Windows)",
                },
                "explanation": {
                    "type": "string",
                    "description": "One-line description of what this does",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
            },
            "required": ["command", "explanation"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from disk. Returns up to 4000 characters. Use offset/limit for large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path or ~ path"},
                "offset": {
                    "type": "integer",
                    "description": "Character offset to start reading from",
                },
                "limit": {"type": "integer", "description": "Max characters to return"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file (creates or overwrites).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "offset": {
                    "type": "integer",
                    "description": "Offset for partial overwrite",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "append_file",
        "description": "Append content to an existing file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List files in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"},
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (default: *)",
                },
                "recursive": {"type": "boolean", "description": "Search recursively"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for text in files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to search"},
                "query": {"type": "string", "description": "Text to search for"},
                "extensions": {
                    "type": "string",
                    "description": "File extensions to search (e.g., .py,.js)",
                },
            },
            "required": ["path", "query"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web using Firecrawl.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_extract",
        "description": "Extract content from a specific URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to extract from"},
                "prompt": {"type": "string", "description": "What to extract"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "web_crawl",
        "description": "Crawl a website with custom instructions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to crawl"},
                "instruction": {"type": "string", "description": "What to look for"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_navigate",
        "description": "Navigate browser to a URL using Browser-Use.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_click",
        "description": "Click an element in the browser.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Element selector"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_type",
        "description": "Type text into an element in the browser.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Element selector"},
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "browser_search",
        "description": "Search the web using the browser.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "browser_open_tab",
        "description": "Open a new tracked browser tab to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to open in a new tab"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_switch_tab",
        "description": "Switch to a tracked browser tab by index.",
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "Tracked tab index"}},
            "required": ["index"],
        },
    },
    {
        "name": "browser_list_tabs",
        "description": "List tracked browser tabs in the persistent browser session.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_session_state",
        "description": "Show current browser session state, tracked tabs, and recent actions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "clipboard_read",
        "description": "Read the system clipboard.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "clipboard_write",
        "description": "Write text to the system clipboard.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "process_list",
        "description": "List running processes.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "process_kill",
        "description": "Kill a process by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "system_info",
        "description": "Get system information (CPU, memory, disk, battery).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "take_screenshot",
        "description": "Take a screenshot of the current screen.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "surface_to_user",
        "description": (
            "Show a result, draft, or proposed action to the user. "
            "Use for: email drafts, documents to review, decisions that need approval. "
            "Writes to a file the UI layer watches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What is this?"},
                "content": {"type": "string", "description": "The draft or result"},
                "action_label": {
                    "type": "string",
                    "description": "What the confirm action does",
                },
                "requires_approval": {
                    "type": "boolean",
                    "description": "Whether user must approve",
                },
            },
            "required": ["title", "content"],
        },
    },
    # Window management
    {
        "name": "window_list",
        "description": "List all open windows",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "window_focus",
        "description": "Focus a window by title (partial match)",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
    {
        "name": "window_focus_verified",
        "description": "Focus a window by title (partial match) and verify target presence.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
    {
        "name": "window_move",
        "description": "Move window to position",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
            "required": ["title", "x", "y"],
        },
    },
    {
        "name": "window_resize",
        "description": "Resize window",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            "required": ["title", "width", "height"],
        },
    },
    {
        "name": "window_minimize",
        "description": "Minimize window",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
    {
        "name": "window_maximize",
        "description": "Maximize window",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
    {
        "name": "window_close",
        "description": "Close window",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
    # Application control
    {
        "name": "app_launch",
        "description": (
            "Launch any application, folder, file, or URL on the user's computer. "
            "Pass the app name exactly as the user said it — e.g. 'file explorer', 'chrome', "
            "'notepad', 'spotify', 'discord', 'vs code', 'calculator', 'terminal'. "
            "For URLs pass the full URL. For files pass the full path. "
            "Always prefer this tool over run_command for opening apps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "App name ('chrome', 'file explorer', 'notepad'), full exe path, file path, or URL."
                },
                "arguments": {"type": "string"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "app_launch_verified",
        "description": "Launch an application and verify it started. Use for important launches where confirmation matters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "App name, exe path, or URL."},
                "arguments": {"type": "string"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "app_close",
        "description": "Close an application by name",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    # Smart home / device bridge
    {
        "name": "smart_home_call",
        "description": "Call Home Assistant service (domain.service), e.g. light.turn_on",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "entity_id": {"type": "string"},
                "payload_json": {
                    "type": "string",
                    "description": "Optional JSON object string for service payload",
                },
            },
            "required": ["service"],
        },
    },
    {
        "name": "set_system_volume",
        "description": "Set local system output volume percentage (0-100).",
        "input_schema": {
            "type": "object",
            "properties": {"percent": {"type": "integer"}},
            "required": ["percent"],
        },
    },
    # Mouse control
    {
        "name": "mouse_move",
        "description": "Move mouse to position",
        "input_schema": {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        },
    },
    {
        "name": "mouse_click",
        "description": "Click at position",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string"},
            },
        },
    },
    # Keyboard control
    {
        "name": "keyboard_type",
        "description": "Type text",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "keyboard_hotkey",
        "description": "Press hotkey (e.g., Ctrl+C, Alt+Tab)",
        "input_schema": {
            "type": "object",
            "properties": {"keys": {"type": "string"}},
            "required": ["keys"],
        },
    },
    # Clipboard
    {
        "name": "clipboard_get",
        "description": "Get clipboard content",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "clipboard_set",
        "description": "Set clipboard content",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    # Screenshot
    {
        "name": "screenshot",
        "description": "Take full screenshot",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "screenshot_region",
        "description": "Take screenshot of region",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            "required": ["x", "y", "width", "height"],
        },
    },
    # Subagent delegation
    {
        "name": "delegate_task",
        "description": "Break a complex task into parallel sub-tasks using subagents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task to delegate"},
                "subagent_type": {
                    "type": "string",
                    "description": "Type: research, file_ops, code, general, quick",
                },
                "max_subagents": {
                    "type": "integer",
                    "description": "Max subagents (default 3)",
                },
            },
            "required": ["task"],
        },
    },
    # Memory (RetainDB)
    {
        "name": "memory_add",
        "description": "Store a fact or preference in persistent memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "What to remember"},
                "memory_type": {
                    "type": "string",
                    "description": "Type: factual, preference, instruction, event",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_search",
        "description": "Search stored memories for relevant context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_get_profile",
        "description": "Get all stored memories about the user.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "memory_store_file",
        "description": "Store a file in RetainDB - extracts text and creates searchable memories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to file to store"},
                "scope": {
                    "type": "string",
                    "description": "Scope: USER, PROJECT, ORG, AGENT",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "memory_list_files",
        "description": "List files stored in RetainDB.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "Filter by path prefix"},
                "scope": {"type": "string", "description": "Scope: USER, PROJECT, ORG"},
            },
        },
    },
    # Todo/Task tracking
    {
        "name": "todo_add",
        "description": "Add a new task or todo item.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "description": {"type": "string", "description": "Task details"},
                "due": {"type": "string", "description": "Due date (ISO format)"},
                "priority": {
                    "type": "integer",
                    "description": "Priority 1-4 (1=highest)",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "todo_list",
        "description": "List pending tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "pending, completed, or all",
                },
                "limit": {"type": "integer", "description": "Max tasks to show"},
            },
        },
    },
    {
        "name": "todo_complete",
        "description": "Mark a task as completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "todo_id": {"type": "integer", "description": "Task ID"},
            },
            "required": ["todo_id"],
        },
    },
    {
        "name": "todo_delete",
        "description": "Delete a task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "todo_id": {"type": "integer", "description": "Task ID"},
            },
            "required": ["todo_id"],
        },
    },
    # Reminders
    {
        "name": "reminder_add",
        "description": "Schedule a reminder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Reminder message"},
                "seconds": {"type": "integer", "description": "Seconds until reminder"},
            },
            "required": ["message", "seconds"],
        },
    },
    {
        "name": "reminder_list",
        "description": "List pending reminders.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # Scheduler
    {
        "name": "schedule_interval",
        "description": "Schedule a recurring task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Unique job name"},
                "seconds": {"type": "integer", "description": "Run every N seconds"},
                "minutes": {"type": "integer", "description": "Run every N minutes"},
                "hours": {"type": "integer", "description": "Run every N hours"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "schedule_cron",
        "description": "Schedule with cron expression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Unique job name"},
                "cron": {
                    "type": "string",
                    "description": "Cron: minute.hour.day (e.g., 30.9 for 9:30 daily)",
                },
            },
            "required": ["job_id", "cron"],
        },
    },
    {
        "name": "unschedule",
        "description": "Cancel a scheduled job.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID to cancel"},
            },
            "required": ["job_id"],
        },
    },
    # Mission mode
    {
        "name": "mission_create",
        "description": "Create a mission plan with executable steps.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Mission objective"},
                "plan_json": {
                    "type": "string",
                    "description": "Optional JSON array of steps",
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "mission_start",
        "description": "Start executing a mission by id.",
        "input_schema": {
            "type": "object",
            "properties": {"mission_id": {"type": "integer"}},
            "required": ["mission_id"],
        },
    },
    {
        "name": "mission_pause",
        "description": "Pause a running mission.",
        "input_schema": {
            "type": "object",
            "properties": {"mission_id": {"type": "integer"}},
            "required": ["mission_id"],
        },
    },
    {
        "name": "mission_resume",
        "description": "Resume a paused mission.",
        "input_schema": {
            "type": "object",
            "properties": {"mission_id": {"type": "integer"}},
            "required": ["mission_id"],
        },
    },
    {
        "name": "mission_status",
        "description": "Show mission progress and step state.",
        "input_schema": {
            "type": "object",
            "properties": {"mission_id": {"type": "integer"}},
            "required": ["mission_id"],
        },
    },
    {
        "name": "mission_list",
        "description": "List recent missions with status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "status": {"type": "string"},
            },
        },
    },
    {
        "name": "mission_rollback",
        "description": "Rollback the most recent mission steps using rollback actions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mission_id": {"type": "integer"},
                "steps": {"type": "integer"},
            },
            "required": ["mission_id"],
        },
    },
    # Code execution
    {
        "name": "execute_code",
        "description": "Run Python, JavaScript, PowerShell, or shell code in a real workspace-aware subprocess.",
        "input_schema": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "description": "python, javascript, bash, powershell",
                },
                "code": {"type": "string", "description": "Code to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds"},
                "workspace": {
                    "type": "string",
                    "description": "Optional working directory for execution",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional filename to persist the script in the workspace",
                },
                "args": {
                    "type": "string",
                    "description": "Optional command-line arguments",
                },
                "keep_file": {
                    "type": "boolean",
                    "description": "Keep temp script file after execution",
                },
            },
            "required": ["language", "code"],
        },
    },
    # Office/Documents
    {
        "name": "excel_read",
        "description": "Read Excel file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Excel file path"},
                "sheet": {"type": "string", "description": "Sheet name"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "excel_write",
        "description": "Write data to Excel file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Excel file path"},
                "data": {"type": "string", "description": "CSV data to write"},
                "sheet": {"type": "string", "description": "Sheet name"},
            },
            "required": ["path", "data"],
        },
    },
    {
        "name": "excel_append",
        "description": "Append rows to Excel file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Excel file path"},
                "data": {"type": "string", "description": "CSV data to append"},
                "sheet": {"type": "string", "description": "Sheet name"},
            },
            "required": ["path", "data"],
        },
    },
    {
        "name": "word_read",
        "description": "Read Word document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Word file path"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "word_write",
        "description": "Write to Word document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Word file path"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "pdf_read",
        "description": "Read PDF text content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "PDF file path"},
                "page": {"type": "integer", "description": "Specific page number"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "pdf_info",
        "description": "Get PDF metadata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "PDF file path"},
            },
            "required": ["path"],
        },
    },
    # Fact checking
    {
        "name": "fact_check",
        "description": (
            "Verify a factual claim against multiple web sources. "
            "Returns a verdict (true/false/misleading/unverified), explanation, and source URLs. "
            "Use whenever the user or media makes a claim that can be verified."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {
                    "type": "string",
                    "description": "The factual claim to verify",
                },
                "context": {
                    "type": "string",
                    "description": "Optional: where this claim was made (e.g. 'YouTube video', 'news article')",
                },
            },
            "required": ["claim"],
        },
    },
    # Complex task execution
    {
        "name": "execute_complex",
        "description": "Execute a complex task with planning, tool chaining, and verification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What you want to achieve"},
                "verify": {
                    "type": "boolean",
                    "description": "Verify goal was achieved",
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "plan_task",
        "description": "Create a plan for a complex task without executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What to achieve"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "bootstrap_capability",
        "description": (
            "When a required app/tool/capability is missing, bootstrap it: detect, "
            "attempt install, and scaffold local fallback workspace so execution can continue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requirement": {
                    "type": "string",
                    "description": "Capability requirement, e.g. 'obsidian-like notes'",
                },
                "install": {
                    "type": "boolean",
                    "description": "Attempt installation if possible",
                },
                "create_local_fallback": {
                    "type": "boolean",
                    "description": "Create local fallback workspace/scripts",
                },
            },
            "required": ["requirement"],
        },
    },
    {
        "name": "create_local_adapter",
        "description": (
            "Create a persistent local adapter tool for repeated workflows. "
            "Adapter is auto-registered in future runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requirement": {
                    "type": "string",
                    "description": "What capability/workflow this adapter should handle",
                },
                "adapter_name": {
                    "type": "string",
                    "description": "Stable adapter name",
                },
                "description": {
                    "type": "string",
                    "description": "What this adapter does",
                },
                "mode": {"type": "string", "description": "command or python"},
                "command_template": {
                    "type": "string",
                    "description": "PowerShell command template with {task}/{context} placeholders",
                },
                "python_script": {
                    "type": "string",
                    "description": "Full Python script text if mode=python",
                },
                "input_schema_json": {
                    "type": "string",
                    "description": "Optional JSON schema string for adapter input",
                },
            },
            "required": ["requirement", "adapter_name"],
        },
    },
    {
        "name": "list_local_adapters",
        "description": "List local adapter tools that are auto-registered.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_permissions",
        "description": "Check runtime permissions/capabilities (screen, mic, hotkey, platform-specific access).",
        "input_schema": {
            "type": "object",
            "properties": {
                "detailed": {
                    "type": "boolean",
                    "description": "Include extra guidance",
                },
            },
        },
    },
    {
        "name": "open_permission_panels",
        "description": "Open OS permission settings panels relevant to Marrow setup.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "verify_local_adapter",
        "description": "Run a smoke test for a local adapter and save pass/fail status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "adapter_name": {"type": "string", "description": "Adapter name"},
                "sample_input_json": {
                    "type": "string",
                    "description": 'JSON object string passed to adapter, e.g. {"task":"daily log"}',
                },
            },
            "required": ["adapter_name"],
        },
    },
    # Background processes
    {
        "name": "run_background",
        "description": "Run a command in background (non-blocking).",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to run"},
                "process_id": {
                    "type": "string",
                    "description": "Optional ID for this process",
                },
                "notify": {"type": "boolean", "description": "Notify when complete"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "get_background_status",
        "description": "Get status of a background process.",
        "input_schema": {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Process ID"},
            },
            "required": ["process_id"],
        },
    },
    {
        "name": "cancel_background",
        "description": "Cancel a running background process.",
        "input_schema": {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Process ID to cancel"},
            },
            "required": ["process_id"],
        },
    },
    {
        "name": "list_background",
        "description": "List all background processes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: running, completed, failed",
                },
            },
        },
    },
    # Approval
    {
        "name": "set_approval_mode",
        "description": "Set approval mode: guarded (ask) or unlocked (run everything).",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "description": "guarded or unlocked"},
            },
            "required": ["mode"],
        },
    },
    # User-facing notification
    {
        "name": "notify_user",
        "description": (
            "Show a visual toast notification to the user. "
            "Use this to surface important info, results, summaries, or alerts "
            "without requiring voice. Always use this for startup briefings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Toast title (short, e.g. 'Marrow' or 'Email Summary')",
                },
                "message": {
                    "type": "string",
                    "description": "The notification body text (max 200 chars works best)",
                },
                "urgency": {
                    "type": "integer",
                    "description": "1=critical (red), 2=high (orange), 3=medium (amber), 4=info (blue), 5=low (gray)",
                },
            },
            "required": ["title", "message"],
        },
    },
    # Email access
    {
        "name": "get_emails",
        "description": (
            "Get recent emails. Tries Outlook via PowerShell first, "
            "then falls back to run_command suggestions. "
            "Returns a summary of unread/recent messages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Look back N hours (default 24)",
                },
                "max_count": {
                    "type": "integer",
                    "description": "Max emails to return (default 10)",
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "Only unread emails (default true)",
                },
            },
        },
    },
    # Calendar access
    {
        "name": "get_calendar",
        "description": (
            "Get today's calendar events and upcoming meetings. "
            "Tries Outlook via PowerShell. Returns a list of events with times."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Days ahead to look (default 1 = today only)",
                },
            },
        },
    },
    {
        "name": "communications_brief",
        "description": "Bundle email, calendar, reminders, and pending tasks into one operator summary.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "document_task",
        "description": "High-level document workflow for reading, summarizing, creating, writing, or appending PDFs, docs, spreadsheets, and text files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "description": "read, summarize, write, create, or append"},
                "path": {"type": "string", "description": "Document path"},
                "content": {"type": "string", "description": "Content for write/create/append"},
                "sheet": {"type": "string", "description": "Optional spreadsheet sheet"},
                "page": {"type": "integer", "description": "Optional PDF page number"},
            },
            "required": ["operation", "path"],
        },
    },
    {
        "name": "browser_research",
        "description": "Run a higher-level browser or research workflow across search and extraction.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What you are trying to learn or accomplish"},
                "query": {"type": "string", "description": "Optional search query"},
                "url": {"type": "string", "description": "Optional URL to inspect"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "computer_workflow",
        "description": "Higher-level desktop/computer operator workflow for setup, installs, env/config work, and automation checks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What should be accomplished"},
                "command": {"type": "string", "description": "Optional PowerShell command to run"},
                "app_name": {"type": "string", "description": "Optional app/process to verify"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "project_workflow",
        "description": "Higher-level project operator workflow for repo status, issue/PR/CI/deploy context, and project checks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Project task or question"},
                "repo_path": {"type": "string", "description": "Optional repository path"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "personal_workflow",
        "description": "Personal operator workflow for errands, travel, shopping, subscriptions, and household coordination.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "shopping, errands, travel, household, or other"},
                "detail": {"type": "string", "description": "What should be tracked or coordinated"},
            },
            "required": ["kind"],
        },
    },
    {
        "name": "verify_workspace_state",
        "description": "Check current desktop/web world state against an expectation using the twin and recent screen context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expectation": {"type": "string", "description": "What should be true right now"},
            },
        },
    },
    {
        "name": "email_draft",
        "description": "Create a real Outlook email draft.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["recipient", "subject", "body"],
        },
    },
    {
        "name": "email_send",
        "description": "Send a real Outlook email immediately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["recipient", "subject", "body"],
        },
    },
    {
        "name": "calendar_create_event",
        "description": "Create a real Outlook calendar event.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
                "location": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["title", "start_iso", "end_iso"],
        },
    },
    {
        "name": "followup_add",
        "description": "Create a follow-up task and reminder for a contact or thread.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "topic": {"type": "string"},
                "when_seconds": {"type": "integer"},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "capture_workspace_checkpoint",
        "description": "Capture a named before/after workspace checkpoint for verification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "expectation": {"type": "string"},
            },
            "required": ["label"],
        },
    },
    {
        "name": "compare_workspace_checkpoints",
        "description": "Compare two named workspace checkpoints against an expectation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "before_label": {"type": "string"},
                "after_label": {"type": "string"},
                "expectation": {"type": "string"},
            },
            "required": ["before_label", "after_label"],
        },
    },
]


# ─── Tool handlers ─────────────────────────────────────────────────────────────


def _terminal_exec(command: str, timeout: int = 30) -> str:
    """
    Run a shell command. Returns stdout+stderr, capped at 3000 chars.
    Uses bash on macOS/Linux, PowerShell on Windows.
    """
    from actions import approval

    should_proceed, reason = approval.check_approval(
        command, "run_command", {"command": command}
    )
    if not should_proceed:
        log.warning(f"Command blocked by approval: {reason}")
        return f"[BLOCKED] {reason}"

    try:
        if platform.system() == "Windows":
            cmd_args = ["powershell", "-NoProfile", "-Command", command]
            result = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(Path.home()),
                encoding="utf-8",
                errors="replace",
            )
        else:
            result = subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(Path.home()),
                encoding="utf-8",
                errors="replace",
            )
        output = (result.stdout + result.stderr).strip()
        if len(output) > 3000:
            output = output[:3000] + "\n[... truncated]"
        return output or "[no output]"
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout}s]"
    except FileNotFoundError:
        # PowerShell not in PATH on Windows — fall back to cmd
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(Path.home()),
            )
            return (result.stdout + result.stderr).strip()[:3000]
        except Exception as e:
            return f"[error: {e}]"
    except Exception as e:
        return f"[error: {e}]"


def _web_fetch(url: str, max_chars: int = 3000) -> str:
    """Fetch a URL and return plain text content."""
    try:
        import urllib.request
        import html.parser

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Marrow/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        # Strip HTML tags with a simple parser
        class _StripHTML(html.parser.HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []

            def handle_data(self, data):
                self.parts.append(data)

        parser = _StripHTML()
        parser.feed(raw)
        text = " ".join(parser.parts)
        # Collapse whitespace
        import re

        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"[fetch error: {e}]"


def _get_emails_mac(hours: int = 24, max_count: int = 10) -> str:
    """Fetch emails on macOS via AppleScript (Mail.app) or himalaya fallback."""
    script = (
        f'tell application "Mail"\n'
        f"  set msgs to (messages of inbox whose read status is false)\n"
        f'  set result to ""\n'
        f"  set n to 0\n"
        f"  repeat with m in msgs\n"
        f"    if n >= {max_count} then exit repeat\n"
        f'    set result to result & (sender of m) & ": " & (subject of m) & "\\n"\n'
        f"    set n to n + 1\n"
        f"  end repeat\n"
        f'  if result is "" then return "No unread emails"\n'
        f"  return result\n"
        f"end tell"
    )
    result = _terminal_exec(f"osascript -e '{script}'", timeout=10)
    if (
        result
        and "[error" not in result.lower()
        and "not running" not in result.lower()
    ):
        return f"Unread emails (Mail.app):\n{result}"

    # Fallback: himalaya CLI
    himalaya = _terminal_exec(
        f"himalaya envelope list --max-width 80 --limit {max_count}", timeout=10
    )
    if (
        himalaya
        and "[error]" not in himalaya.lower()
        and "command not found" not in himalaya.lower()
    ):
        return f"Emails (himalaya):\n{himalaya}"

    return (
        "[Email access unavailable] Open Mail.app and ensure an account is configured, "
        "or install himalaya CLI (`brew install himalaya`) for terminal access."
    )


def _get_emails(hours: int = 24, max_count: int = 10, unread_only: bool = True) -> str:
    """
    Fetch recent emails via platform-native method.
    macOS: AppleScript (Mail.app) → himalaya
    Windows: PowerShell Outlook COM → himalaya
    Falls back gracefully with instructions if nothing is available.
    """
    if platform.system() == "Darwin":
        return _get_emails_mac(hours, max_count)

    # Try Outlook COM via PowerShell (works if Outlook is installed)
    unread_filter = "AND [Unread]=True" if unread_only else ""
    cutoff_date = f"AND [ReceivedTime] > '{__import__('datetime').datetime.now() - __import__('datetime').timedelta(hours=hours)}'"
    ps_script = f"""
try {{
    $outlook = New-Object -ComObject Outlook.Application
    $ns = $outlook.GetNamespace("MAPI")
    $inbox = $ns.GetDefaultFolder(6)  # 6 = olFolderInbox
    $filter = "[ReceivedTime] > '{(__import__("datetime").datetime.now() - __import__("datetime").timedelta(hours=hours)).strftime("%m/%d/%Y %H:%M")}'"
    $items = $inbox.Items.Restrict($filter)
    $items.Sort("[ReceivedTime]", $true)
    $count = 0
    $results = @()
    foreach ($item in $items) {{
        if ($count -ge {max_count}) {{ break }}
        if ({str(unread_only).lower()} -and -not $item.UnRead) {{ continue }}
        $results += "[$($item.ReceivedTime.ToString('HH:mm'))] $($item.SenderName): $($item.Subject)"
        $count++
    }}
    if ($results.Count -eq 0) {{ Write-Output "No {"unread " if unread_only else ""}emails in the last {hours}h" }}
    else {{ $results | ForEach-Object {{ Write-Output $_ }} }}
}} catch {{
    Write-Output "OUTLOOK_UNAVAILABLE: $_"
}}
""".strip()

    result = _terminal_exec(ps_script, timeout=15)

    if "OUTLOOK_UNAVAILABLE" in result or "not recognized" in result.lower():
        # Try himalaya CLI (cross-platform email client)
        himalaya = _terminal_exec(
            f"himalaya envelope list --max-width 80 --limit {max_count}",
            timeout=10,
        )
        if (
            himalaya
            and "[error]" not in himalaya.lower()
            and "not recognized" not in himalaya.lower()
        ):
            return f"Emails (himalaya):\n{himalaya}"

        # Nothing available — give instructions
        return (
            f"[Email access unavailable] No email client found.\n"
            f"To enable: install himalaya CLI (`winget install pimalaya.himalaya`) "
            f"or ensure Microsoft Outlook is installed and configured."
        )

    return f"Recent emails (last {hours}h):\n{result}" if result else "No emails found."


def _get_calendar_mac(days: int = 1) -> str:
    """Fetch calendar events on macOS via AppleScript (Calendar.app)."""
    script = (
        'tell application "Calendar"\n'
        '  set result to ""\n'
        "  repeat with c in calendars\n"
        "    set evts to (events of c whose start date >= (current date) and start date <= ((current date) + "
        f"{days * 86400}))\n"
        "    repeat with e in evts\n"
        '      set result to result & (summary of e) & " @ " & ((start date of e) as string) & "\\n"\n'
        "    end repeat\n"
        "  end repeat\n"
        '  if result is "" then return "No events"\n'
        "  return result\n"
        "end tell"
    )
    result = _terminal_exec(f"osascript << 'EOF'\n{script}\nEOF", timeout=10)
    if result and "[error" not in result.lower():
        return f"Calendar (Calendar.app):\n{result}"
    return (
        "[Calendar access unavailable] Open Calendar.app and ensure events are synced."
    )


def _get_calendar(days: int = 1) -> str:
    """
    Fetch calendar events via platform-native method.
    macOS: AppleScript (Calendar.app)
    Windows: PowerShell Outlook COM
    Falls back to instructions if nothing available.
    """
    if platform.system() == "Darwin":
        return _get_calendar_mac(days)
    end_date = (
        __import__("datetime").datetime.now()
        + __import__("datetime").timedelta(days=days)
    ).strftime("%m/%d/%Y")
    today = __import__("datetime").datetime.now().strftime("%m/%d/%Y")

    ps_script = f"""
try {{
    $outlook = New-Object -ComObject Outlook.Application
    $ns = $outlook.GetNamespace("MAPI")
    $cal = $ns.GetDefaultFolder(9)  # 9 = olFolderCalendar
    $items = $cal.Items
    $items.IncludeRecurrences = $true
    $items.Sort("[Start]")
    $filter = "[Start] >= '{today} 00:00 AM' AND [Start] <= '{end_date} 11:59 PM'"
    $restricted = $items.Restrict($filter)
    $results = @()
    foreach ($item in $restricted) {{
        $results += "$($item.Start.ToString('HH:mm'))-$($item.End.ToString('HH:mm')): $($item.Subject)"
    }}
    if ($results.Count -eq 0) {{ Write-Output "No events today" }}
    else {{ $results | ForEach-Object {{ Write-Output $_ }} }}
}} catch {{
    Write-Output "OUTLOOK_UNAVAILABLE: $_"
}}
""".strip()

    result = _terminal_exec(ps_script, timeout=15)

    if "OUTLOOK_UNAVAILABLE" in result or "not recognized" in result.lower():
        # Try reading a local .ics calendar file if it exists
        ics_check = _terminal_exec(
            "Get-ChildItem $env:USERPROFILE\\*.ics -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName",
            timeout=5,
        )
        if ics_check and ics_check.strip() and "[no output]" not in ics_check:
            return (
                f"[Calendar] Found .ics file: {ics_check.strip()}\n"
                "Outlook is not installed. Calendar events cannot be read automatically."
            )
        return (
            "[Calendar access unavailable] Outlook is not installed or not configured.\n"
            "Install Microsoft Outlook or share your calendar via a local .ics file."
        )

    return f"Today's calendar:\n{result}" if result else "No calendar events."


def _handle_tool_call(tool_name: str, tool_input: dict, context: str = "") -> str:
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _async_handle_tool_call(tool_name, tool_input, context)
        )
    finally:
        loop.close()


def _normalize_short_reply(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\s\.!?,;:]+", " ", t).strip()
    return t


def _resolve_followup_task(task: str) -> str:
    """Resolve short follow-up replies like 'yes' using recent chat context."""
    norm = _normalize_short_reply(task)
    if len(norm) > 30:
        return task

    affirmative = {
        "yes",
        "yep",
        "yeah",
        "sure",
        "ok",
        "okay",
        "do it",
        "go ahead",
        "proceed",
        "sounds good",
        "that one",
        "same",
        "same one",
        "do that",
    }
    negative = {"no", "nope", "nah", "stop", "cancel", "dont", "don't"}

    if norm not in affirmative and norm not in negative:
        return task

    recent = db.get_recent_conversations(limit=20)  # newest first
    if not recent:
        return task

    last_question = ""
    last_user_request = ""

    for row in recent:
        role = (row.get("role") or "").lower()
        content = (row.get("content") or "").strip()
        if not content:
            continue

        if role == "assistant" and not last_question:
            low = content.lower()
            if (
                "?" in content
                or "should i" in low
                or "want me to" in low
                or "shall i" in low
                or "can i" in low
            ):
                last_question = content[:260]

        if (
            role == "user"
            and _normalize_short_reply(content) not in affirmative | negative
        ):
            if len(content) > 6:
                last_user_request = content[:260]
                break

    if norm in affirmative:
        if last_question:
            return (
                "User confirmed YES to your previous question. "
                "Do not ask the same question again. "
                "Infer the intended next action and execute it now. "
                f"Previous question: {last_question}"
            )
        if last_user_request:
            return (
                "User confirmed continuation. Proceed with their previous request: "
                f"{last_user_request}"
            )
        return "User said yes. Proceed with the most recent pending action."

    # Negative
    if last_question:
        return (
            "User declined your previous question/request. "
            f"Do not execute it. Previous question: {last_question}"
        )
    return "User said no. Cancel the pending action and confirm cancellation."


def _is_history_question(task: str) -> bool:
    low = (task or "").lower().strip()
    hints = (
        "what did you observe",
        "what have you observed",
        "based on what you observed",
        "from what you observed",
        "what happened earlier",
        "what was i doing",
        "what did i do",
        "in the past",
        "earlier today",
        "last hour",
        "previously",
        "recap",
        "summarize what happened",
    )
    if any(h in low for h in hints):
        return True
    return ("what" in low or "when" in low or "who" in low) and (
        "earlier" in low or "before" in low or "past" in low
    )


def _build_observation_history_context() -> str:
    """Compact local recall context for questions about past observations."""
    lines: list[str] = ["=== OBSERVED HISTORY (local memory) ==="]

    try:
        obs = db.get_observations(limit=40)
        if obs:
            lines.append("Recent observations:")
            for o in obs[:16]:
                lines.append(
                    f"- [{o.get('type', 'obs')}] {str(o.get('content', ''))[:140]}"
                )
    except Exception:
        pass

    try:
        conv = db.get_recent_conversations(limit=24)
        if conv:
            lines.append("Recent conversation snippets:")
            for row in list(reversed(conv))[-12:]:
                role = (row.get("role") or "user")[:10]
                content = (row.get("content") or "").replace("\n", " ").strip()
                if content:
                    lines.append(f"- {role}: {content[:130]}")
    except Exception:
        pass

    try:
        ctx = db.get_recent_context(4 * 3600)
        shots = ctx.get("screenshots", [])
        if shots:
            lines.append("Recent app/window activity:")
            seen = set()
            for s in shots[:12]:
                app = (s.get("app_name") or "unknown").strip()
                title = (s.get("window_title") or "").strip()
                key = f"{app}|{title}"
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"- {app}: {title[:110]}")
    except Exception:
        pass

    return "\n".join(lines[:180])


async def _async_handle_tool_call(
    tool_name: str, tool_input: dict, context: str = ""
) -> str:
    from actions import browser, web, file_tools, system
    from actions import adapters as adapters_mod

    # Dynamic adapter tools (adapter_<name>)
    adapter_result = adapters_mod.execute_adapter_tool(
        tool_name, tool_input, _terminal_exec
    )
    if adapter_result is not None:
        return adapter_result

    if tool_name == "run_command":
        timeout = tool_input.get("timeout", 30)
        log.info(
            f"Action › {tool_input.get('explanation', tool_input['command'][:60])}"
        )
        return _terminal_exec(tool_input["command"], timeout=timeout)

    elif tool_name == "read_file":
        path = tool_input["path"].replace("~", str(Path.home()))
        offset = tool_input.get("offset", 0)
        limit = tool_input.get("limit", 4000)
        return await file_tools.file_read(path, offset=offset, limit=limit)

    elif tool_name == "write_file":
        path = tool_input["path"].replace("~", str(Path.home()))
        content = tool_input["content"]
        offset = tool_input.get("offset", 0)
        return await file_tools.file_write(path, content, offset=offset)

    elif tool_name == "append_file":
        path = tool_input["path"].replace("~", str(Path.home()))
        content = tool_input["content"]
        return await file_tools.file_append(path, content)

    elif tool_name == "delete_file":
        path = tool_input["path"].replace("~", str(Path.home()))
        return await file_tools.file_delete(path)

    elif tool_name == "list_files":
        path = tool_input.get("path", ".").replace("~", str(Path.home()))
        pattern = tool_input.get("pattern", "*")
        recursive = tool_input.get("recursive", False)
        return await file_tools.file_list(path, pattern=pattern, recursive=recursive)

    elif tool_name == "search_files":
        path = tool_input.get("path", ".").replace("~", str(Path.home()))
        query = tool_input["query"]
        extensions = tool_input.get("extensions", "")
        return await file_tools.file_search(path, query, extensions=extensions)

    elif tool_name == "web_search":
        query = tool_input["query"]
        limit = tool_input.get("limit", 5)
        return await web.web_search(query, limit=limit)

    elif tool_name == "web_extract":
        url = tool_input["url"]
        prompt = tool_input.get("prompt", "Extract all text content")
        return await web.web_extract(url, prompt=prompt)

    elif tool_name == "web_crawl":
        url = tool_input["url"]
        instruction = tool_input.get("instruction", "Get all visible text")
        return await web.web_crawl(url, instruction=instruction)

    elif tool_name == "browser_navigate":
        url = tool_input["url"]
        return await browser.browser_navigate(url)

    elif tool_name == "browser_click":
        selector = tool_input["selector"]
        return await browser.browser_click(selector)

    elif tool_name == "browser_type":
        selector = tool_input["selector"]
        text = tool_input["text"]
        return await browser.browser_type(selector, text)

    elif tool_name == "browser_search":
        query = tool_input["query"]
        return await browser.browser_search(query)

    elif tool_name == "browser_open_tab":
        return await browser.browser_open_tab(tool_input["url"])

    elif tool_name == "browser_switch_tab":
        return await browser.browser_switch_tab(int(tool_input["index"]))

    elif tool_name == "browser_list_tabs":
        return await browser.browser_list_tabs()

    elif tool_name == "browser_session_state":
        return await browser.browser_session_state()

    elif tool_name == "clipboard_read":
        return await system.clipboard_read()

    elif tool_name == "clipboard_write":
        text = tool_input["text"]
        return await system.clipboard_write(text)

    elif tool_name == "process_list":
        return await system.process_list()

    elif tool_name == "process_kill":
        name = tool_input["name"]
        return await system.process_kill(name)

    elif tool_name == "system_info":
        return await system.system_info()

    elif tool_name == "take_screenshot":
        return await system.take_screenshot()

    elif tool_name == "surface_to_user":
        payload = {
            "title": tool_input["title"],
            "content": tool_input["content"],
            "action_label": tool_input.get("action_label", ""),
            "requires_approval": tool_input.get("requires_approval", False),
        }
        try:
            SURFACE_FILE.parent.mkdir(parents=True, exist_ok=True)
            SURFACE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning(f"surface_to_user write failed: {e}")
        log.info(f"[SURFACE] {tool_input['title']}")
        log.info(tool_input["content"][:400])
        return "Surfaced to user."

    # Subagent delegation
    elif tool_name == "delegate_task":
        from actions import delegate as delegate_mod

        task = tool_input["task"]
        subagent_type = tool_input.get("subagent_type", "general")
        max_agents = tool_input.get("max_subagents", 3)
        return await delegate_mod.delegate_task(
            task, subagent_type, max_agents, context
        )

    # Memory
    elif tool_name == "memory_add":
        from actions import memory as memory_mod

        content = tool_input["content"]
        mem_type = tool_input.get("memory_type", "factual")
        return await memory_mod.memory_add(content, mem_type)

    elif tool_name == "memory_search":
        from actions import memory as memory_mod

        query = tool_input["query"]
        return await memory_mod.memory_search(query)

    elif tool_name == "memory_get_profile":
        from actions import memory as memory_mod

        return await memory_mod.memory_get_profile()

    elif tool_name == "memory_store_file":
        from actions import memory as memory_mod

        file_path = tool_input["file_path"]
        scope = tool_input.get("scope", "USER")
        return await memory_mod.memory_store_file(file_path, scope)

    elif tool_name == "memory_list_files":
        from actions import memory as memory_mod

        prefix = tool_input.get("prefix", "")
        scope = tool_input.get("scope", "USER")
        return await memory_mod.memory_list_files(prefix, scope)

    # Todo
    elif tool_name == "todo_add":
        from actions import todo as todo_mod

        title = tool_input["title"]
        desc = tool_input.get("description", "")
        due = tool_input.get("due")
        priority = tool_input.get("priority", 3)
        return await todo_mod.todo_add(title, desc, due, priority)

    elif tool_name == "todo_list":
        from actions import todo as todo_mod

        status = tool_input.get("status", "pending")
        limit = tool_input.get("limit", 20)
        return await todo_mod.todo_list(status, limit)

    elif tool_name == "todo_complete":
        from actions import todo as todo_mod

        return await todo_mod.todo_complete(tool_input["todo_id"])

    elif tool_name == "todo_delete":
        from actions import todo as todo_mod

        return await todo_mod.todo_delete(tool_input["todo_id"])

    # Reminders
    elif tool_name == "reminder_add":
        from actions import todo as todo_mod

        msg = tool_input["message"]
        secs = tool_input.get("seconds", 60)
        return await todo_mod.reminder_add(msg, secs)

    elif tool_name == "reminder_list":
        from actions import todo as todo_mod

        return await todo_mod.reminder_list()

    # Scheduler
    elif tool_name == "schedule_interval":
        from actions import scheduler as sched_mod

        job_id = tool_input["job_id"]
        secs = tool_input.get("seconds")
        mins = tool_input.get("minutes")
        hrs = tool_input.get("hours")
        return await sched_mod.schedule_interval(
            job_id, lambda: None, seconds=secs, minutes=mins, hours=hrs
        )

    elif tool_name == "schedule_cron":
        from actions import scheduler as sched_mod

        job_id = tool_input["job_id"]
        cron = tool_input["cron"]
        return await sched_mod.schedule_cron(job_id, lambda: None, cron)

    elif tool_name == "unschedule":
        from actions import scheduler as sched_mod

        return await sched_mod.unschedule(tool_input["job_id"])

    # Mission mode
    elif tool_name == "mission_create":
        from actions import mission as mission_mod

        return await mission_mod.mission_create(
            goal=tool_input["goal"],
            plan_json=tool_input.get("plan_json", ""),
            context=context,
        )

    elif tool_name == "mission_start":
        from actions import mission as mission_mod

        return await mission_mod.mission_start(
            int(tool_input["mission_id"]), context=context
        )

    elif tool_name == "mission_pause":
        from actions import mission as mission_mod

        return await mission_mod.mission_pause(int(tool_input["mission_id"]))

    elif tool_name == "mission_resume":
        from actions import mission as mission_mod

        return await mission_mod.mission_resume(
            int(tool_input["mission_id"]), context=context
        )

    elif tool_name == "mission_status":
        from actions import mission as mission_mod

        return await mission_mod.mission_status(int(tool_input["mission_id"]))

    elif tool_name == "mission_list":
        from actions import mission as mission_mod

        return await mission_mod.mission_list(
            limit=int(tool_input.get("limit", 10)),
            status=str(tool_input.get("status", "")).strip(),
        )

    elif tool_name == "mission_rollback":
        from actions import mission as mission_mod

        return await mission_mod.mission_rollback(
            mission_id=int(tool_input["mission_id"]),
            steps=int(tool_input.get("steps", 1)),
            context=context,
        )

    # Code execution
    elif tool_name == "execute_code":
        from actions import code_exec as code_mod

        lang = tool_input["language"]
        code = tool_input["code"]
        timeout = tool_input.get("timeout", 30)
        return await code_mod.code_run(
            lang,
            code,
            timeout,
            workspace=tool_input.get("workspace"),
            filename=tool_input.get("filename"),
            args=tool_input.get("args", ""),
            keep_file=bool(tool_input.get("keep_file", False)),
        )

    # Office
    elif tool_name == "excel_read":
        from actions import office as office_mod

        return await office_mod.excel_read(tool_input["path"], tool_input.get("sheet"))

    elif tool_name == "excel_write":
        from actions import office as office_mod

        return await office_mod.excel_write(
            tool_input["path"], tool_input["data"], tool_input.get("sheet")
        )

    elif tool_name == "excel_append":
        from actions import office as office_mod

        return await office_mod.excel_append(
            tool_input["path"], tool_input["data"], tool_input.get("sheet")
        )

    elif tool_name == "word_read":
        from actions import office as office_mod

        return await office_mod.word_read(tool_input["path"])

    elif tool_name == "word_write":
        from actions import office as office_mod

        return await office_mod.word_write(tool_input["path"], tool_input["content"])

    elif tool_name == "pdf_read":
        from actions import office as office_mod

        return await office_mod.pdf_read(tool_input["path"], tool_input.get("page"))

    elif tool_name == "pdf_info":
        from actions import office as office_mod

        return await office_mod.pdf_info(tool_input["path"])

    # Complex task execution
    elif tool_name == "execute_complex":
        from actions import complex_task as ct_mod

        goal = tool_input["goal"]
        verify = tool_input.get("verify", True)
        return await ct_mod.execute_complex(goal, context, verify)

    elif tool_name == "plan_task":
        from actions import complex_task as ct_mod

        return await ct_mod.plan_task(tool_input["goal"], context)

    elif tool_name == "bootstrap_capability":
        from actions import bootstrap as bootstrap_mod

        return await bootstrap_mod.bootstrap_capability(
            requirement=tool_input["requirement"],
            install=tool_input.get("install", True),
            create_local_fallback=tool_input.get("create_local_fallback", True),
            run_command=_terminal_exec,
        )

    elif tool_name == "create_local_adapter":
        return adapters_mod.create_local_adapter(
            requirement=tool_input["requirement"],
            adapter_name=tool_input["adapter_name"],
            description=tool_input.get("description", ""),
            mode=tool_input.get("mode", "command"),
            command_template=tool_input.get("command_template", ""),
            python_script=tool_input.get("python_script", ""),
            input_schema_json=tool_input.get("input_schema_json", ""),
        )

    elif tool_name == "list_local_adapters":
        rows = adapters_mod.list_adapters()
        if not rows:
            return "No local adapters registered."
        lines = ["## Local Adapters"]
        for r in rows[:30]:
            runs = int(r.get("total_runs", 0))
            succ = int(r.get("success_runs", 0))
            trust = (succ + 1.0) / (runs + 2.0)
            lines.append(
                f"- {r.get('name', 'adapter')} ({r.get('mode', 'command')}, trust {trust:.2f}, runs {runs}): {r.get('description', '')[:90]}"
            )
        return "\n".join(lines)

    elif tool_name == "verify_local_adapter":
        return adapters_mod.verify_local_adapter(
            adapter_name=tool_input["adapter_name"],
            sample_input_json=tool_input.get("sample_input_json", ""),
            run_command=_terminal_exec,
        )

    elif tool_name == "check_permissions":
        from actions import permissions as perms_mod

        return perms_mod.check_permissions(detailed=tool_input.get("detailed", False))

    elif tool_name == "open_permission_panels":
        from actions import permissions as perms_mod

        return perms_mod.open_permission_panels()

    # Background processes
    elif tool_name == "run_background":
        from actions import process_registry as proc_mod

        command = tool_input["command"]
        process_id = tool_input.get("process_id")
        notify = tool_input.get("notify", True)

        return await proc_mod.run_background(command, process_id, notify)

    elif tool_name == "get_background_status":
        from actions import process_registry as proc_mod

        return proc_mod.get_background_status(tool_input["process_id"])

    elif tool_name == "cancel_background":
        from actions import process_registry as proc_mod

        return (
            "Cancelled"
            if proc_mod.cancel_background(tool_input["process_id"])
            else "Not found or not running"
        )

    elif tool_name == "list_background":
        from actions import process_registry as proc_mod

        status = tool_input.get("status")
        procs = proc_mod.get_process_registry().list_processes()

        if not procs:
            return "No background processes."

        lines = ["## Background Processes\n"]
        for p in procs:
            if status and p.status.value != status:
                continue
            lines.append(
                f"- {p.process_id}: {p.status.value} (command: {p.command[:60]})"
            )

        return "\n".join(lines)

    # User-facing toast notification
    elif tool_name == "notify_user":
        title = tool_input.get("title", "Marrow")
        message = tool_input.get("message", "")
        urgency = int(tool_input.get("urgency", 3))
        try:
            from ui.bridge import get_bridge

            get_bridge().toast_requested.emit(title, message, urgency)
            log.info(f"[TOAST] {title}: {message[:80]}")
        except Exception as e:
            log.warning(f"notify_user bridge emit failed: {e}")
        return f"[notified] {message[:80]}"

    # Email retrieval
    elif tool_name == "get_emails":
        hours = int(tool_input.get("hours", 24))
        max_count = int(tool_input.get("max_count", 10))
        unread = tool_input.get("unread_only", True)
        return _get_emails(hours, max_count, unread)

    # Calendar retrieval
    elif tool_name == "get_calendar":
        days = int(tool_input.get("days", 1))
        return _get_calendar(days)

    elif tool_name == "email_draft":
        from actions import communications as comms_mod

        return await comms_mod.email_draft(
            tool_input["recipient"],
            tool_input["subject"],
            tool_input["body"],
            run_command=_terminal_exec,
        )

    elif tool_name == "email_send":
        from actions import communications as comms_mod

        return await comms_mod.email_send(
            tool_input["recipient"],
            tool_input["subject"],
            tool_input["body"],
            run_command=_terminal_exec,
        )

    elif tool_name == "calendar_create_event":
        from actions import communications as comms_mod

        return await comms_mod.calendar_create_event(
            tool_input["title"],
            tool_input["start_iso"],
            tool_input["end_iso"],
            location=tool_input.get("location", ""),
            body=tool_input.get("body", ""),
            run_command=_terminal_exec,
        )

    elif tool_name == "followup_add":
        from actions import communications as comms_mod

        return await comms_mod.followup_add(
            tool_input["contact"],
            tool_input.get("topic", ""),
            int(tool_input.get("when_seconds", 86400)),
        )

    elif tool_name == "capture_workspace_checkpoint":
        from actions import verification as verify_mod

        snap = verify_mod.capture_checkpoint(
            tool_input["label"],
            tool_input.get("expectation", ""),
        )
        return f"[checkpoint] {snap.get('label', '')} captured"

    elif tool_name == "compare_workspace_checkpoints":
        from actions import verification as verify_mod

        return verify_mod.compare_checkpoints(
            tool_input["before_label"],
            tool_input["after_label"],
            tool_input.get("expectation", ""),
        )

    elif tool_name == "communications_brief":
        from actions import operator_ops as ops_mod

        return await ops_mod.communications_brief(_get_emails, _get_calendar)

    elif tool_name == "document_task":
        from actions import operator_ops as ops_mod

        return await ops_mod.document_task(
            tool_input["operation"],
            tool_input["path"],
            content=tool_input.get("content", ""),
            sheet=tool_input.get("sheet", ""),
            page=tool_input.get("page"),
        )

    elif tool_name == "browser_research":
        from actions import operator_ops as ops_mod

        return await ops_mod.browser_research(
            tool_input["goal"],
            query=tool_input.get("query", ""),
            url=tool_input.get("url", ""),
        )

    elif tool_name == "computer_workflow":
        from actions import operator_ops as ops_mod

        return await ops_mod.computer_workflow(
            tool_input["goal"],
            command=tool_input.get("command", ""),
            app_name=tool_input.get("app_name", ""),
            run_command=_terminal_exec,
        )

    elif tool_name == "project_workflow":
        from actions import operator_ops as ops_mod

        return await ops_mod.project_workflow(
            tool_input["goal"],
            repo_path=tool_input.get("repo_path", ""),
            run_command=_terminal_exec,
        )

    elif tool_name == "personal_workflow":
        from actions import operator_ops as ops_mod

        return await ops_mod.personal_workflow(
            tool_input["kind"],
            tool_input.get("detail", ""),
        )

    elif tool_name == "verify_workspace_state":
        from actions import operator_ops as ops_mod

        return await ops_mod.verify_workspace_state(
            tool_input.get("expectation", "")
        )

    # Approval
    elif tool_name == "set_approval_mode":
        from actions import approval as approval_mod

        mode = tool_input["mode"]
        approval_mod.set_approval_mode(mode)
        return f"Approval mode set to: {mode}"

    # Window management
    elif tool_name == "window_list":
        from actions import app_control as app_mod

        return await app_mod.window_list()
    elif tool_name == "window_focus":
        from actions import app_control as app_mod

        return await app_mod.window_focus(tool_input["title"])
    elif tool_name == "window_focus_verified":
        from actions import app_control as app_mod

        return await app_mod.window_focus_verified(tool_input["title"])
    elif tool_name == "window_move":
        from actions import app_control as app_mod

        return await app_mod.window_move(
            tool_input["title"], tool_input["x"], tool_input["y"]
        )
    elif tool_name == "window_resize":
        from actions import app_control as app_mod

        return await app_mod.window_resize(
            tool_input["title"], tool_input["width"], tool_input["height"]
        )
    elif tool_name == "window_minimize":
        from actions import app_control as app_mod

        return await app_mod.window_minimize(tool_input["title"])
    elif tool_name == "window_maximize":
        from actions import app_control as app_mod

        return await app_mod.window_maximize(tool_input["title"])
    elif tool_name == "window_close":
        from actions import app_control as app_mod

        return await app_mod.window_close(tool_input["title"])

    # Application control
    elif tool_name == "app_launch":
        from actions import app_control as app_mod

        return await app_mod.app_launch(
            tool_input["path"], tool_input.get("arguments", "")
        )
    elif tool_name == "app_launch_verified":
        from actions import app_control as app_mod

        return await app_mod.app_launch_verified(
            tool_input["path"], tool_input.get("arguments", "")
        )
    elif tool_name == "app_close":
        from actions import app_control as app_mod

        return await app_mod.app_close(tool_input["name"])

    # Smart home / hardware bridge
    elif tool_name == "smart_home_call":
        from actions import smart_home as smarthome_mod

        payload = {}
        if tool_input.get("payload_json"):
            try:
                payload = json.loads(tool_input.get("payload_json", "{}"))
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}

        return await smarthome_mod.ha_call(
            service=tool_input["service"],
            entity_id=tool_input.get("entity_id", ""),
            payload=payload,
        )

    elif tool_name == "set_system_volume":
        from actions import smart_home as smarthome_mod

        return await smarthome_mod.set_volume(int(tool_input["percent"]))

    # Mouse control
    elif tool_name == "mouse_move":
        from actions import app_control as app_mod

        return await app_mod.mouse_move(tool_input["x"], tool_input["y"])
    elif tool_name == "mouse_click":
        from actions import app_control as app_mod

        return await app_mod.mouse_click(
            tool_input.get("x"), tool_input.get("y"), tool_input.get("button", "left")
        )

    # Keyboard control
    elif tool_name == "keyboard_type":
        from actions import app_control as app_mod

        return await app_mod.keyboard_type(tool_input["text"])
    elif tool_name == "keyboard_hotkey":
        from actions import app_control as app_mod

        return await app_mod.keyboard_hotkey(tool_input["keys"])

    # Clipboard (redundant with system.py but here for completeness)
    elif tool_name == "clipboard_get":
        from actions import app_control as app_mod

        return await app_mod.clipboard_get()
    elif tool_name == "clipboard_set":
        from actions import app_control as app_mod

        return await app_mod.clipboard_set(tool_input["text"])

    # Screenshot
    elif tool_name == "screenshot":
        from actions import app_control as app_mod

        return await app_mod.screenshot()
    elif tool_name == "screenshot_region":
        from actions import app_control as app_mod

        return await app_mod.screenshot_region(
            tool_input["x"], tool_input["y"], tool_input["width"], tool_input["height"]
        )

    # Fact checking
    elif tool_name == "fact_check":
        from brain.claim_verifier import _verify_claim, Claim

        claim_obj = Claim(
            text=tool_input["claim"],
            topic="general",
            source=tool_input.get("context", "user"),
            confidence=1.0,
        )
        result = await _verify_claim(claim_obj)
        if result:
            sources = "\n".join(f"  • {s}" for s in result.sources[:3])
            verdict_label = {
                "false": "FALSE",
                "true": "CONFIRMED",
                "misleading": "MISLEADING",
                "unverified": "UNVERIFIED",
            }.get(result.verdict, result.verdict.upper())
            out = f"[{verdict_label}] {result.explanation}"
            if sources:
                out += f"\nSources:\n{sources}"
            return out
        return "[unverified] Could not find sufficient evidence to verify this claim."

    return f"[unknown tool: {tool_name}]"


# ─── Main entry point ──────────────────────────────────────────────────────────


def _emit_execution_status(kind: str, message: str, **extra) -> None:
    payload = {"kind": kind, "message": message[:220]}
    payload.update(extra)
    try:
        from ui.bridge import get_bridge

        bridge = get_bridge()
        bridge.agent_update.emit(json.dumps(payload))
        bridge.overlay_update.emit(
            json.dumps(
                {
                    "kind": "execution",
                    "state": "acting",
                    "current_action": message[:120],
                    "next_step": extra.get("tool", ""),
                    "confidence": extra.get("confidence", 0.72),
                    "body": message[:220],
                }
            )
        )
    except Exception:
        pass


def _task_needs_generalist_escalation(task: str) -> bool:
    low = (task or "").lower()
    triggers = [
        "build ",
        "create app",
        "create software",
        "build software",
        "scaffold",
        "debug ",
        "fix ",
        "refactor",
        "write code",
        "implement",
        "ship ",
        "run tests",
        "set up repo",
        "compile",
    ]
    return any(token in low for token in triggers)


async def _build_dynamic_capability_context() -> str:
    try:
        from actions.capabilities import capability_summary_text

        return "\n\nRuntime capabilities:\n" + capability_summary_text()
    except Exception:
        return ""


async def _maybe_swarm_boost(task: str, context: str) -> str:
    if not _task_needs_generalist_escalation(task):
        return ""
    try:
        from brain.swarm import get_swarm_coordinator

        _emit_execution_status(
            "swarm", f"Preparing multi-agent pass for: {task[:120]}", confidence=0.8
        )
        summary = await get_swarm_coordinator().run(task, context=context)
        return "\n\nSwarm analysis:\n" + summary[:2400]
    except Exception as e:
        log.debug(f"Swarm boost skipped: {e}")
        return ""


async def _repair_direct_execution(
    llm,
    task: str,
    context: str,
    result_text: str,
) -> str:
    try:
        from brain.context_engine import build_reasoning_context
    except Exception:
        return ""

    selected = await build_reasoning_context(
        task,
        context_hint=context,
        session_id=getattr(config, "DEEP_REASONING_SESSION_ID", "default"),
    )
    assembled = selected.get("assembled_context", "") or context
    prompt = f"""A direct action attempt underdelivered. Produce a tighter retry brief.

Return strict JSON only:
{{
  "retry": true|false,
  "revised_task": "",
  "reason": ""
}}

Retry only if a sharper second pass is likely to help.

Original task:
{task}

Context:
{assembled[:3500]}

Current result:
{(result_text or "")[:2500]}
"""
    response = await llm.create(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=220,
        model_type="scoring",
    )
    raw = (response.text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        return ""
    data = json.loads(raw[start:end])
    if not bool(data.get("retry")):
        return ""
    return str(data.get("revised_task", "") or "").strip()[:700]


async def execute_action(task: str, context: str = "") -> str:
    """
    Run the LLM with all Marrow tools to complete a task.
    Uses the unified LLM client so Anthropic/OpenAI/Ollama all work.
    Records actions and conversation for memory.
    """
    from brain.llm import get_client
    from actions import memory as memory_mod
    from actions import adapters as adapters_mod
    from actions import verification as verify_mod

    llm = get_client()

    resolved_task = _resolve_followup_task(task)
    if resolved_task != task:
        log.info(f"Resolved follow-up '{task}' -> '{resolved_task[:120]}'")

    # Inject memory context
    memory_context = ""
    try:
        profile = await memory_mod.memory_get_profile()
        if profile and len(profile) > 10:
            memory_context = f"\n\nRelevant memory:\n{profile[:2000]}"
    except Exception:
        pass

    # Inject recent chat turns for continuity in interactive sessions
    recent_chat_context = ""
    try:
        recent = db.get_recent_conversations(limit=config.ACTION_CHAT_HISTORY_MESSAGES)
        if recent:
            # DB returns newest first; we want chronological order
            recent = list(reversed(recent))
            lines = []
            chars = 0
            for row in recent:
                role = row.get("role", "user")
                content = (row.get("content") or "").strip().replace("\n", " ")
                if not content:
                    continue
                entry = f"{role}: {content[:220]}"
                chars += len(entry)
                if chars > config.ACTION_CHAT_HISTORY_CHARS:
                    break
                lines.append(entry)
            if lines:
                recent_chat_context = "\n\nRecent conversation turns:\n" + "\n".join(
                    lines
                )
    except Exception:
        pass

    recommended_adapter = adapters_mod.recommend_adapter_tool(
        resolved_task,
        min_trust=float(config.ADAPTER_MIN_TRUST_TO_RECOMMEND),
    )

    observed_history_context = ""
    try:
        if _is_history_question(resolved_task):
            observed_history_context = (
                "\n\nPrioritize answering from observed local history below. "
                "If data is incomplete, state uncertainty clearly.\n"
                + _build_observation_history_context()
            )
    except Exception:
        pass

    adapter_hint = (
        f"\n\nPreferred adapter for this task: {recommended_adapter}. Use it first unless clearly unsuitable."
        if recommended_adapter
        else ""
    )

    capability_context = await _build_dynamic_capability_context()
    swarm_context = await _maybe_swarm_boost(resolved_task, context)

    user_content = (
        f"{resolved_task}\n\nContext:\n{context}{recent_chat_context}{memory_context}{observed_history_context}{capability_context}{swarm_context}"
        if context
        else f"{resolved_task}{recent_chat_context}{memory_context}{observed_history_context}{capability_context}{swarm_context}"
    )
    user_content += adapter_hint

    log.info(f"Executing action: {task[:80]}")
    _emit_execution_status(
        "start", f"Executing: {resolved_task[:120]}", confidence=0.74
    )
    before_label = f"direct_before_{int(__import__('time').time() * 1000)}"
    after_label = before_label.replace("before", "after")
    verify_mod.capture_checkpoint(before_label, resolved_task[:220])
    asyncio.create_task(
        memory_mod.memory_record_conversation("user", task, "action_request")
    )

    async def _tool_handler(name: str, inp: dict) -> str:
        return await _async_handle_tool_call(name, inp, context)

    async def _on_tool_call(name: str, inp: dict, result: str) -> None:
        _emit_execution_status(
            "tool",
            f"{name} completed",
            tool=name,
            confidence=0.78 if not result.startswith("[error]") else 0.38,
        )
        asyncio.create_task(
            memory_mod.memory_record_action(
                task=task,
                result=result[:500],
                tool=name,
                success=not result.startswith("[error]"),
            )
        )

    toolset = MARROW_TOOLS + adapters_mod.get_adapter_tools()

    final_text = await llm.create_with_tools(
        messages=[{"role": "user", "content": user_content}],
        tools=toolset,
        tool_handler=_tool_handler,
        system=ACTION_SYSTEM_PROMPT,
        max_tokens=1024,
        model_type="reasoning",
        max_iterations=MAX_ITERATIONS,
        on_tool_call=_on_tool_call,
    )

    # If base loop returns weak/incomplete answer, auto-escalate to complex planner.
    if config.AUTO_COMPLEX_ESCALATION:
        low = (final_text or "").lower()
        _simple_task = any(w in resolved_task.lower() for w in [
            "open ", "launch ", "start ", "close ", "minimize ", "maximize ",
            "focus ", "switch to", "show ", "hide ",
        ])
        incomplete = (
            not _simple_task
            and (
                (not final_text)
                or "max iterations reached" in low
                or "[unknown tool" in low
                or "[error" in low
                or "unavailable" in low
            )
        )
        if incomplete:
            try:
                revised_task = await _repair_direct_execution(
                    llm,
                    resolved_task,
                    context,
                    final_text or "",
                )
                if revised_task and revised_task.lower() != resolved_task.lower():
                    _emit_execution_status(
                        "repair",
                        "Retrying direct execution with revised brief",
                        confidence=0.67,
                    )
                    retry_content = (
                        f"{revised_task}\n\nContext:\n{context}{recent_chat_context}{memory_context}{observed_history_context}{capability_context}{swarm_context}"
                        if context
                        else f"{revised_task}{recent_chat_context}{memory_context}{observed_history_context}{capability_context}{swarm_context}"
                    )
                    retry_content += adapter_hint
                    repaired = await llm.create_with_tools(
                        messages=[{"role": "user", "content": retry_content}],
                        tools=toolset,
                        tool_handler=_tool_handler,
                        system=ACTION_SYSTEM_PROMPT,
                        max_tokens=1024,
                        model_type="reasoning",
                        max_iterations=MAX_ITERATIONS,
                        on_tool_call=_on_tool_call,
                    )
                    if repaired:
                        final_text = repaired
                        low = final_text.lower()
                        incomplete = (
                            (not final_text)
                            or "max iterations reached" in low
                            or "[unknown tool" in low
                            or "[error" in low
                            or "unavailable" in low
                        )
            except Exception as e:
                log.debug(f"Direct execution repair skipped: {e}")
        if incomplete:
            try:
                from actions import complex_task as ct_mod

                _emit_execution_status(
                    "escalation", "Escalating to complex planner", confidence=0.68
                )
                plan_summary = await ct_mod.execute_complex(
                    goal=task,
                    context=context,
                    verify=False,
                )
                final_text = (
                    (final_text or "") + "\n\n[Escalated execution]\n" + plan_summary
                )
            except Exception as e:
                log.debug(f"Auto complex escalation skipped: {e}")

    verify_mod.capture_checkpoint(after_label, resolved_task[:220])
    verification_summary = verify_mod.compare_checkpoints(
        before_label,
        after_label,
        resolved_task[:220],
    )
    rollback_hint = verify_mod.rollback_hint(
        resolved_task,
        final_text or "",
        before_label,
        after_label,
    )
    if verification_summary:
        final_text = (final_text or "Done.") + "\n\n[Verification]\n" + verification_summary
    if rollback_hint:
        final_text = (final_text or "Done.") + "\n\n" + rollback_hint

    # Auto-learn: suggest adapter for repeated workflows
    if config.ADAPTER_AUTO_LEARN:
        try:
            tip = adapters_mod.maybe_suggest_adapter(
                task,
                threshold=max(2, int(config.ADAPTER_SUGGEST_THRESHOLD)),
            )
            if tip:
                final_text = (final_text or "Done.") + "\n\n" + tip
                try:
                    from ui.bridge import get_bridge

                    get_bridge().toast_requested.emit(
                        config.MARROW_NAME,
                        "Repeated workflow detected. I can create a reusable local adapter.",
                        4,
                    )
                except Exception:
                    pass
        except Exception as e:
            log.debug(f"Adapter auto-learn skipped: {e}")

    asyncio.create_task(
        memory_mod.memory_record_conversation(
            "assistant", final_text, "action_response"
        )
    )
    _emit_execution_status("done", f"Finished: {resolved_task[:120]}", confidence=0.83)
    return final_text or "Done."
