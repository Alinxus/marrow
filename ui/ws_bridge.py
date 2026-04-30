"""
WebSocket bridge — replaces Qt signals for the Tauri frontend.

Protocol: JSON messages over ws://localhost:7734
  Backend → Frontend: {"type": "<signal_name>", "data": <payload>}
  Frontend → Backend: {"type": "command", "action": "<action>", "payload": <data>}
"""

import asyncio
import json
import logging
from typing import Any, Optional, Callable, Set

log = logging.getLogger(__name__)

_WS_PORT = 7734
_clients: Set = set()
_command_handler: Optional[Callable] = None
_server = None


def set_command_handler(fn: Callable) -> None:
    global _command_handler
    _command_handler = fn


async def _handle_client(websocket) -> None:
    _clients.add(websocket)
    log.info(f"UI client connected ({len(_clients)} total)")
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                if _command_handler:
                    await _command_handler(msg)
            except Exception as e:
                log.debug(f"WS command error: {e}")
    except Exception:
        pass
    finally:
        _clients.discard(websocket)
        log.info(f"UI client disconnected ({len(_clients)} remaining)")


async def broadcast(event_type: str, data: Any) -> None:
    if not _clients:
        return
    msg = json.dumps({"type": event_type, "data": data})
    dead = set()
    for ws in list(_clients):
        try:
            await ws.send(msg)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


async def start_server() -> None:
    global _server
    try:
        import websockets
        _server = await websockets.serve(_handle_client, "127.0.0.1", _WS_PORT)
        log.info(f"Marrow WS bridge listening on ws://127.0.0.1:{_WS_PORT}")
    except Exception as e:
        log.error(f"WS bridge failed to start: {e}")


def stop_server() -> None:
    global _server
    if _server:
        _server.close()
        _server = None
