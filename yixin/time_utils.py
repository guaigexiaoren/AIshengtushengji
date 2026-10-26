from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping


def local_to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def utc_naive_to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().replace(tzinfo=None)


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    minutes, second = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分{second:02d}秒"
    hours, minute = divmod(minutes, 60)
    return f"{hours}小时{minute:02d}分"


def task_elapsed_seconds(
    task: Mapping,
    *,
    now: datetime | None = None,
) -> float | None:
    created = parse_utc_timestamp(task.get("created_at"))
    if created is None:
        return None
    finished = parse_utc_timestamp(task.get("finished_at"))
    end = finished or now or datetime.now(timezone.utc)
    return max(0.0, (end - created).total_seconds())


def format_task_elapsed(
    task: Mapping,
    *,
    now: datetime | None = None,
) -> str:
    seconds = task_elapsed_seconds(task, now=now)
    return "—" if seconds is None else format_elapsed(seconds)
