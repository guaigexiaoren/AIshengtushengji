from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from ..time_utils import utc_naive_to_local
from .types import PROJECT_COMPLETED, PROJECT_SCOPE_LABELS, TASK_APPROVED


REPORT_DATE_COMPLETED = "completed"
REPORT_DATE_CREATED = "created"


@dataclass(frozen=True)
class ReportFilter:
    start_date: date | None = None
    end_date: date | None = None
    date_basis: str = REPORT_DATE_COMPLETED
    project_scope: str | None = None
    project_name: str | None = None
    project_id: int | None = None
    assignee_id: int | None = None
    model: str = ""


@dataclass(frozen=True)
class ReportTaskRow:
    project_id: int
    project_code: str
    project_scope: str
    project_name: str
    product_code: str
    project_status: str
    priority: str
    task_id: int
    task_code: str
    task_status: str
    assignee_id: int | None
    owner_name: str
    assignee_name: str
    model: str
    garment_name: str
    model_name: str
    final_quantity: int
    generated_image_count: int
    attempt_count: int
    successful_attempt_count: int
    failed_attempt_count: int
    generation_cost: Decimal
    project_created_at: datetime
    claimed_at: datetime | None
    work_started_at: datetime | None
    submitted_at: datetime | None
    approved_at: datetime | None
    provider_name: str = ""


@dataclass(frozen=True)
class SummaryMetrics:
    project_count: int
    task_count: int
    approved_task_count: int
    project_completion_rate: float
    task_completion_rate: float
    final_image_count: int
    average_images_per_project: float
    active_user_count: int
    generated_image_count: int
    attempt_count: int
    failed_attempt_count: int
    generation_success_rate: float
    average_generation_attempts: float
    total_cost: Decimal
    cost_per_final_image: Decimal
    average_claim_wait_seconds: float
    average_production_seconds: float
    average_end_to_end_seconds: float


@dataclass(frozen=True)
class GroupMetrics:
    key: str
    label: str
    project_count: int
    task_count: int
    approved_task_count: int
    final_image_count: int
    generated_image_count: int
    attempt_count: int
    failed_attempt_count: int
    generation_success_rate: float
    average_generation_attempts: float
    total_cost: Decimal
    average_production_seconds: float


@dataclass(frozen=True)
class ReportData:
    rows: tuple[ReportTaskRow, ...]
    summary: SummaryMetrics
    by_project: tuple[GroupMetrics, ...]
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


def aggregate_report(
    rows: list[ReportTaskRow],
    *,
    date_basis: str = REPORT_DATE_COMPLETED,
    by_model: tuple[GroupMetrics, ...] | None = None,
) -> ReportData:
    approved = [row for row in rows if row.task_status == TASK_APPROVED]
    project_count = len({row.project_id for row in rows})
    completed_project_count = len(
        {
            row.project_id
            for row in rows
            if row.project_status == PROJECT_COMPLETED
        }
    )
    total_cost = sum((row.generation_cost for row in rows), Decimal("0"))
    final_image_count = sum(row.final_quantity for row in approved)
    generated_image_count = sum(
        row.generated_image_count for row in rows
    )
    attempt_count = sum(row.attempt_count for row in rows)
    successful_attempt_count = sum(
        row.successful_attempt_count for row in rows
    )
    failed_attempt_count = sum(row.failed_attempt_count for row in rows)
    tasks_with_attempts = sum(row.attempt_count > 0 for row in rows)
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
        average_images_per_project=(
            final_image_count / project_count if project_count else 0.0
        ),
        active_user_count=len(
            {
                row.assignee_id
                for row in rows
                if row.assignee_id is not None
            }
        ),
        generated_image_count=generated_image_count,
        attempt_count=attempt_count,
        failed_attempt_count=failed_attempt_count,
        generation_success_rate=(
            successful_attempt_count / attempt_count
            if attempt_count
            else 0.0
        ),
        average_generation_attempts=(
            attempt_count / tasks_with_attempts
            if tasks_with_attempts
            else 0.0
        ),
        total_cost=total_cost,
        cost_per_final_image=(
            total_cost / final_image_count
            if final_image_count
            else Decimal("0")
        ),
        average_claim_wait_seconds=mean(claim_waits) if claim_waits else 0.0,
        average_production_seconds=mean(production_times) if production_times else 0.0,
        average_end_to_end_seconds=mean(end_to_end_times) if end_to_end_times else 0.0,
    )
    return ReportData(
        rows=tuple(rows),
        summary=summary,
        by_project=_group(
            rows,
            lambda row: (
                f"{row.project_scope}:{row.project_name}",
                f"{PROJECT_SCOPE_LABELS.get(row.project_scope, row.project_scope)}"
                f" · {row.project_name}",
            ),
        ),
        by_user=_group(
            rows,
            lambda row: (
                f"user:{row.assignee_id or 0}",
                row.assignee_name or "未分配",
            ),
        ),
        by_model=by_model or (),
        by_date=_group(
            rows,
            lambda row: _report_date(row, date_basis),
        ),
    )


def _report_date(
    row: ReportTaskRow,
    date_basis: str,
) -> tuple[str, str]:
    value = (
        row.approved_at
        if date_basis == REPORT_DATE_COMPLETED
        else row.project_created_at
    )
    label = utc_naive_to_local(value).date().isoformat() if value else ""
    return label, label


def _group(rows: list[ReportTaskRow], key) -> tuple[GroupMetrics, ...]:
    grouped: dict[str, tuple[str, list[ReportTaskRow]]] = {}
    for row in rows:
        group_key, label = key(row)
        grouped.setdefault(group_key, (label, []))[1].append(row)
    result: list[GroupMetrics] = []
    for group_key in sorted(grouped):
        label, items = grouped[group_key]
        approved = [
            row for row in items if row.task_status == TASK_APPROVED
        ]
        attempt_count = sum(row.attempt_count for row in items)
        successful_attempt_count = sum(
            row.successful_attempt_count for row in items
        )
        production_times = [
            value
            for row in items
            if (
                value := calculate_duration_seconds(
                    row.work_started_at or row.claimed_at,
                    row.submitted_at or row.approved_at,
                )
            )
            is not None
        ]
        result.append(
            GroupMetrics(
                key=group_key,
                label=label,
                project_count=len({row.project_id for row in items}),
                task_count=len(items),
                approved_task_count=len(approved),
                final_image_count=sum(
                    row.final_quantity for row in approved
                ),
                generated_image_count=sum(
                    row.generated_image_count for row in items
                ),
                attempt_count=attempt_count,
                failed_attempt_count=sum(
                    row.failed_attempt_count for row in items
                ),
                generation_success_rate=(
                    successful_attempt_count / attempt_count
                    if attempt_count
                    else 0.0
                ),
                average_generation_attempts=(
                    attempt_count
                    / sum(row.attempt_count > 0 for row in items)
                    if any(row.attempt_count > 0 for row in items)
                    else 0.0
                ),
                total_cost=sum(
                    (row.generation_cost for row in items),
                    Decimal("0"),
                ),
                average_production_seconds=(
                    mean(production_times)
                    if production_times
                    else 0.0
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
        ("平均每项目成品图数", summary.average_images_per_project),
        ("参与作图AI人数", summary.active_user_count),
        ("生成图片数", summary.generated_image_count),
        ("生成轮次", summary.attempt_count),
        ("失败轮次", summary.failed_attempt_count),
        ("生成成功率", summary.generation_success_rate),
        ("平均生成轮次", summary.average_generation_attempts),
        ("总成本", float(summary.total_cost)),
        ("每张最终图成本", float(summary.cost_per_final_image)),
        ("平均领取等待（小时）", summary.average_claim_wait_seconds / 3600),
        ("平均制作耗时（小时）", summary.average_production_seconds / 3600),
        ("平均端到端耗时（小时）", summary.average_end_to_end_seconds / 3600),
    )
    for row in summary_rows:
        summary_sheet.append(list(row))

    project_sheet = workbook.create_sheet("任务明细")
    project_sheet.append(
        [
            "任务编码",
            "项目",
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
            "生成图片数",
            "生成轮次",
            "失败轮次",
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
                row.product_code,
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
                row.generated_image_count,
                row.attempt_count,
                row.failed_attempt_count,
                float(row.generation_cost),
                _local_datetime(row.project_created_at),
                _local_datetime(row.claimed_at),
                _local_datetime(row.submitted_at),
                _local_datetime(row.approved_at),
            ]
        )

    _append_group_sheet(workbook, "项目排行", report.by_project)
    _append_group_sheet(workbook, "AI人员排行", report.by_user)
    _append_group_sheet(workbook, "供应商排行", report.by_model)
    _append_group_sheet(workbook, "每日趋势", report.by_date)

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
            "项目数",
            "任务数",
            "已通过任务",
            "最终图片数",
            "生成图片数",
            "生成轮次",
            "失败轮次",
            "成功率",
            "总成本",
            "平均制作耗时（小时）",
        ]
    )
    for row in rows:
        sheet.append(
            [
                row.label,
                row.project_count,
                row.task_count,
                row.approved_task_count,
                row.final_image_count,
                row.generated_image_count,
                row.attempt_count,
                row.failed_attempt_count,
                row.generation_success_rate,
                float(row.total_cost),
                row.average_production_seconds / 3600,
            ]
        )


def _local_datetime(value: datetime | None) -> datetime | None:
    return utc_naive_to_local(value) if value is not None else None
