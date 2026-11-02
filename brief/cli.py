"""Command-line entry point for 今日幕僚."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .delivery import (
    SmtpConfig,
    doctor,
    previous_failure_note,
    render,
    send_brief,
)
from .judgment import analyze, load_rules
from .sources import load_source


def _configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from .env without overriding real environment vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="today-brief",
        description="Generate the daily calendar briefing email.",
    )
    parser.add_argument("--dry-run", action="store_true", help="render without sending")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="generate for a specific date")
    parser.add_argument("--resend", action="store_true", help="force resend an unconfirmed brief")
    parser.add_argument("--doctor", action="store_true", help="print diagnostics")
    return parser


def _source_from_env() -> tuple[str | None, Path | None]:
    url = os.environ.get("CALENDAR_ICS_URL") or None
    raw_path = os.environ.get("CALENDAR_ICS_FILE") or None
    path = Path(raw_path).expanduser() if raw_path else None
    return url, path


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    _load_dotenv(Path(".env"))
    args = build_parser().parse_args(argv)
    rules_path = Path(os.environ.get("BRIEF_RULES", "rules.toml"))
    state_dir = Path(os.environ.get("BRIEF_STATE_DIR", "state"))
    out_dir = Path(os.environ.get("BRIEF_OUT_DIR", "out"))
    rules = load_rules(rules_path)
    tz = ZoneInfo(rules.timezone)
    day = date.fromisoformat(args.date) if args.date else datetime.now(tz).date()
    now = (
        datetime.combine(day, time(hour=rules.brief_hour), tzinfo=tz)
        if args.date
        else datetime.now(tz)
    )
    url, path = _source_from_env()
    source_label = "订阅" if url else ("导出文件" if path else "未配置")
    smtp = SmtpConfig.from_env()

    if args.doctor:
        source_display = "订阅（已配置）" if url else (str(path) if path else "未配置")
        print(doctor(state_dir, out_dir, smtp, source_display, day))
        return 0

    if not url and not path:
        print("未配置日历来源：设置 CALENDAR_ICS_URL 或 CALENDAR_ICS_FILE。", file=sys.stderr)
        return 2

    loaded = load_source(url=url, path=path, state_dir=state_dir, now=now)
    if not loaded.ok:
        print(f"无法生成简报：{loaded.warning}", file=sys.stderr)
        return 1

    brief = analyze(loaded.events, rules, day)
    footer_bits = [f"抓取于 {now:%H:%M}", f"来源：{source_label}"]
    if loaded.cached:
        footer_bits.append("缓存")
    if loaded.warning:
        footer_bits.append(loaded.warning)
    if note := previous_failure_note(state_dir, day):
        footer_bits.append(note)
    footer = " · ".join(footer_bits)
    rendered = render(brief, day, footer)

    result = send_brief(
        rendered,
        day,
        smtp,
        state_dir,
        out_dir,
        dry_run=args.dry_run,
        force=args.resend,
    )
    print(result)
    if result == "dry_run":
        print(f"写入 {out_dir / f'brief_{day.isoformat()}.txt'}")
        print(f"写入 {out_dir / f'brief_{day.isoformat()}.html'}")
        return 0
    if result in {"sent", "already_sent"}:
        return 0
    if result == "inflight":
        print("上次发送状态未确认；用 --resend 显式重发，或先运行 --doctor 查看。", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
