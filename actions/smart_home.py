"""Smart home and device control (Home Assistant + local fallbacks)."""

from __future__ import annotations

import platform
import subprocess

import httpx

import config


def _ha_client() -> tuple[httpx.Client | None, str]:
    if not config.HOME_ASSISTANT_URL or not config.HOME_ASSISTANT_TOKEN:
        return None, "Home Assistant not configured"
    c = httpx.Client(
        base_url=config.HOME_ASSISTANT_URL.rstrip("/"),
        headers={
            "Authorization": f"Bearer {config.HOME_ASSISTANT_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=8,
    )
    return c, ""


async def ha_call(
    service: str, entity_id: str = "", payload: dict | None = None
) -> str:
    """Call Home Assistant service: domain.service (e.g. light.turn_on)."""
    client, err = _ha_client()
    if not client:
        return f"[error] {err}. Set HOME_ASSISTANT_URL + HOME_ASSISTANT_TOKEN."

    if "." not in service:
        return "[error] service must be like domain.service"
    domain, svc = service.split(".", 1)
    body = dict(payload or {})
    if entity_id:
        body["entity_id"] = entity_id

    try:
        r = client.post(f"/api/services/{domain}/{svc}", json=body)
        if r.status_code >= 300:
            return f"[error] HA service failed ({r.status_code}): {r.text[:180]}"
        return f"OK: {service} {entity_id or ''}".strip()
    except Exception as e:
        return f"[error] HA call failed: {e}"
    finally:
        try:
            client.close()
        except Exception:
            pass


async def set_volume(percent: int) -> str:
    """Set local system output volume 0-100."""
    v = max(0, min(100, int(percent)))
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            subprocess.run(
                ["osascript", "-e", f"set volume output volume {v}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return f"Volume set to {v}%"
        if sys_name == "Windows":
            # Fallback via PowerShell key simulation (coarse)
            return "[warning] direct Windows volume set not implemented in native API fallback"
        return "[warning] local volume control not implemented on this platform"
    except Exception as e:
        return f"[error] set_volume failed: {e}"
