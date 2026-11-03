from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from ..time_utils import utc_naive_to_local
from .types import PROJECT_COMPLETED, TASK_APPROVED


@dataclass(frozen=True)
class ReportFilter:
    start_date: date | None = None
    end_date: date | None = None
    project_id: int | None = None
    assignee_id: int | None = None
    model: str = ""


@dataclass(frozen=True)
class ReportTaskRow:
    project_id: int
    project_code: str
    project_name: str
    project_status: str
    priority: str
    task_id: int
    task_code: str
    task_status: str
    owner_name: str
    assignee_name: str
    model: str
    garment_name: str
    model_name: str
    final_quantity: int
    rework_count: int
    generated_image_count: int
    generation_cost: Decimal
    project_created_at: datetime
    claimed_at: datetime | None
    work_started_at: datetime | None
    submitted_at: datetime | None
    approved_at: datetime | None
    returned_at: datetime | None
    manual_image_count: int = 0
    provider_name: str = ""


@dataclass(frozen=True)
class SummaryMetrics:
    project_count: int
    task_count: int
    approved_task_count: int
    project_completion_rate: float
    task_completion_rate: float
    final_image_count: int
    rework_count: int
    rework_rate: float
    generated_image_count: int
    manual_image_count: int
    total_cost: Decimal
    cost_per_approved_task: Decimal
    cost_per_final_image: Decimal
    average_claim_wait_seconds: float
    average_production_seconds: float
    average_review_seconds: float
    average_end_to_end_seconds: float


@dataclass(frozen=True)
class GroupMetrics:
    label: str
    task_count: int
    approved_task_count: int
    final_image_count: int
    rework_count: int
    generated_image_count: int
    manual_image_count: int
    total_cost: Decimal


@dataclass(frozen=True)
class ReportData:
    rows: tuple[ReportTaskRow, ...]
    summary: SummaryMetrics
    by_user: tuple[GroupMetrics, ...]
    by_model: tuple[GroupMetrics, ...]
    by_date: tuple[GroupMetrics, ...]


def calculate_duration_seconds(
    start: datetime | None,
    end: datetime | None,
) -> float | None:
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds()


def aggregate_report(rows: list[ReportTaskRow]) -> ReportData:
    approved = [row for row in rows if row.task_status == TASK_APPROVED]
    project_count = len({row.project_id for row in rows})
    completed_project_count = len(
        {
            row.project_id
            for row in rows
            if row.project_status == PROJECT_COMPLETED
        }
    )
    rework_count = sum(row.rework_count for row in rows)
    total_cost = sum((row.generation_cost for row in rows), Decimal("0"))
    final_image_count = sum(row.final_quantity for row in approved)
    review_candidates = [
        row for row in rows if row.submitted_at is not None and row.approved_at is not None
    ]
    claim_waits = [
        value
        for row in rows
        if (value := calculate_duration_seconds(row.project_created_at, row.claimed_at))
        is not None
    ]
    production_times = [
        value
        for row in rows
        if (
            value := calculate_duration_seconds(
                row.work_started_at or row.claimed_at,
                row.submitted_at or row.approved_at,
            )
        )
        is not None
    ]
    review_times = [
        value
        for row in review_candidates
        if (
            value := calculate_duration_seconds(
                row.submitted_at,
                row.approved_at,
            )
        )
        is not None
    ]
    end_to_end_times = [
        value
        for row in approved
        if (
            value := calculate_duration_seconds(
                row.project_created_at,
                row.approved_at,
            )
        )
        is not None
    ]
    summary = SummaryMetrics(
        project_count=project_count,
        task_count=len(rows),
        approved_task_count=len(approved),
        project_completion_rate=(
            completed_project_count / project_count if project_count else 0.0
        ),
        task_completion_rate=len(approved) / len(rows) if rows else 0.0,
        final_image_count=final_image_count,
        rework_count=rework_count,
        rework_rate=rework_count / len(rows) if rows else 0.0,
        generated_image_count=sum(row.generated_image_count for row in rows),
        manual_image_count=sum(row.manual_image_count for row in rows),
        total_cost=total_cost,
        cost_per_approved_task=(
            total_cost / len(approved) if approved else Decimal("0")
        ),
        cost_per_final_image=(
            total_cost / final_image_count
            if final_image_count
            else Decimal("0")
        ),
        average_claim_wait_seconds=mean(claim_waits) if claim_waits else 0.0,
        average_production_seconds=mean(production_times) if production_times else 0.0,
        average_review_seconds=mean(review_times) if review_times else 0.0,
        average_end_to_end_seconds=mean(end_to_end_times) if end_to_end_times else 0.0,
    )
    return ReportData(
        rows=tuple(rows),
        summary=summary,
        by_user=_group(rows, lambda row: row.assignee_name or "未分配"),
        by_model=_group(
            rows,
            lambda row: (
                f"{row.provider_name} / {row.model}"
                if row.provider_name and row.model
                else row.provider_name or row.model or "未设置"
            ),
        ),
        by_date=_group(
            rows,
            lambda row: utc_naive_to_local(
                row.project_created_at
            ).date().isoformat(),
        ),
    )


def _group(rows: list[ReportTaskRow], key) -> tuple[GroupMetrics, ...]:
    grouped: dict[str, list[ReportTaskRow]] = defaultdict(list)
    for row in rows:
        grouped[str(key(row))].append(row)
    result: list[GroupMetrics] = []
    for label in sorted(grouped):
        items = grouped[label]
        result.append(
            GroupMetrics(
                label=label,
                task_count=len(items),
                approved_task_count=sum(
                    row.task_status == TASK_APPROVED for row in items
                ),
                final_image_count=sum(
                    row.final_quantity
                    for row in items
                    if row.task_status == TASK_APPROVED
                ),
                rework_count=sum(row.rework_count for row in items),
                generated_image_count=sum(
                    row.generated_image_count for row in items
                ),
                manual_image_count=sum(
                    row.manual_image_count for row in items
                ),
                total_cost=sum(
                    (row.generation_cost for row in items),
                    Decimal("0"),
                ),
            )
        )
    return tuple(result)


def export_report_excel(report: ReportData, path: Path) -> Path:
    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "汇总"
    summary_sheet.append(["指标", "数值"])
    summary = report.summary
    summary_rows = (
        ("总任务数", summary.project_count),
        ("子任务数", summary.task_count),
        ("已通过任务", summary.approved_task_count),
        ("总任务完成率", summary.project_completion_rate),
        ("子任务通过率", summary.task_completion_rate),
        ("最终图片数", summary.final_image_count),
        ("返工次数", summary.rework_count),
        ("返工率", summary.rework_rate),
        ("生成图片数", summary.generated_image_count),
        ("PS成品数", summary.manual_image_count),
        ("总成本", float(summary.total_cost)),
        ("每个通过任务成本", float(summary.cost_per_approved_task)),
        ("每张最终图成本", float(summary.cost_per_final_image)),
        ("平均领取等待（小时）", summary.average_claim_wait_seconds / 3600),
        ("平均制作耗时（小时）", summary.average_production_seconds / 3600),
        ("平均审核耗时（小时）", summary.average_review_seconds / 3600),
        ("平均端到端耗时（小时）", summary.average_end_to_end_seconds / 3600),
    )
    for row in summary_rows:
        summary_sheet.append(list(row))

    project_sheet = workbook.create_sheet("任务明细")
    project_sheet.append(
        [
            "任务编码",
            "货号",
            "总任务状态",
            "子任务编码",
            "子任务状态",
            "运营",
            "制作人员",
            "API 平台",
            "模型",
            "服装图",
            "模特图",
            "最终图片数",
            "返工次数",
            "生成图片数",
            "PS成品数",
            "生成成本",
            "任务创建时间",
            "领取时间",
            "提交验收时间",
            "验收通过时间",
        ]
    )
    for row in report.rows:
        project_sheet.append(
            [
                row.project_code,
                row.project_name,
                row.project_status,
                row.task_code,
                row.task_status,
                row.owner_name,
                row.assignee_name,
                row.provider_name or "自动/历史",
                row.model,
                row.garment_name,
                row.model_name,
                row.final_quantity,
                row.rework_count,
                row.generated_image_count,
                row.manual_image_count,
                float(row.generation_cost),
                _local_datetime(row.project_created_at),
                _local_datetime(row.claimed_at),
                _local_datetime(row.submitted_at),
                _local_datetime(row.approved_at),
            ]
        )

    _append_group_sheet(workbook, "人员绩效", report.by_user)
    _append_group_sheet(workbook, "成本明细", report.by_model)
    _append_group_sheet(workbook, "趋势", report.by_date)

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="315E9E")
        for column_cells in sheet.columns:
            width = min(
                40,
                max(10, max(len(str(cell.value or "")) for cell in column_cells) + 2),
            )
            sheet.column_dimensions[column_cells[0].column_letter].width = width

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


def _append_group_sheet(workbook: Workbook, title: str, rows: tuple[GroupMetrics, ...]) -> None:
    sheet = workbook.create_sheet(title)
    sheet.append(
        [
            "分组",
            "任务数",
            "已通过任务",
            "最终图片数",
            "返工次数",
            "生成图片数",
            "PS成品数",
            "总成本",
        ]
    )
    for row in rows:
        sheet.append(
            [
                row.label,
                row.task_count,
                row.approved_task_count,
                row.final_image_count,
                row.rework_count,
                row.generated_image_count,
                row.manual_image_count,
                float(row.total_cost),
            ]
        )


def _local_datetime(value: datetime | None) -> datetime | None:
    return utc_naive_to_local(value) if value is not None else None
