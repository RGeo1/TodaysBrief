"""Read/write .env and rules.toml for the GUI and CLI."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .sources import _atomic_write

WINDOW_RE = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")
DEFAULT_KEYWORDS = [
    ("客户|评审|面试|review|interview|1:1", 4),
    ("午餐|lunch|hold|可选", 1),
]


@dataclass
class RulesForm:
    timezone: str = "Asia/Shanghai"
    window_hours: int = 24
    brief_hour: int = 7
    max_actions: int = 5
    protected_windows: list[str] = field(default_factory=lambda: ["09:30-11:00"])
    keywords: list[tuple[str, int]] = field(
        default_factory=lambda: list(DEFAULT_KEYWORDS)
    )
    focus_block_minutes: int = 90
    protect_min_salvage_minutes: int = 45
    tight_gap_minutes: int = 10
    do_buffer_minutes: int = 20


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _quote_env(value: str) -> str:
    if value == "":
        return ""
    if any(character in value for character in ' \t#"'):
        return json.dumps(value, ensure_ascii=False)
    return value


def update_env(path: Path, updates: dict[str, str | None]) -> None:
    """Update keys in place, preserving comments and order. None removes a line."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                seen.add(key)
                value = updates[key]
                if value is None:
                    continue
                output.append(f"{key}={_quote_env(value)}")
                continue
        output.append(line)
    for key, value in updates.items():
        if key not in seen and value is not None:
            output.append(f"{key}={_quote_env(value)}")
    text = "\n".join(output).rstrip("\n") + "\n"
    _atomic_write(path, text.encode("utf-8"))


def read_rules_form(path: Path) -> RulesForm:
    if not path.exists():
        return RulesForm()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    general = raw.get("general", {})
    protected = raw.get("protected", {})
    thresholds = raw.get("thresholds", {})
    windows = [str(window) for window in protected.get("windows", ["09:30-11:00"])]
    keywords: list[tuple[str, int]] = []
    for entry in raw.get("keywords", []):
        pattern = entry.get("pattern")
        weight = entry.get("weight", 2)
        if isinstance(pattern, str) and isinstance(weight, int):
            keywords.append((pattern, weight))
    return RulesForm(
        timezone=str(general.get("timezone", "Asia/Shanghai")),
        window_hours=int(general.get("window_hours", 24)),
        brief_hour=int(general.get("brief_hour", 7)),
        max_actions=int(general.get("max_actions", 5)),
        protected_windows=windows or ["09:30-11:00"],
        keywords=keywords or list(DEFAULT_KEYWORDS),
        focus_block_minutes=int(thresholds.get("focus_block_minutes", 90)),
        protect_min_salvage_minutes=int(
            thresholds.get("protect_min_salvage_minutes", 45)
        ),
        tight_gap_minutes=int(thresholds.get("tight_gap_minutes", 10)),
        do_buffer_minutes=int(thresholds.get("do_buffer_minutes", 20)),
    )


def _validate(form: RulesForm) -> None:
    if not form.timezone.strip():
        raise ValueError("时区不能为空")
    if not (1 <= form.brief_hour <= 23):
        raise ValueError("发信时间应在 0–23 点之间")
    if form.max_actions < 1:
        raise ValueError("最多行动数至少为 1")
    if not form.protected_windows:
        raise ValueError("至少需要一个保护时段")
    for window in form.protected_windows:
        if not WINDOW_RE.match(window.strip()):
            raise ValueError(f"保护时段格式应为 HH:MM-HH:MM：{window!r}")
    for pattern, weight in form.keywords:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"非法关键词正则 {pattern!r}: {exc}") from exc
        if not (1 <= weight <= 5):
            raise ValueError(f"关键词权重应在 1–5 之间：{pattern!r}")
    for name in (
        "window_hours",
        "focus_block_minutes",
        "protect_min_salvage_minutes",
        "tight_gap_minutes",
        "do_buffer_minutes",
    ):
        if getattr(form, name) <= 0:
            raise ValueError(f"{name} 必须大于 0")


def write_rules_form(path: Path, form: RulesForm) -> None:
    _validate(form)
    lines = [
        "[general]",
        f"timezone = {json.dumps(form.timezone.strip(), ensure_ascii=False)}",
        f"window_hours = {form.window_hours}",
        f"brief_hour = {form.brief_hour}",
        f"max_actions = {form.max_actions}",
        "",
    ]
    for pattern, weight in form.keywords:
        lines.extend(
            [
                "[[keywords]]",
                f"pattern = {json.dumps(pattern, ensure_ascii=False)}",
                f"weight = {weight}",
                "",
            ]
        )
    windows = ", ".join(
        json.dumps(window.strip(), ensure_ascii=False) for window in form.protected_windows
    )
    lines.extend(
        [
            "[protected]",
            f"windows = [{windows}]",
            "",
            "[thresholds]",
            f"focus_block_minutes = {form.focus_block_minutes}",
            f"protect_min_salvage_minutes = {form.protect_min_salvage_minutes}",
            f"tight_gap_minutes = {form.tight_gap_minutes}",
            f"do_buffer_minutes = {form.do_buffer_minutes}",
            "",
        ]
    )
    _atomic_write(path, "\n".join(lines).encode("utf-8"))
