"""Real communications actions for email and follow-up workflows."""

from __future__ import annotations

import platform
from typing import Optional

from actions import todo


def _ps_escape(text: str) -> str:
    return str(text or "").replace("'", "''")


def _run_powershell(script: str, run_command) -> str:
    return run_command(script, 25)


async def email_draft(
    recipient: str,
    subject: str,
    body: str,
    *,
    run_command,
) -> str:
    if platform.system() != "Windows":
        return "[error] Email draft automation currently supports Windows Outlook only."
    script = f"""
try {{
    $outlook = New-Object -ComObject Outlook.Application
    $mail = $outlook.CreateItem(0)
    $mail.To = '{_ps_escape(recipient)}'
    $mail.Subject = '{_ps_escape(subject)}'
    $mail.Body = @'
{body}
'@
    $mail.Save()
    Write-Output 'Draft saved in Outlook.'
}} catch {{
    Write-Output 'ERROR: ' + $_
}}
""".strip()
    result = _run_powershell(script, run_command)
    return result if "ERROR:" not in result else f"[error] {result}"


async def email_send(
    recipient: str,
    subject: str,
    body: str,
    *,
    run_command,
) -> str:
    if platform.system() != "Windows":
        return "[error] Email send automation currently supports Windows Outlook only."
    script = f"""
try {{
    $outlook = New-Object -ComObject Outlook.Application
    $mail = $outlook.CreateItem(0)
    $mail.To = '{_ps_escape(recipient)}'
    $mail.Subject = '{_ps_escape(subject)}'
    $mail.Body = @'
{body}
'@
    $mail.Send()
    Write-Output 'Email sent.'
}} catch {{
    Write-Output 'ERROR: ' + $_
}}
""".strip()
    result = _run_powershell(script, run_command)
    return result if "ERROR:" not in result else f"[error] {result}"


async def calendar_create_event(
    title: str,
    start_iso: str,
    end_iso: str,
    *,
    location: str = "",
    body: str = "",
    run_command,
) -> str:
    if platform.system() != "Windows":
        return "[error] Calendar event creation currently supports Windows Outlook only."
    script = f"""
try {{
    $outlook = New-Object -ComObject Outlook.Application
    $appt = $outlook.CreateItem(1)
    $appt.Subject = '{_ps_escape(title)}'
    $appt.Start = '{_ps_escape(start_iso)}'
    $appt.End = '{_ps_escape(end_iso)}'
    $appt.Location = '{_ps_escape(location)}'
    $appt.Body = @'
{body}
'@
    $appt.Save()
    Write-Output 'Calendar event saved.'
}} catch {{
    Write-Output 'ERROR: ' + $_
}}
""".strip()
    result = _run_powershell(script, run_command)
    return result if "ERROR:" not in result else f"[error] {result}"


async def followup_add(contact: str, topic: str, when_seconds: int = 86400) -> str:
    title = f"Follow up with {contact}"
    detail = topic or f"Check back in with {contact}"
    await todo.todo_add(title=title, description=detail, priority=2)
    await todo.reminder_add(f"{title}: {detail}", when_seconds)
    return f"[follow-up] Added task and reminder for {contact}."
