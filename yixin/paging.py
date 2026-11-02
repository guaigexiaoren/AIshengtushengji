from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Generic, TypeVar


T = TypeVar("T")
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True)
class PageRequest:
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    sort_field: str = "created_at"
    sort_direction: str = "desc"

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("页码必须大于 0")
        if self.page_size < 1:
            raise ValueError("每页数量必须大于 0")
        if self.sort_direction not in ("asc", "desc"):
            raise ValueError("排序方向必须是 asc 或 desc")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


@dataclass(frozen=True)
class PageResult(Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.total <= 0:
            return 1
        return (self.total + self.page_size - 1) // self.page_size


@dataclass(frozen=True)
class TaskQuery:
    keyword: str = ""
    statuses: tuple[str, ...] = ()
    start_date: date | None = None
    end_date: date | None = None


@dataclass(frozen=True)
class HistoryQuery:
    keyword: str = ""
    project_name: str | None = None
    status: str | None = None
    task_type: str | None = None
    start_date: date | None = None
    end_date: date | None = None
