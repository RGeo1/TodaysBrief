"""Rendering, length enforcement and idempotent SMTP delivery."""

from __future__ import annotations

import html
import json
import os
import smtplib
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Callable

from .judgment import Action, Brief, format_reason
from .sources import Event, RULE_TZ, _atomic_write_json, _read_json

MAX_VISIBLE_CHARS = 250
MAX_ACTION_CHARS = 45
ACCENT = "#1f5f4a"


class LockBusy(RuntimeError):
    """Another process holds the send lock."""


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = open(self.path, "r+", encoding="utf-8")
        except FileNotFoundError:
            self._handle = open(self.path, "w+", encoding="utf-8")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write("0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise LockBusy(f"send lock busy: {exc}") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    sender: str
    recipient: str
    user: str = ""
    password: str = ""
    tls: str = "starttls"  # ssl | starttls | none
    timeout: int = 20

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        return cls(
            host=os.environ.get("SMTP_HOST", ""),
            port=int(os.environ.get("SMTP_PORT", "587")),
            sender=os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")),
            recipient=os.environ.get("SMTP_TO", ""),
            user=os.environ.get("SMTP_USER", ""),
            password=os.environ.get("SMTP_PASSWORD", ""),
            tls=os.environ.get("SMTP_TLS", "starttls").lower(),
            timeout=int(os.environ.get("SMTP_TIMEOUT", "20")),
        )

    @property
    def complete(self) -> bool:
        return bool(self.host and self.sender and self.recipient)

    def redacted(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "sender": self.sender,
            "recipient": self.recipient,
            "user": self.user,
            "tls": self.tls,
            "timeout": self.timeout,
            "password": "***" if self.password else "",
        }


@dataclass
class RenderedBrief:
    subject: str
    text: str
    html: str
    actions: list[Action]
    timeline: list[Event]
    truncated: bool = False


def _sanitize_header(value: str) -> str:
    return "".join(character for character in value if character not in "\r\n")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _action_line(action: Action) -> str:
    why = format_reason(action.reason)
    if len(action.title) + len(why) > MAX_ACTION_CHARS:
        why = _clip(why, max(8, MAX_ACTION_CHARS - len(action.title)))
    return f"{action.tag} {action.title}\n   {why}"


def _timeline_line(event: Event) -> str:
    when = "全天" if event.all_day else event.start.strftime("%H:%M")
    return f"{when} {event.title}"


def _visible_body(verdict: str, actions: list[Action], timeline: list[Event]) -> str:
    parts = [verdict]
    parts.extend(_action_line(action) for action in actions)
    parts.extend(_timeline_line(event) for event in timeline)
    return "\n".join(parts)


def _visible_length(text: str) -> int:
    return len(text.replace("\n", ""))


def _fit(brief: Brief) -> tuple[list[Action], list[Event], bool]:
    actions = list(brief.actions)
    timeline = list(brief.timeline)
    truncated = False
    while _visible_length(_visible_body(brief.verdict, actions, timeline)) > MAX_VISIBLE_CHARS and actions:
        actions.pop()
        truncated = True
    while _visible_length(_visible_body(brief.verdict, actions, timeline)) > MAX_VISIBLE_CHARS and len(timeline) > 1:
        timeline.pop()
        truncated = True
    return actions, timeline, truncated


def _render_text(verdict: str, actions: list[Action], timeline: list[Event], footer: str) -> str:
    lines = [verdict, ""]
    for action in actions:
        lines.append(_action_line(action))
    if timeline:
        lines.append("")
        lines.append("时间线")
        lines.extend(_timeline_line(event) for event in timeline)
    lines.append("")
    lines.append(footer)
    return "\n".join(lines)


def _render_html(verdict: str, actions: list[Action], timeline: list[Event], footer: str) -> str:
    escape = html.escape
    rows = []
    for action in actions:
        rows.append(
            "<tr>"
            f'<td style="padding:10px 12px 10px 0;vertical-align:top;width:64px;">'
            f'<span style="display:inline-block;border:1px solid {ACCENT};border-radius:2px;'
            f'font-size:10px;letter-spacing:0.08em;color:{ACCENT};padding:1px 4px;">'
            f"{escape(action.tag)}</span></td>"
            f'<td style="padding:10px 0;border-top:1px solid #e5e5e5;vertical-align:top;">'
            f'<div style="font-size:14px;font-weight:600;">{escape(action.title)}</div>'
            f'<div style="font-size:12px;color:#555;line-height:1.5;">{escape(format_reason(action.reason))}</div>'
            "</td></tr>"
        )
    timeline_rows = []
    for event in timeline:
        when = "全天" if event.all_day else event.start.strftime("%H:%M")
        timeline_rows.append(
            "<tr>"
            f'<td style="padding:6px 12px 6px 0;font-size:12px;color:#444;white-space:nowrap;">{escape(when)}</td>'
            f'<td style="padding:6px 0;border-bottom:1px solid #eee;font-size:13px;">{escape(event.title)}</td>'
            "</tr>"
        )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="color-scheme" content="light dark">'
        "<title>今日幕僚</title></head>"
        '<body style="margin:0;padding:0;background:#f6f6f4;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f6f6f4;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="width:600px;max-width:100%;background:#ffffff;border:1px solid #ddd;'
        'font-family:-apple-system,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;color:#222;">'
        '<tr><td style="padding:28px 28px 8px;font-size:12px;color:#777;">'
        "今日幕僚 · 每日简报</td></tr>"
        '<tr><td style="padding:0 28px 8px;font-size:21px;font-weight:600;line-height:1.45;">'
        f"{escape(verdict)}</td></tr>"
        '<tr><td style="padding:8px 28px 0;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(rows)
        + "</table></td></tr>"
        '<tr><td style="padding:16px 28px 4px;font-size:11px;letter-spacing:0.12em;color:#777;">时间线</td></tr>'
        '<tr><td style="padding:0 28px 16px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(timeline_rows)
        + "</table></td></tr>"
        '<tr><td style="padding:14px 28px 24px;border-top:1px solid #e5e5e5;'
        'font-size:11px;color:#777;line-height:1.6;">'
        f"{escape(footer)}</td></tr>"
        "</table></td></tr></table></body></html>"
    )


def render(brief: Brief, day: date, footer: str) -> RenderedBrief:
    actions, timeline, truncated = _fit(brief)
    subject = _sanitize_header(f"{day:%Y-%m-%d} 今日幕僚")
    return RenderedBrief(
        subject=subject,
        text=_render_text(brief.verdict, actions, timeline, footer),
        html=_render_html(brief.verdict, actions, timeline, footer),
        actions=actions,
        timeline=timeline,
        truncated=truncated,
    )


def _smtp_send(rendered: RenderedBrief, config: SmtpConfig, message_id: str) -> None:
    message = EmailMessage()
    message["Subject"] = _sanitize_header(rendered.subject)
    message["From"] = _sanitize_header(config.sender)
    message["To"] = _sanitize_header(config.recipient)
    message["Message-ID"] = message_id
    message.set_content(rendered.text)
    message.add_alternative(rendered.html, subtype="html")
    if config.tls == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout)
    else:
        server = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
    with server:
        if config.tls == "starttls":
            server.ehlo()
            server.starttls()
            server.ehlo()
        if config.user:
            server.login(config.user, config.password)
        server.send_message(message)


Sender = Callable[[RenderedBrief, SmtpConfig, str], None]


def _record_path(state_dir: Path, day: date) -> Path:
    return state_dir / "runs" / f"{day.isoformat()}.json"


def _read_record(state_dir: Path, day: date) -> dict:
    return _read_json(_record_path(state_dir, day))


def _write_record(state_dir: Path, day: date, **fields: object) -> dict:
    path = _record_path(state_dir, day)
    previous = _read_json(path)
    previous.update(fields)
    previous["updated_at"] = datetime.now(RULE_TZ).isoformat()
    _atomic_write_json(path, previous)
    return previous


def _write_undelivered(out_dir: Path, day: date, rendered: RenderedBrief) -> None:
    target = out_dir / "undelivered"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{day.isoformat()}.txt").write_text(rendered.text, encoding="utf-8")
    (target / f"{day.isoformat()}.html").write_text(rendered.html, encoding="utf-8")


def previous_failure_note(state_dir: Path, day: date) -> str | None:
    path = _record_path(state_dir, day)
    record = _read_json(path)
    if record.get("status") == "failed":
        return f"上次投递失败于 {record.get('updated_at', '未知时间')}。"
    return None


def send_brief(
    rendered: RenderedBrief,
    day: date,
    config: SmtpConfig,
    state_dir: Path,
    out_dir: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    sender: Sender | None = None,
    retry_delays: tuple[int, ...] = (5, 15),
) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    if dry_run:
        (out_dir / f"brief_{day.isoformat()}.txt").write_text(rendered.text, encoding="utf-8")
        (out_dir / f"brief_{day.isoformat()}.html").write_text(rendered.html, encoding="utf-8")
        return "dry_run"
    state_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(state_dir / "send.lock"):
        record = _read_record(state_dir, day)
        status = record.get("status")
        if status == "sent" and not force:
            return "already_sent"
        if status == "inflight" and not force:
            return "inflight"
        if status == "claimed" and not force:
            updated = record.get("updated_at")
            if updated:
                try:
                    age = datetime.now(RULE_TZ) - datetime.fromisoformat(updated)
                    if age < timedelta(minutes=10):
                        return "busy"
                except ValueError:
                    pass
        message_id = make_msgid(domain="today-brief.local")
        _write_record(state_dir, day, status="claimed", attempts=record.get("attempts", 0))
        _write_record(state_dir, day, status="inflight", message_id=message_id)
        last_error: Exception | None = None
        attempts = 0
        for delay in (0, *retry_delays):
            if delay:
                time.sleep(delay)
            attempts += 1
            try:
                (sender or _smtp_send)(rendered, config, message_id)
                _write_record(state_dir, day, status="sent", attempts=attempts, error="")
                return "sent"
            except Exception as exc:  # retry only while the process is alive
                last_error = exc
        _write_record(
            state_dir,
            day,
            status="failed",
            attempts=attempts,
            error=f"{type(last_error).__name__}: {last_error}",
        )
        _write_undelivered(out_dir, day, rendered)
        return f"failed: {last_error}"


def doctor(state_dir: Path, out_dir: Path, config: SmtpConfig, source: str, day: date) -> str:
    record = _read_record(state_dir, day)
    undelivered = list((out_dir / "undelivered").glob(f"{day.isoformat()}.*"))
    lines = [
        f"source: {source or '未配置'}",
        f"smtp: {json.dumps(config.redacted(), ensure_ascii=False)}",
        f"run: {json.dumps(record, ensure_ascii=False) if record else '今天还没有运行记录'}",
        f"undelivered: {len(undelivered)} 个文件",
        f"state: {state_dir}",
        f"out: {out_dir}",
    ]
    return "\n".join(lines)
