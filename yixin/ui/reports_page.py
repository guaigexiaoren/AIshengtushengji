from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.reports import (
    REPORT_DATE_COMPLETED,
    REPORT_DATE_CREATED,
    GroupMetrics,
    ReportData,
    ReportFilter,
    export_report_excel,
)
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.types import (
    ROLE_LABELS,
    TASK_STATUS_LABELS,
    AuthUser,
)
from ..constants import MODEL_OPTIONS
from ..time_utils import utc_naive_to_local
from .async_queries import get_query_runner
from .components import FilterBar, PageActionBar, PageHeader, SectionCard


class MetricCard(QFrame):
    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setObjectName("PageSubtitle")
        self.value_label = QLabel("—")
        self.value_label.setStyleSheet(
            "color:#1f2d43;font-size:22px;font-weight:700;"
        )
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)


class ReportTrendChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.groups: tuple[GroupMetrics, ...] = ()
        self.setMinimumHeight(210)

    def set_groups(self, groups: tuple[GroupMetrics, ...]) -> None:
        self.groups = groups
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f7f9fd"))
        available = self.rect().adjusted(48, 36, -18, -38)
        painter.setFont(QFont(self.font().family(), 9))
        painter.setPen(QPen(QColor("#8795aa"), 1))
        painter.drawText(16, 20, "每日产出趋势")
        painter.setBrush(QColor("#4778cc"))
        painter.drawRect(190, 12, 10, 10)
        painter.drawText(206, 22, "成品图")
        painter.setBrush(QColor("#78b7d8"))
        painter.drawRect(278, 12, 10, 10)
        painter.drawText(294, 22, "生成图")
        painter.drawLine(
            available.left(),
            available.bottom(),
            available.right(),
            available.bottom(),
        )
        if not self.groups:
            painter.setPen(QColor("#7d8ba0"))
            painter.drawText(
                available,
                Qt.AlignmentFlag.AlignCenter,
                "当前筛选范围暂无数据",
            )
            return
        maximum = max(
            max(group.final_image_count, group.generated_image_count)
            for group in self.groups
        ) or 1
        slot_width = available.width() / max(1, len(self.groups))
        bar_width = max(5.0, min(24.0, slot_width * 0.28))
        for index, group in enumerate(self.groups):
            center = available.left() + slot_width * (index + 0.5)
            for offset, count, color in (
                (-bar_width, group.final_image_count, "#4778cc"),
                (1, group.generated_image_count, "#78b7d8"),
            ):
                height = available.height() * count / maximum
                painter.fillRect(
                    int(center + offset),
                    int(available.bottom() - height),
                    max(1, int(bar_width)),
                    max(1, int(height)),
                    QColor(color),
                )
            painter.setPen(QColor("#4f5d72"))
            painter.drawText(
                int(center - slot_width / 2),
                available.bottom() + 6,
                int(slot_width),
                18,
                Qt.AlignmentFlag.AlignHCenter,
                group.label[5:] if len(group.label) >= 10 else group.label,
            )


class ReportsPage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        actor: AuthUser,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.actor = actor
        self.report_data: ReportData | None = None
        self.query_runner = get_query_runner()
        self._active_request = -1
        self.metric_cards: dict[str, MetricCard] = {}
        self._build()
        self.query_runner.succeeded.connect(self._query_succeeded)
        self.query_runner.failed.connect(self._query_failed)
        if auto_load:
            self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        outer.addWidget(
            PageHeader(
                "经营报告",
                "查看真实生成轮次、图片产出、人员效率与成本数据。",
            )
        )

        filter_bar = FilterBar()
        filters = filter_bar.layout
        self.preset_filter = QComboBox()
        self.preset_filter.addItem("近 30 天", "30d")
        self.preset_filter.addItem("今日", "today")
        self.preset_filter.addItem("近 7 天", "7d")
        self.preset_filter.addItem("本月", "month")
        self.preset_filter.addItem("自定义", "custom")
        self.start_date = QDateEdit(QDate.currentDate().addDays(-29))
        self.end_date = QDateEdit(QDate.currentDate())
        self.start_date.setCalendarPopup(True)
        self.end_date.setCalendarPopup(True)
        self.date_basis = QComboBox()
        self.date_basis.addItem("按完成时间", REPORT_DATE_COMPLETED)
        self.date_basis.addItem("按任务创建时间", REPORT_DATE_CREATED)
        self.project_filter = QComboBox()
        self.project_filter.setMinimumWidth(180)
        self.user_filter = QComboBox()
        self.user_filter.setMinimumWidth(130)
        self.model_filter = QComboBox()
        self.model_filter.addItem("全部模型", "")
        for label, model_id in MODEL_OPTIONS:
            self.model_filter.addItem(label, model_id)
        filters.addWidget(QLabel("日期"))
        filters.addWidget(self.preset_filter)
        filters.addWidget(self.start_date)
        filters.addWidget(QLabel("至"))
        filters.addWidget(self.end_date)
        filters.addWidget(QLabel("口径"))
        filters.addWidget(self.date_basis)
        filters.addWidget(QLabel("项目"))
        filters.addWidget(self.project_filter)
        filters.addWidget(QLabel("AI人员"))
        filters.addWidget(self.user_filter)
        filters.addWidget(self.model_filter)
        filters.addStretch(1)
        outer.addWidget(filter_bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)

        cards = QGridLayout()
        cards.setHorizontalSpacing(12)
        cards.setVerticalSpacing(12)
        for index, (key, title) in enumerate(
            (
                ("final_images", "成品图总数"),
                ("generated_images", "AI生成图总数"),
                ("completed_tasks", "完成任务数"),
                ("average_project_images", "平均每项目成品图"),
                ("active_users", "参与作图AI人数"),
                ("generation_success", "生成成功率"),
                ("failed_attempts", "失败轮次"),
                ("average_attempts", "平均生成轮次"),
                ("total_cost", "总成本"),
                ("cost_per_image", "每张成品图成本"),
                ("average_production", "平均制作耗时"),
                ("project_completion", "主任务完成率"),
            )
        ):
            card = MetricCard(title)
            self.metric_cards[key] = card
            cards.addWidget(card, index // 4, index % 4)
        content_layout.addLayout(cards)

        chart_card = SectionCard("每日产出趋势")
        self.trend_chart = ReportTrendChart()
        chart_card.layout.addWidget(self.trend_chart)
        content_layout.addWidget(chart_card)

        project_card = SectionCard("项目产出排行")
        self.project_table = self._table(
            [
                "项目",
                "主任务",
                "子任务",
                "完成",
                "成品图",
                "生成图",
                "失败轮次",
                "成功率",
                "成本",
                "平均制作",
            ]
        )
        for column, width in enumerate(
            (190, 70, 70, 70, 80, 80, 80, 80, 90, 100)
        ):
            self.project_table.setColumnWidth(column, width)
        project_card.layout.addWidget(self.project_table)
        content_layout.addWidget(project_card)

        user_card = SectionCard("AI人员排行")
        self.user_table = self._table(
            ["AI人员", "项目", "完成", "成品图", "生成图", "失败轮次", "成功率", "成本", "平均制作"]
        )
        for column, width in enumerate(
            (130, 70, 70, 80, 80, 80, 80, 90, 100)
        ):
            self.user_table.setColumnWidth(column, width)
        user_card.layout.addWidget(self.user_table)
        content_layout.addWidget(user_card)

        model_card = SectionCard("模型 / 供应商排行")
        self.model_table = self._table(
            ["平台 / 模型", "项目", "任务", "成功轮次", "失败轮次", "生成图", "成功率", "成本"]
        )
        for column, width in enumerate(
            (240, 70, 70, 80, 80, 80, 80, 90)
        ):
            self.model_table.setColumnWidth(column, width)
        model_card.layout.addWidget(self.model_table)
        content_layout.addWidget(model_card)

        detail_card = SectionCard("逐子任务明细")
        self.detail_table = self._table(
            [
                "任务",
                "项目",
                "货号",
                "子任务",
                "运营",
                "AI人员",
                "模型",
                "供应商",
                "状态",
                "成品图",
                "生成图",
                "生成轮次",
                "失败轮次",
                "成本",
                "创建时间",
                "完成时间",
            ]
        )
        for column, width in enumerate(
            (
                180,
                180,
                100,
                180,
                100,
                110,
                150,
                110,
                90,
                70,
                70,
                80,
                80,
                90,
                145,
                145,
            )
        ):
            self.detail_table.setColumnWidth(column, width)
        detail_card.layout.addWidget(self.detail_table)
        content_layout.addWidget(detail_card)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        action_bar = PageActionBar()
        self.status_label = QLabel("正在加载经营数据…")
        self.status_label.setObjectName("PageSubtitle")
        self.refresh_button = QPushButton("刷新数据")
        self.refresh_button.setObjectName("PrimaryButton")
        self.refresh_button.clicked.connect(self.refresh)
        self.export_button = QPushButton("导出 Excel")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_report)
        action_bar.layout.addWidget(self.status_label, 1)
        action_bar.layout.addWidget(self.export_button)
        action_bar.layout.addWidget(self.refresh_button)
        outer.addWidget(action_bar)

        self.preset_filter.currentIndexChanged.connect(
            self._preset_changed
        )
        self.start_date.dateChanged.connect(self._mark_custom)
        self.end_date.dateChanged.connect(self._mark_custom)

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        table.setMinimumHeight(190)
        return table

    def refresh(self) -> None:
        self._refresh_options()
        self._load_report()

    def _refresh_options(self) -> None:
        current_project = self.project_filter.currentData()
        current_user = self.user_filter.currentData()
        try:
            projects = self.repository.list_report_project_options(
                self.actor
            )
            users = self.repository.list_users()
        except Exception as exc:
            QMessageBox.warning(self, "读取筛选条件失败", str(exc))
            return
        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem("全部项目", (None, None))
        for scope, name in projects:
            scope_label = "内部" if scope == "internal" else "外部"
            self.project_filter.addItem(
                f"{scope_label} · {name}",
                (scope, name),
            )
        project_index = self.project_filter.findData(current_project)
        self.project_filter.setCurrentIndex(max(0, project_index))
        self.project_filter.blockSignals(False)

        self.user_filter.blockSignals(True)
        self.user_filter.clear()
        self.user_filter.addItem("全部人员", None)
        for user in users:
            self.user_filter.addItem(
                f"{user.display_name} · {ROLE_LABELS.get(user.role, user.role)}",
                user.id,
            )
        user_index = self.user_filter.findData(current_user)
        self.user_filter.setCurrentIndex(max(0, user_index))
        self.user_filter.blockSignals(False)

    def _preset_changed(self) -> None:
        preset = self.preset_filter.currentData()
        today = QDate.currentDate()
        ranges = {
            "today": (today, today),
            "7d": (today.addDays(-6), today),
            "30d": (today.addDays(-29), today),
            "month": (QDate(today.year(), today.month(), 1), today),
        }
        if preset in ranges:
            start, end = ranges[preset]
            self.start_date.blockSignals(True)
            self.end_date.blockSignals(True)
            self.start_date.setDate(start)
            self.end_date.setDate(end)
            self.start_date.blockSignals(False)
            self.end_date.blockSignals(False)

    def _mark_custom(self) -> None:
        if self.preset_filter.currentData() != "custom":
            self.preset_filter.blockSignals(True)
            self.preset_filter.setCurrentIndex(
                self.preset_filter.findData("custom")
            )
            self.preset_filter.blockSignals(False)

    def _load_report(self) -> None:
        if self.start_date.date() > self.end_date.date():
            QMessageBox.warning(
                self,
                "日期范围错误",
                "开始日期不能晚于结束日期。",
            )
            return
        scope, name = self.project_filter.currentData() or (None, None)
        report_filter = ReportFilter(
            start_date=self.start_date.date().toPython(),
            end_date=self.end_date.date().toPython(),
            date_basis=self.date_basis.currentData(),
            project_scope=scope,
            project_name=name,
            assignee_id=self.user_filter.currentData(),
            model=self.model_filter.currentData() or "",
        )
        self.refresh_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.status_label.setText("正在加载经营数据…")
        self._active_request = self.query_runner.submit(
            lambda: self.repository.report(self.actor, report_filter)
        )

    def _query_succeeded(self, request_id: int, result) -> None:
        if request_id != self._active_request:
            return
        self.refresh_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.report_data = result
        self.status_label.setText(
            f"已统计 {result.summary.project_count} 个主任务"
        )
        self._render_report(result)

    def _query_failed(self, request_id: int, message: str) -> None:
        if request_id != self._active_request:
            return
        self.refresh_button.setEnabled(True)
        self.export_button.setEnabled(self.report_data is not None)
        self.status_label.setText("加载失败，已保留上一次成功数据")
        QMessageBox.warning(self, "报表生成失败", message)

    def _render_report(self, report: ReportData) -> None:
        summary = report.summary
        values = {
            "final_images": f"{summary.final_image_count} 张",
            "generated_images": f"{summary.generated_image_count} 张",
            "completed_tasks": (
                f"{summary.approved_task_count} / {summary.task_count}"
            ),
            "average_project_images": (
                f"{summary.average_images_per_project:.1f} 张"
            ),
            "active_users": f"{summary.active_user_count} 人",
            "generation_success": f"{summary.generation_success_rate:.1%}",
            "failed_attempts": f"{summary.failed_attempt_count} 次",
            "average_attempts": (
                f"{summary.average_generation_attempts:.2f} 轮"
            ),
            "total_cost": f"¥{summary.total_cost:.2f}",
            "cost_per_image": (
                f"¥{summary.cost_per_final_image:.2f}"
            ),
            "average_production": (
                f"{summary.average_production_seconds / 3600:.2f} 小时"
            ),
            "project_completion": (
                f"{summary.project_completion_rate:.1%}"
            ),
        }
        for key, value in values.items():
            self.metric_cards[key].value_label.setText(value)
        self.trend_chart.set_groups(report.by_date)
        self._fill_project_table(self.project_table, report.by_project)
        self._fill_user_table(self.user_table, report.by_user)
        self._fill_model_table(self.model_table, report.by_model)
        self.detail_table.setRowCount(0)
        for item in report.rows:
            row = self.detail_table.rowCount()
            self.detail_table.insertRow(row)
            details = (
                item.project_code,
                f"{'内部' if item.project_scope == 'internal' else '外部'}"
                f" · {item.project_name}",
                item.product_code,
                item.task_code,
                item.owner_name,
                item.assignee_name or "未分配",
                item.model,
                item.provider_name or "未记录",
                TASK_STATUS_LABELS.get(item.task_status, item.task_status),
                str(item.final_quantity),
                str(item.generated_image_count),
                str(item.attempt_count),
                str(item.failed_attempt_count),
                f"¥{item.generation_cost:.2f}",
                utc_naive_to_local(item.project_created_at).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                (
                    utc_naive_to_local(item.approved_at).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if item.approved_at
                    else ""
                ),
            )
            for column, value in enumerate(details):
                self.detail_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )

    @staticmethod
    def _fill_project_table(
        table: QTableWidget,
        groups: tuple[GroupMetrics, ...],
    ) -> None:
        table.setRowCount(0)
        for group in groups:
            row = table.rowCount()
            table.insertRow(row)
            values = [
                group.label,
                str(group.project_count),
                str(group.task_count),
                str(group.approved_task_count),
                str(group.final_image_count),
                str(group.generated_image_count),
                str(group.failed_attempt_count),
                f"{group.generation_success_rate:.1%}",
                f"¥{group.total_cost:.2f}",
                f"{group.average_production_seconds / 3600:.2f} 小时",
            ]
            for column, value in enumerate(values[: table.columnCount()]):
                table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )

    @staticmethod
    def _fill_user_table(
        table: QTableWidget,
        groups: tuple[GroupMetrics, ...],
    ) -> None:
        table.setRowCount(0)
        for group in groups:
            row = table.rowCount()
            table.insertRow(row)
            values = (
                group.label,
                str(group.project_count),
                str(group.approved_task_count),
                str(group.final_image_count),
                str(group.generated_image_count),
                str(group.failed_attempt_count),
                f"{group.generation_success_rate:.1%}",
                f"¥{group.total_cost:.2f}",
                f"{group.average_production_seconds / 3600:.2f} 小时",
            )
            for column, value in enumerate(values):
                table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )

    @staticmethod
    def _fill_model_table(
        table: QTableWidget,
        groups: tuple[GroupMetrics, ...],
    ) -> None:
        table.setRowCount(0)
        for group in groups:
            row = table.rowCount()
            table.insertRow(row)
            values = (
                group.label,
                str(group.project_count),
                str(group.task_count),
                str(group.attempt_count - group.failed_attempt_count),
                str(group.failed_attempt_count),
                str(group.generated_image_count),
                f"{group.generation_success_rate:.1%}",
                f"¥{group.total_cost:.2f}",
            )
            for column, value in enumerate(values):
                table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )

    def _export_report(self) -> None:
        if self.report_data is None:
            return
        default_name = f"经营报告_{date.today():%Y%m%d}.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出经营报告",
            default_name,
            "Excel 工作簿 (*.xlsx)",
        )
        if not path:
            return
        try:
            export_report_excel(self.report_data, Path(path))
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "已导出", path)
