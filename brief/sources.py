"""Single-source ICS loading, freshness and recurrence expansion.

v1 reads one source: a subscription URL when configured, otherwise an
exported .ics file. A successful 200 or 304 is fresh. Content timestamps
(DTSTAMP/LAST-MODIFIED) are NOT used as a staleness gate; a source with no
validator falls back to the last good snapshot and says so.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
import recurring_ical_events

RULE_TZ = ZoneInfo("Asia/Shanghai")
WINDOW_HOURS = 24
MAX_INSTANCES_PER_UID = 1000

# Common Windows TZIDs found in Outlook exports. zoneinfo cannot load these.
WINDOWS_TZ_TO_IANA = {
    "China Standard Time": "Asia/Shanghai",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "Pacific Standard Time": "America/Los_Angeles",
    "Mountain Standard Time": "America/Denver",
    "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "UTC": "UTC",
}

Fetch = Callable[[str, dict[str, str], int], "FetchResult"]


@dataclass(frozen=True)
class Event:
    """One expanded calendar instance, normalised to the rule timezone."""

    uid: str
    start: datetime
    end: datetime
    title: str
    location: str = ""
    all_day: bool = False
    status: str = "CONFIRMED"
    transp: str = "OPAQUE"
    partstat: str | None = None
    private: bool = False
    recurrence_id: str | None = None
    recurrence_range: str | None = None

    @property
    def busy(self) -> bool:
        return self.transp.upper() != "TRANSPARENT"

    @property
    def instance_key(self) -> tuple[str, str, str | None]:
        """(UID, RECURRENCE-ID-or-start, RANGE) — never UID alone."""
        recurrence = self.recurrence_id or self.start.isoformat()
        return (self.uid, recurrence, self.recurrence_range)


@dataclass(frozen=True)
class FetchResult:
    status: int
    body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class LoadResult:
    events: list[Event]
    ok: bool
    cached: bool = False
    source: str = ""
    warning: str | None = None
    fetched_at: str | None = None

    @property
    def usable(self) -> bool:
        return self.ok


_DEFAULT_FETCHER: Fetch | None = None


def _http_fetch(url: str, headers: dict[str, str], timeout: int) -> FetchResult:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return FetchResult(
                status=getattr(response, "status", 200),
                body=response.read(),
                headers={k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult(status=304)
        raise


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, payload: dict) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _resolve_tz(tzid: str | None) -> ZoneInfo:
    if not tzid:
        return RULE_TZ
    candidates = [tzid]
    mapped = WINDOWS_TZ_TO_IANA.get(tzid)
    if mapped:
        candidates.insert(0, mapped)
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue
    raise ZoneInfoNotFoundError(tzid)


def _prop_dt(prop) -> datetime | date:
    """Read a DTSTART/DTEND/RECURRENCE-ID value, mapping Windows TZIDs."""
    try:
        return prop.dt
    except Exception:
        raw = str(prop)
        tzid = prop.params.get("TZID")
        tz = _resolve_tz(tzid)
        for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=tz)
            except ValueError:
                continue
        raise


def _to_rule_tz(value: datetime | date) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=RULE_TZ), False
        return value.astimezone(RULE_TZ), False
    return datetime.combine(value, time.min, tzinfo=RULE_TZ), True


def _component_end(component, start: datetime, all_day: bool) -> datetime:
    end_prop = component.get("DTEND")
    if end_prop is not None:
        end_value = _prop_dt(end_prop)
        end, end_is_date = _to_rule_tz(end_value)
        if all_day and end_is_date:
            return end  # VALUE=DATE DTEND is exclusive; midnight is correct.
        return end
    duration = component.get("DURATION")
    if duration is not None:
        return start + duration.dt
    return start + timedelta(days=1) if all_day else start


def _attendee_partstat(component) -> str | None:
    attendees = component.get("ATTENDEE")
    if attendees is None:
        return None
    if not isinstance(attendees, list):
        attendees = [attendees]
    for attendee in attendees:
        partstat = attendee.params.get("PARTSTAT")
        if partstat:
            return str(partstat).upper()
    return None


def _is_private(component, title: str) -> bool:
    if str(component.get("CLASS", "")).upper() == "PRIVATE":
        return True
    return title.strip().lower() in {"", "busy", "忙碌"}


def _event_from_component(component) -> Event:
    uid = str(component.get("UID", "")).strip()
    if not uid:
        raise ValueError("VEVENT without UID")
    start_value = _prop_dt(component["DTSTART"])
    start, all_day = _to_rule_tz(start_value)
    end = _component_end(component, start, all_day)
    if end < start:
        end = start
    title = str(component.get("SUMMARY", "")).strip()
    private = _is_private(component, title)
    recurrence_prop = component.get("RECURRENCE-ID")
    recurrence_id = None
    recurrence_range = None
    if recurrence_prop is not None:
        recurrence_value = _prop_dt(recurrence_prop)
        recurrence_id = _to_rule_tz(recurrence_value)[0].isoformat()
        recurrence_range = recurrence_prop.params.get("RANGE")
    return Event(
        uid=uid,
        start=start,
        end=end,
        title="忙碌" if private else title,
        location=str(component.get("LOCATION", "")).strip(),
        all_day=all_day,
        status=str(component.get("STATUS", "CONFIRMED")).upper(),
        transp=str(component.get("TRANSP", "OPAQUE")).upper(),
        partstat=_attendee_partstat(component),
        private=private,
        recurrence_id=recurrence_id,
        recurrence_range=recurrence_range,
    )


def _eligible(event: Event) -> bool:
    if event.status == "CANCELLED":
        return False
    if event.partstat == "DECLINED":
        return False
    return True


def _iter_calendars(data: bytes) -> Iterable[icalendar.Calendar]:
    text = data.decode("utf-8", errors="replace")
    for part in text.split("END:VCALENDAR"):
        if "BEGIN:VCALENDAR" not in part:
            continue
        chunk = part[part.index("BEGIN:VCALENDAR") :] + "END:VCALENDAR\r\n"
        yield icalendar.Calendar.from_ical(chunk)


def parse_calendar(
    data: bytes,
    window_start: datetime,
    window_end: datetime,
    max_instances_per_uid: int = MAX_INSTANCES_PER_UID,
) -> tuple[list[Event], list[str]]:
    """Expand VEVENTs in [window_start, window_end), isolating bad events."""
    warnings: list[str] = []
    if b"END:VCALENDAR" not in data:
        raise ValueError("incomplete ICS: missing END:VCALENDAR")
    calendars = list(_iter_calendars(data))
    if not calendars:
        raise ValueError("no VCALENDAR found")
    events: list[Event] = []
    per_uid: dict[str, int] = {}

    for calendar in calendars:
        for component in calendar.walk("VEVENT"):
            try:
                sub = icalendar.Calendar()
                sub.add("VERSION", "2.0")
                sub.add_component(component)
                instances = recurring_ical_events.of(sub).between(window_start, window_end)
                for instance in instances:
                    event = _event_from_component(instance)
                    if not _eligible(event):
                        continue
                    count = per_uid.get(event.uid, 0)
                    if count >= max_instances_per_uid:
                        if count == max_instances_per_uid:
                            warnings.append(
                                f"{event.uid}: 超过 {max_instances_per_uid} 个实例，其余已跳过"
                            )
                        per_uid[event.uid] = count + 1
                        continue
                    per_uid[event.uid] = count + 1
                    events.append(event)
            except Exception as exc:  # one bad VEVENT must not abort the brief
                uid = str(component.get("UID", "?")).strip() or "?"
                warnings.append(f"{uid}: 无法解析（{type(exc).__name__}: {exc}）")

    events.sort(key=lambda event: (event.start, event.end, event.title))
    return events, warnings


def load_source(
    *,
    url: str | None,
    path: Path | None,
    state_dir: Path,
    now: datetime,
    window_hours: int = WINDOW_HOURS,
    fetcher: Fetch | None = None,
    timeout: int = 20,
) -> LoadResult:
    """Load one ICS source with last-good-snapshot fallback."""
    if not url and not path:
        return LoadResult(events=[], ok=False, warning="未配置日历来源")

    state_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = state_dir / "source.ics"
    meta_path = state_dir / "source.json"
    meta = _read_json(meta_path)
    window_start = now
    window_end = now + timedelta(hours=window_hours)
    source = url or str(path)

    data: bytes | None = None
    cached = False
    warning: str | None = None

    if url:
        headers = {"User-Agent": "today-brief/0.1"}
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
        try:
            result = (fetcher or _http_fetch)(url, headers, timeout)
            if result.status == 304 and snapshot_path.exists():
                data = snapshot_path.read_bytes()
            elif result.status == 200 and result.body is not None:
                data = result.body
                try:
                    parse_calendar(data, window_start, window_end)
                except Exception as exc:
                    raise ValueError(f"返回内容无法解析：{exc}") from exc
                _atomic_write(snapshot_path, data)
                _atomic_write_json(
                    meta_path,
                    {
                        "etag": result.headers.get("etag"),
                        "last_modified": result.headers.get("last-modified"),
                        "fetched_at": datetime.now(RULE_TZ).isoformat(),
                    },
                )
            else:
                raise ValueError(f"意外的 HTTP 状态：{result.status}")
        except Exception as exc:
            if snapshot_path.exists():
                data = snapshot_path.read_bytes()
                cached = True
                warning = f"抓取失败，使用 {meta.get('fetched_at', '上次')} 的缓存：{exc}"
            else:
                return LoadResult(events=[], ok=False, source=source, warning=f"抓取失败且无缓存：{exc}")
    else:
        assert path is not None
        try:
            data = path.read_bytes()
        except OSError as exc:
            if snapshot_path.exists():
                data = snapshot_path.read_bytes()
                cached = True
                warning = f"读取失败，使用上次缓存：{exc}"
            else:
                return LoadResult(events=[], ok=False, source=source, warning=f"读取失败：{exc}")

    if data is None:
        return LoadResult(events=[], ok=False, source=source, warning="没有可用数据")

    try:
        events, warnings = parse_calendar(data, window_start, window_end)
    except Exception as exc:
        if snapshot_path.exists() and not cached:
            events, warnings = parse_calendar(snapshot_path.read_bytes(), window_start, window_end)
            cached = True
            warning = f"解析失败，使用缓存：{exc}"
        else:
            return LoadResult(events=[], ok=False, source=source, warning=f"解析失败：{exc}")

    if warnings:
        extra = f"{len(warnings)} 个事件无法解析或超出上限"
        warning = f"{warning}；{extra}" if warning else extra
    return LoadResult(
        events=events,
        ok=True,
        cached=cached,
        source=source,
        warning=warning,
        fetched_at=meta.get("fetched_at"),
    )
