"""Detectors, structured reasons and action selection.

No numeric score in v1: actions sort by tag priority, then start time, and
cap at `max_actions`. Reasons are structured so the renderer can build the
sentence without a string whitelist.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

from .sources import Event

TAG_PRIORITY = {"PROTECT": 0, "DECIDE": 1, "DO": 2, "WATCH": 3}
WORKDAY_START = time(8, 0)
WORKDAY_END = time(20, 0)
DEFAULT_PROTECTED = ((time(9, 30), time(11, 0)),)
DEFAULT_KEYWORDS = (
    (re.compile(r"客户|评审|面试|review|interview|1:1", re.IGNORECASE), 4),
    (re.compile(r"午餐|lunch|hold|可选", re.IGNORECASE), 1),
)


@dataclass(frozen=True)
class Rules:
    timezone: str = "Asia/Shanghai"
    window_hours: int = 24
    brief_hour: int = 7
    max_actions: int = 5
    focus_block_minutes: int = 90
    protect_min_salvage_minutes: int = 45
    tight_gap_minutes: int = 10
    do_buffer_minutes: int = 20
    default_keyword_weight: int = 2
    protected_windows: tuple[tuple[time, time], ...] = DEFAULT_PROTECTED
    keyword_weights: tuple[tuple[re.Pattern[str], int], ...] = DEFAULT_KEYWORDS


@dataclass(frozen=True)
class Reason:
    kind: str
    values: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    tag: str
    title: str
    start: datetime
    end: datetime
    reason: Reason


@dataclass
class Brief:
    verdict: str
    actions: list[Action]
    timeline: list[Event]
    no_focus: bool = False
    warnings: list[str] = field(default_factory=list)


def load_rules(path: Path) -> Rules:
    if not path.exists():
        return Rules()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    general = raw.get("general", {})
    thresholds = raw.get("thresholds", {})
    protected = raw.get("protected", {})
    windows = []
    for window in protected.get("windows", ["09:30-11:00"]):
        try:
            start_raw, end_raw = window.split("-", 1)
            windows.append((time.fromisoformat(start_raw.strip()), time.fromisoformat(end_raw.strip())))
        except ValueError as exc:
            raise ValueError(f"protected.windows 格式错误：{window!r}") from exc
    keywords = []
    for entry in raw.get("keywords", []):
        pattern = entry.get("pattern")
        weight = entry.get("weight")
        if not isinstance(pattern, str) or not isinstance(weight, int):
            raise ValueError(f"keywords 条目需要 pattern 和整数 weight：{entry!r}")
        try:
            keywords.append((re.compile(pattern, re.IGNORECASE), weight))
        except re.error as exc:
            raise ValueError(f"非法关键词正则 {pattern!r}: {exc}") from exc
    return Rules(
        timezone=general.get("timezone", "Asia/Shanghai"),
        window_hours=int(general.get("window_hours", 24)),
        brief_hour=int(general.get("brief_hour", 7)),
        max_actions=int(general.get("max_actions", 5)),
        focus_block_minutes=int(thresholds.get("focus_block_minutes", 90)),
        protect_min_salvage_minutes=int(thresholds.get("protect_min_salvage_minutes", 45)),
        tight_gap_minutes=int(thresholds.get("tight_gap_minutes", 10)),
        do_buffer_minutes=int(thresholds.get("do_buffer_minutes", 20)),
        default_keyword_weight=int(thresholds.get("default_keyword_weight", 2)),
        protected_windows=tuple(windows) if windows else DEFAULT_PROTECTED,
        keyword_weights=tuple(keywords) if keywords else DEFAULT_KEYWORDS,
    )


def _minutes(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def _merge(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[list[datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _free(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    free: list[tuple[datetime, datetime]] = []
    cursor = start
    for busy_start, busy_end in _merge(busy):
        if busy_end <= cursor or busy_start >= end:
            continue
        if busy_start > cursor:
            free.append((cursor, min(busy_start, end)))
        cursor = max(cursor, busy_end)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return [(a, b) for a, b in free if b > a]


def _weight(title: str, rules: Rules) -> tuple[int, str | None]:
    normalized = title.replace("：", ":")
    for pattern, weight in rules.keyword_weights:
        match = pattern.search(normalized)
        if match:
            return weight, match.group(0)
    return rules.default_keyword_weight, None


def _label(event: Event) -> str:
    return "忙碌块" if event.private else (event.title or "未命名事件")


def _day_bounds(day: date, rules: Rules) -> tuple[datetime, datetime]:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(rules.timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def _window_bounds(day: date, window: tuple[time, time], rules: Rules) -> tuple[datetime, datetime]:
    day_start, _ = _day_bounds(day, rules)
    start = datetime.combine(day, window[0], tzinfo=day_start.tzinfo)
    end = datetime.combine(day, window[1], tzinfo=day_start.tzinfo)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _previous_event(event: Event, events: list[Event]) -> Event | None:
    previous = [candidate for candidate in events if candidate.busy and candidate.end <= event.start]
    return max(previous, key=lambda candidate: candidate.end) if previous else None


def _per_event_actions(events: list[Event], rules: Rules) -> dict[tuple[str, datetime], Action]:
    actions: dict[tuple[str, datetime], Action] = {}
    timed = [event for event in events if not event.all_day and event.busy]

    for index, current in enumerate(timed):
        for previous in timed[:index]:
            if current.start < previous.end and current.end > previous.start:
                overlap = _minutes(max(current.start, previous.start), min(current.end, previous.end))
                key = (current.uid, current.start)
                actions[key] = Action(
                    tag="DECIDE",
                    title=f"「{_label(previous)}」与「{_label(current)}」重叠",
                    start=current.start,
                    end=current.end,
                    reason=Reason(
                        "overlap",
                        {
                            "a": _label(previous),
                            "b": _label(current),
                            "minutes": overlap,
                        },
                    ),
                )
                break
            gap = _minutes(previous.end, current.start)
            if gap < rules.tight_gap_minutes:
                key = (current.uid, current.start)
                actions[key] = Action(
                    tag="DECIDE",
                    title=f"「{_label(previous)}」与「{_label(current)}」之间只有 {gap} 分钟",
                    start=current.start,
                    end=current.end,
                    reason=Reason(
                        "tight_gap",
                        {"a": _label(previous), "b": _label(current), "minutes": gap},
                    ),
                )
                break

    for event in timed:
        weight, keyword = _weight(event.title, rules)
        duration = _minutes(event.start, event.end)
        if weight >= 3 and duration >= 30:
            key = (event.uid, event.start)
            if key in actions:
                continue
            previous = _previous_event(event, events)
            gap = _minutes(previous.end, event.start) if previous else rules.do_buffer_minutes
            if 10 <= gap < rules.do_buffer_minutes:
                title = f"「{_label(event)}」前留 {gap} 分钟准备"
                reason = Reason(
                    "do_buffer",
                    {
                        "event": _label(event),
                        "keyword": keyword or "",
                        "weight": weight,
                        "gap": gap,
                        "previous_end": previous.end.strftime("%H:%M") if previous else "",
                    },
                )
                actions[key] = Action("DO", title, event.start, event.end, reason)
                continue
        if previous := _previous_event(event, events):
            if (
                previous.location
                and event.location
                and previous.location != event.location
                and 10 <= _minutes(previous.end, event.start) < 20
            ):
                actions[(event.uid, event.start)] = Action(
                    tag="WATCH",
                    title=f"「{_label(previous)}」与「{_label(event)}」地点不同",
                    start=event.start,
                    end=event.end,
                    reason=Reason(
                        "commute",
                        {
                            "a": _label(previous),
                            "b": _label(event),
                            "gap": _minutes(previous.end, event.start),
                        },
                    ),
                )
                continue
        if weight < 3 and duration >= 120:
            actions[(event.uid, event.start)] = Action(
                tag="WATCH",
                title=f"「{_label(event)}」占用 {duration // 60} 小时",
                start=event.start,
                end=event.end,
                reason=Reason("long_low", {"event": _label(event), "duration": duration}),
            )

    for event in events:
        if not event.all_day:
            continue
        weight, keyword = _weight(event.title, rules)
        if weight >= 3:
            actions[(event.uid, event.start)] = Action(
                tag="WATCH",
                title=f"全天：{_label(event)}",
                start=event.start,
                end=event.end,
                reason=Reason(
                    "all_day",
                    {"event": _label(event), "keyword": keyword or "", "weight": weight},
                ),
            )
    return actions


def _protect_actions(events: list[Event], day: date, rules: Rules) -> tuple[list[Action], bool]:
    day_start, day_end = _day_bounds(day, rules)
    work_start = datetime.combine(day, WORKDAY_START, tzinfo=day_start.tzinfo)
    work_end = datetime.combine(day, WORKDAY_END, tzinfo=day_start.tzinfo)
    busy = [(event.start, event.end) for event in events if event.busy]
    actions: list[Action] = []
    for window in rules.protected_windows:
        window_start, window_end = _window_bounds(day, window, rules)
        free = _free(busy, window_start, window_end)
        if not free:
            continue
        longest = max(free, key=lambda pair: pair[1] - pair[0])
        length = _minutes(*longest)
        if length >= rules.focus_block_minutes:
            actions.append(
                Action(
                    tag="PROTECT",
                    title=f"守住 {window_start:%H:%M}–{window_end:%H:%M}",
                    start=window_start,
                    end=window_end,
                    reason=Reason("protect_free", {"minutes": length}),
                )
            )
        elif length >= rules.protect_min_salvage_minutes:
            actions.append(
                Action(
                    tag="PROTECT",
                    title=f"抢救 {longest[0]:%H:%M}–{longest[1]:%H:%M}",
                    start=longest[0],
                    end=longest[1],
                    reason=Reason(
                        "protect_occupied",
                        {"window": f"{window_start:%H:%M}–{window_end:%H:%M}", "minutes": length},
                    ),
                )
            )
    work_free = _free(busy, work_start, work_end)
    no_focus = not any(_minutes(a, b) >= rules.focus_block_minutes for a, b in work_free)
    return actions, no_focus


def _verdict(actions: list[Action], no_focus: bool, rules: Rules) -> str:
    if no_focus and not any(action.tag in {"PROTECT", "DECIDE", "DO"} for action in actions):
        return "今天没有完整专注块。至少给自己留 30 分钟。"
    if not actions:
        first = rules.protected_windows[0]
        return f"今天没有需要防御的事。把 {first[0]:%H:%M}–{first[1]:%H:%M} 留给自己。"
    top = actions[0]
    if top.tag == "PROTECT":
        return f"今天最重要的不是开会，是{top.title}。"
    if top.tag == "DECIDE":
        return f"今天有会要撞车，先处理「{top.title.split('」')[0].lstrip('「')}」。"
    if top.tag == "DO":
        return f"今天最值得准备的是「{top.reason.values.get('event', '')}」。"
    return f"今天有 {len(actions)} 件事值得看一眼。"


def _clip(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def analyze(events: list[Event], rules: Rules, day: date) -> Brief:
    protect, no_focus = _protect_actions(events, day, rules)
    per_event = list(_per_event_actions(events, rules).values())
    actions = protect + per_event
    actions.sort(key=lambda action: (TAG_PRIORITY[action.tag], action.start, action.end, action.title))
    actions = actions[: rules.max_actions]
    timeline = sorted(events, key=lambda event: (not event.all_day, event.start, event.title))
    return Brief(
        verdict=_clip(_verdict(actions, no_focus, rules)),
        actions=actions,
        timeline=timeline[:3],
        no_focus=no_focus,
    )


def format_reason(reason: Reason) -> str:
    values = reason.values
    if reason.kind == "protect_free":
        return f"这是今天唯一 ≥{values['minutes']} 分钟的连续空档。"
    if reason.kind == "protect_occupied":
        return f"{values['window']} 被占用，但仍剩 {values['minutes']} 分钟可抢救。"
    if reason.kind == "overlap":
        return f"「{values['a']}」与「{values['b']}」重叠 {values['minutes']} 分钟。"
    if reason.kind == "tight_gap":
        return f"「{values['a']}」与「{values['b']}」间隔 {values['minutes']} 分钟。"
    if reason.kind == "do_buffer":
        previous = f"，前一个事件 {values['previous_end']} 结束" if values.get("previous_end") else ""
        return f"标题含「{values['keyword']}」（权重 {values['weight']}）{previous}，间隔 {values['gap']} 分钟。"
    if reason.kind == "commute":
        return f"「{values['a']}」与「{values['b']}」地点不同，间隔 {values['gap']} 分钟。"
    if reason.kind == "all_day":
        return f"全天事件，标题含「{values['keyword']}」（权重 {values['weight']}）。"
    if reason.kind == "long_low":
        return f"{values['duration']} 分钟，但标题权重低。"
    return "值得看一眼。"
