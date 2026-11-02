from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    ATTEMPT_CANCELLED,
    ATTEMPT_FAILED,
    ATTEMPT_QUEUED,
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    ROLE_ADMIN,
    AuthUser,
)
from ..constants import (
    MODE_LABELS,
    MODEL_LABELS,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCESS,
)
from ..database import Database
from ..library import LibraryService
from ..local_export import export_original_image
from ..settings import (
    LOCAL_SAVE_FIXED,
    LOCAL_SAVE_REFERENCE,
    AppSettings,
)
from ..storage import StoredImage
from ..time_utils import utc_naive_to_local
from ..worker import TaskWorker
from .date_filter import OptionalDateEdit
from .save_helpers import show_saved_path, show_saved_toast
from .widgets import ImageView, build_task_assets


HISTORY_KEY_ROLE = int(Qt.ItemDataRole.UserRole) + 10
HISTORY_RECORD_ROLE = int(Qt.ItemDataRole.UserRole) + 11


STATUS_LABELS = {
    TASK_QUEUED: "排队中",
    ATTEMPT_QUEUED: "排队中",
    TASK_RUNNING: "生成中",
    ATTEMPT_RUNNING: "生成中",
    TASK_SUCCESS: "成功",
    ATTEMPT_SUCCEEDED: "成功",
    TASK_FAILED: "失败",
    ATTEMPT_FAILED: "失败",
    TASK_CANCELLED: "已取消",
    ATTEMPT_CANCELLED: "已取消",
    "manual_ps": "PS成品",
}


@dataclass(frozen=True)
class HistoryRecord:
    kind: str
    record_id: int
    task_id: int
    attempt_id: int | None
    status: str
    created_at: datetime
    project_name: str
    product_code: str
    main_code: str
    secondary_code: str
    operator_name: str
    model: str
    provider_name: str
    prompt: str
    result_count: int
    raw: object

    @property
    def kind_label(self) -> str:
        return "项目" if self.kind == "project" else "普通"

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.record_id}"


class ClickableImageView(ImageView):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.current_path is not None
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class LargeImageDialog(QDialog):
    def __init__(self, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("查看大图")
        self.resize(1100, 800)
        self._pixmap = QPixmap(str(path))
        self._scale = 1.0
        self._fit_pending = True
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.zoom_in_button = QPushButton("放大")
        self.zoom_out_button = QPushButton("缩小")
        self.fit_button = QPushButton("适应窗口")
        self.actual_button = QPushButton("100%")
        self.scale_label = QLabel("100%")
        toolbar.addWidget(self.zoom_in_button)
        toolbar.addWidget(self.zoom_out_button)
        toolbar.addWidget(self.fit_button)
        toolbar.addWidget(self.actual_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.scale_label)
        layout.addLayout(toolbar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.image_label = QLabel()
        self.scroll.setWidget(self.image_label)
        layout.addWidget(self.scroll, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.accept)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self.zoom_in_button.clicked.connect(lambda: self._zoom_by(1.2))
        self.zoom_out_button.clicked.connect(lambda: self._zoom_by(1 / 1.2))
        self.fit_button.clicked.connect(self._fit_to_window)
        self.actual_button.clicked.connect(lambda: self._set_scale(1.0))
        self._render()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._fit_pending:
            self._fit_pending = False
            self._fit_to_window()

    def wheelEvent(self, event) -> None:
        self._zoom_by(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
        event.accept()

    def _zoom_by(self, factor: float) -> None:
        self._set_scale(self._scale * factor)

    def _set_scale(self, value: float) -> None:
        self._scale = max(0.1, min(4.0, value))
        self._render()

    def _fit_to_window(self) -> None:
        if self._pixmap.isNull():
            return
        available = self.scroll.viewport().size()
        ratio = min(
            available.width() / max(1, self._pixmap.width()),
            available.height() / max(1, self._pixmap.height()),
        )
        self._set_scale(ratio)

    def _render(self) -> None:
        if self._pixmap.isNull():
            self.image_label.setText("图片无法读取")
            return
        width = max(1, round(self._pixmap.width() * self._scale))
        height = max(1, round(self._pixmap.height() * self._scale))
        self.image_label.setPixmap(
            self._pixmap.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.image_label.resize(width, height)
        self.scale_label.setText(f"{round(self._scale * 100)}%")


class UnifiedHistoryPage(QWidget):
    library_changed = Signal()
    retry_requested = Signal(int)
    project_retry_requested = Signal(int, int)
    project_attempt_cancelled = Signal(int)

    def __init__(
        self,
        database: Database,
        library_service: LibraryService,
        worker: TaskWorker,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.library_service = library_service
        self.worker = worker
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.app_settings = AppSettings(database)
        self.records: list[HistoryRecord] = []
        self._records_by_key: dict[str, HistoryRecord] = {}
        self.current_record: HistoryRecord | None = None
        self.current_assets: list = []
        self.current_selected_asset = None
        self._build()
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)

        left = QFrame()
        left.setObjectName("Card")
        left_layout = QVBoxLayout(left)
        filters = QHBoxLayout()
        self.project_filter = QComboBox()
        self.project_filter.addItem("全部项目", None)
        self.product_search = QLineEdit()
        self.product_search.setPlaceholderText("搜索货号")
        self.status_filter = QComboBox()
        self.status_filter.addItem("全部状态", None)
        for value, label in (
            (ATTEMPT_QUEUED, "排队中"),
            (ATTEMPT_RUNNING, "生成中"),
            (ATTEMPT_SUCCEEDED, "成功"),
            (ATTEMPT_FAILED, "失败"),
            (ATTEMPT_CANCELLED, "已取消"),
            ("manual_ps", "PS成品"),
        ):
            self.status_filter.addItem(label, value)
        self.type_filter = QComboBox()
        self.type_filter.addItem("全部类型", None)
        self.type_filter.addItem("项目", "project")
        self.type_filter.addItem("普通", "ordinary")
        self.start_date = OptionalDateEdit("开始日期")
        self.end_date = OptionalDateEdit("结束日期")
        reset = QPushButton("重置筛选")
        refresh = QPushButton("刷新")
        filters.addWidget(self.project_filter, 1)
        filters.addWidget(self.product_search, 1)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.type_filter)
        filters.addWidget(self.start_date)
        filters.addWidget(self.end_date)
        filters.addWidget(reset)
        filters.addWidget(refresh)
        left_layout.addLayout(filters)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "任务类型",
                "项目 / 任务",
                "子任务 / 模式",
                "货号 / 标题",
                "操作人员",
                "模型",
                "状态",
                "创建时间",
                "结果数",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate(
            (55, 170, 170, 100, 80, 100, 70, 120, 50)
        ):
            self.table.setColumnWidth(column, width)
        left_layout.addWidget(self.table, 1)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("Card")
        right_layout = QVBoxLayout(right)
        self.detail_title = QLabel("选择一条历史记录")
        self.detail_title.setStyleSheet(
            "font-size:18px;font-weight:700;"
        )
        self.detail_meta = QLabel()
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setStyleSheet("color:#60708a;")
        self.prompt_view = QTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setMaximumHeight(145)
        right_layout.addWidget(self.detail_title)
        right_layout.addWidget(self.detail_meta)
        right_layout.addWidget(self.prompt_view)

        self.thumbnail_title = QLabel("参考图与结果图")
        self.combined_strip = self._image_strip()
        right_layout.addWidget(self.thumbnail_title)
        right_layout.addWidget(self.combined_strip)

        self.preview = ClickableImageView()
        right_layout.addWidget(self.preview, 1)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("取消任务")
        self.retry_button = QPushButton("重试任务")
        self.save_local_button = QPushButton("保存本地")
        self.save_local_button.setStyleSheet(
            "QPushButton { background:#2e64c4;color:white;"
            "border:1px solid #2455aa;border-radius:5px;"
            "padding:6px 12px;font-weight:600; }"
            "QPushButton:disabled { background:#aebad0;color:#eef2f8; }"
        )
        self.save_library_button = QPushButton("保存作品库")
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.retry_button)
        actions.addStretch(1)
        actions.addWidget(self.save_local_button)
        actions.addWidget(self.save_library_button)
        right_layout.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([900, 600])

        refresh.clicked.connect(self.refresh)
        reset.clicked.connect(self._reset_filters)
        self.project_filter.currentIndexChanged.connect(self._apply_filters)
        self.product_search.textChanged.connect(self._apply_filters)
        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        self.type_filter.currentIndexChanged.connect(self._apply_filters)
        self.start_date.dateChanged.connect(self._apply_filters)
        self.end_date.dateChanged.connect(self._apply_filters)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.combined_strip.currentRowChanged.connect(
            self._show_combined_asset
        )
        self.preview.clicked.connect(self._zoom_current)
        self.cancel_button.clicked.connect(self._cancel_current)
        self.retry_button.clicked.connect(self._retry_current)
        self.save_local_button.clicked.connect(self._save_local)
        self.save_library_button.clicked.connect(self._save_library)
        self._set_actions(False)

    @staticmethod
    def _image_strip():
        from .widgets import ImageStrip

        return ImageStrip(icon_size=72)

    def refresh(self) -> None:
        shared_root = self.repository.get_shared_setting("share_root")
        if shared_root:
            self.resolver = SharedPathResolver(shared_root)
        project_rows = []
        try:
            project_rows = self.repository.list_project_attempt_history(
                self.actor
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取项目历史失败", str(exc))
        ordinary_tasks = self.database.list_tasks(limit=5000)
        records = [
            self._project_record(row) for row in project_rows
        ]
        records.extend(
            self._ordinary_record(task) for task in ordinary_tasks
        )
        self.records = sorted(
            records,
            key=lambda item: item.created_at,
            reverse=True,
        )
        self._refresh_project_filter()
        self._apply_filters()

    def _project_record(self, row: dict) -> HistoryRecord:
        created = utc_naive_to_local(row["created_at"])
        return HistoryRecord(
            kind="project",
            record_id=int(row["id"]),
            task_id=int(row["task_id"]),
            attempt_id=int(row["id"]),
            status=str(row["status"]),
            created_at=created,
            project_name=str(
                row.get("access_project_name")
                or row.get("project_name")
                or row.get("product_code")
                or ""
            ),
            product_code=str(row.get("product_code") or ""),
            main_code=str(row.get("project_code") or ""),
            secondary_code=str(row.get("task_code") or ""),
            operator_name=str(row.get("operator_name") or ""),
            model=str(row.get("model") or ""),
            provider_name=str(row.get("provider_name") or ""),
            prompt=str(row.get("prompt_text") or ""),
            result_count=int(row.get("result_count") or 0),
            raw=row,
        )

    def _ordinary_record(self, task: dict) -> HistoryRecord:
        created = datetime.fromisoformat(str(task["created_at"]))
        created = utc_naive_to_local(created)
        prompt = str(task.get("prompt") or "")
        title = prompt.strip().splitlines()[0][:80] if prompt else "未命名创作"
        return HistoryRecord(
            kind="ordinary",
            record_id=int(task["id"]),
            task_id=int(task["id"]),
            attempt_id=None,
            status=str(task["status"]),
            created_at=created,
            project_name="普通创作",
            product_code=title,
            main_code=f"任务 #{task['id']}",
            secondary_code=MODE_LABELS.get(
                task["mode"],
                task["mode"],
            ),
            operator_name="本机",
            model=str(task.get("model") or ""),
            provider_name="自动/历史",
            prompt=prompt,
            result_count=len(self.database.get_task_results(task["id"])),
            raw=task,
        )

    def _refresh_project_filter(self) -> None:
        selected = self.project_filter.currentData()
        names = sorted({
            record.project_name
            for record in self.records
            if record.kind == "project" and record.project_name
        })
        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem("全部项目", None)
        for name in names:
            self.project_filter.addItem(name, name)
        index = self.project_filter.findData(selected)
        self.project_filter.setCurrentIndex(max(0, index))
        self.project_filter.blockSignals(False)

    def _reset_filters(self) -> None:
        self.project_filter.setCurrentIndex(0)
        self.product_search.clear()
        self.status_filter.setCurrentIndex(0)
        self.type_filter.setCurrentIndex(0)
        self.start_date.clear()
        self.end_date.clear()
        self._apply_filters()

    def _apply_filters(self, *_args) -> None:
        project_name = self.project_filter.currentData()
        product = self.product_search.text().strip().casefold()
        status = self.status_filter.currentData()
        kind = self.type_filter.currentData()
        start = self.start_date.python_date()
        end = self.end_date.python_date()
        rows = []
        for record in self.records:
            if kind and record.kind != kind:
                continue
            if project_name and (
                record.kind != "project"
                or record.project_name != project_name
            ):
                continue
            if product and product not in record.product_code.casefold():
                continue
            if status and record.status not in (
                {status, TASK_SUCCESS}
                if status == ATTEMPT_SUCCEEDED
                else {status}
            ):
                continue
            created = record.created_at.date()
            if start is not None and created < start:
                continue
            if end is not None and created > end:
                continue
            rows.append(record)
        self._records_by_key = {record.key: record for record in rows}
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for record in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                record.kind_label,
                record.main_code,
                record.secondary_code,
                record.product_code,
                record.operator_name,
                MODEL_LABELS.get(record.model, record.model),
                record.status_label,
                record.created_at.strftime("%Y-%m-%d %H:%M"),
                str(record.result_count),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(HISTORY_KEY_ROLE, record.key)
                    item.setData(HISTORY_RECORD_ROLE, record)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        if self.table.rowCount():
            self.table.selectRow(0)
        else:
            self.current_record = None
            self.detail_title.setText("没有符合条件的历史记录")
            self.detail_meta.clear()
            self.prompt_view.clear()
            self.combined_strip.clear()
            self.preview.set_image(None)
            self._set_actions(False)

    def _selection_changed(self) -> None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        record = (
            item.data(HISTORY_RECORD_ROLE)
            if item is not None
            else None
        )
        if record is None:
            return
        self.current_record = record
        self._load_record(record)

    def _load_record(self, record: HistoryRecord) -> None:
        self.current_assets = []
        if record.kind == "project":
            task = self.repository.get_task(record.task_id)
            if task is None:
                return
            references = [
                StoredImage(
                    path=self.resolver.resolve_project_path(
                        task.project_relative_root,
                        asset.relative_path,
                    ),
                    content_hash=asset.content_hash,
                    mime=asset.mime,
                    width=asset.width,
                    height=asset.height,
                    original_name=asset.original_name,
                )
                for asset in task.garment_assets
            ]
            references.append(
                StoredImage(
                    path=self.resolver.resolve_project_path(
                        task.project_relative_root,
                        task.model_path,
                    ),
                    content_hash=task.model_hash,
                    mime="image/png",
                    width=0,
                    height=0,
                    original_name=task.model_name,
                )
            )
            results = []
            for result in self.repository.list_attempt_results(
                int(record.attempt_id)
            ):
                path = self.resolver.resolve_project_path(
                    task.project_relative_root,
                    result.relative_path,
                )
                results.append(
                    {
                        "path": path,
                        "thumbnail_path": path,
                        "result": result,
                    }
                )
            self.current_assets = build_task_assets(references, results)
            provider_text = record.provider_name or "自动/历史"
            requested_provider = str(
                record.raw.get("requested_provider_name") or ""
            )
            if requested_provider and requested_provider != provider_text:
                provider_text += f"（首选 {requested_provider}）"
            meta = (
                f"{record.kind_label} · {record.project_name} · "
                f"{record.product_code}\n"
                f"项目 {record.main_code} · 子任务 {record.secondary_code}\n"
                f"模型 {MODEL_LABELS.get(record.model, record.model)} · "
                f"供应商 {provider_text} · "
                f"状态 {record.status_label}\n"
                f"宽高比 {record.raw.get('aspect_ratio') or '自动'} · "
                f"分辨率 {record.raw.get('resolution') or '自动'} · "
                f"数量 {record.raw.get('n') or 0}"
            )
        else:
            task_id = int(record.task_id)
            self.current_assets = build_task_assets(
                self.database.get_task_references(task_id),
                self.database.get_task_results(task_id),
            )
            meta = (
                f"普通创作 · {record.secondary_code} · "
                f"{MODEL_LABELS.get(record.model, record.model)}\n"
                f"{record.main_code} · 状态 {record.status_label}\n"
                f"宽高比 {record.raw.get('aspect_ratio') or '自动'} · "
                f"分辨率 {record.raw.get('resolution') or '自动'} · "
                f"数量 {record.raw.get('n') or 0}"
            )
        self.detail_title.setText(record.main_code)
        self.detail_meta.setText(meta)
        self.prompt_view.setPlainText(record.prompt)
        refs = [asset for asset in self.current_assets if asset.is_reference]
        results = [asset for asset in self.current_assets if asset.is_result]
        self.thumbnail_title.setText(
            f"参考图（{len(refs)}） · 结果图（{len(results)}）"
        )
        ordered_assets = [*refs, *results]
        self.combined_strip.set_images(
            [
                (asset.thumbnail_path, asset.label, asset)
                for asset in ordered_assets
            ]
        )
        if results:
            target_row = len(refs)
            self.combined_strip.setCurrentRow(target_row)
            self._show_combined_asset(target_row)
        elif refs:
            self.combined_strip.setCurrentRow(0)
            self._show_combined_asset(0)
        else:
            self.current_selected_asset = None
            self.preview.set_image(None)
        self._update_actions(record)

    def _show_combined_asset(self, row: int) -> None:
        refs = [
            asset
            for asset in self.current_assets
            if asset.is_reference
        ]
        results = [
            asset
            for asset in self.current_assets
            if asset.is_result
        ]
        assets = [*refs, *results]
        if not 0 <= row < len(assets):
            return
        asset = assets[row]
        self.current_selected_asset = asset
        self.preview.set_image(asset.path)
        self.save_local_button.setEnabled(asset.is_result)
        self.save_library_button.setEnabled(asset.is_result)

    def _update_actions(self, record: HistoryRecord) -> None:
        if record.kind == "ordinary":
            cancel = record.status in (TASK_QUEUED, TASK_RUNNING)
            retry = record.status in (
                TASK_SUCCESS,
                TASK_FAILED,
                TASK_CANCELLED,
            )
        else:
            owns_attempt = bool(
                self.actor.role == ROLE_ADMIN
                or int(record.raw.get("operator_id") or 0)
                == self.actor.id
            )
            cancel = (
                record.status == ATTEMPT_QUEUED
                and owns_attempt
            )
            task = self.repository.get_task(record.task_id)
            retry = bool(
                task
                and record.status
                not in (ATTEMPT_QUEUED, ATTEMPT_RUNNING)
                and (
                    self.actor.role == ROLE_ADMIN
                    or task.assignee_id == self.actor.id
                )
            )
        self.cancel_button.setEnabled(cancel)
        self.retry_button.setEnabled(retry)
        has_result = any(
            asset.is_result for asset in self.current_assets
        )
        self.save_local_button.setEnabled(has_result)
        self.save_library_button.setEnabled(has_result)

    def _set_actions(self, enabled: bool) -> None:
        for button in (
            self.cancel_button,
            self.retry_button,
            self.save_local_button,
            self.save_library_button,
        ):
            button.setEnabled(enabled)

    def _cancel_current(self) -> None:
        record = self.current_record
        if record is None:
            return
        if record.kind == "ordinary":
            if record.status == TASK_RUNNING:
                answer = QMessageBox.warning(
                    self,
                    "停止等待",
                    "停止等待不会保证服务端终止请求，仍可能产生费用。\n"
                    "是否继续？",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.worker.request_cancel(record.task_id)
        else:
            try:
                self.repository.cancel_queued_generation_attempt(
                    int(record.attempt_id),
                    self.actor,
                )
            except Exception as exc:
                QMessageBox.warning(self, "取消失败", str(exc))
                return
            self.project_attempt_cancelled.emit(int(record.attempt_id))
        self.refresh()

    def _retry_current(self) -> None:
        record = self.current_record
        if record is None:
            return
        if record.kind == "ordinary":
            self.retry_requested.emit(record.task_id)
            return
        self.project_retry_requested.emit(
            record.task_id,
            int(record.attempt_id),
        )

    def _save_local(self) -> None:
        record = self.current_record
        asset = self.current_selected_asset
        if record is None or asset is None or not asset.is_result:
            return
        if record.kind == "ordinary":
            from ..local_export import LocalImageExporter

            exporter = LocalImageExporter(
                self.database,
                self.app_settings,
            )
            result = asset.payload
            directory = exporter.preferred_directory(record.task_id)
            if directory is None:
                directory_text = QFileDialog.getExistingDirectory(
                    self,
                    "选择保存目录",
                    str(self.app_settings.last_folder() or Path.home()),
                )
                if not directory_text:
                    return
                directory = Path(directory_text)
            saved = exporter.save_result(
                record.task_id,
                int(result["id"]),
                directory,
            )
            show_saved_toast(self, saved)
            return
        directory = self._project_save_directory()
        if directory is None:
            return
        result = asset.payload["result"]
        saved = export_original_image(
            asset.path,
            directory,
            task_code=record.secondary_code,
            result_index=result.result_index,
        )
        show_saved_toast(self, saved)

    def _project_save_directory(self) -> Path | None:
        mode = self.app_settings.local_save_mode()
        if mode == LOCAL_SAVE_FIXED:
            folder = self.app_settings.fixed_folder()
            if folder and folder.is_dir():
                return folder
        if mode == LOCAL_SAVE_REFERENCE:
            reference = next(
                (
                    asset
                    for asset in self.current_assets
                    if asset.is_reference
                ),
                None,
            )
            if reference is not None:
                folder = Path(reference.path).parent.parent / "AI图"
                folder.mkdir(parents=True, exist_ok=True)
                return folder
        last = self.app_settings.last_folder()
        if last and last.is_dir():
            return last
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择保存目录",
            str(Path.home()),
        )
        if not directory:
            return None
        folder = Path(directory)
        self.app_settings.set_last_folder(folder)
        return folder

    def _save_library(self) -> None:
        record = self.current_record
        asset = self.current_selected_asset
        if record is None or asset is None or not asset.is_result:
            return
        try:
            if record.kind == "ordinary":
                result = asset.payload
                work_id = self.library_service.save_task_result(
                    record.task_id,
                    int(result["id"]),
                )
            else:
                result = asset.payload["result"]
                work_id = self.library_service.save_project_result(
                    repository=self.repository,
                    resolver=self.resolver,
                    task_id=record.task_id,
                    attempt_id=int(record.attempt_id),
                    result=result,
                )
            folder = self.library_service.work_folder(work_id)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.library_changed.emit()
        show_saved_path(
            self,
            title="已保存到作品库",
            message="图片、参考图和生成信息已保存。",
            path=folder,
        )

    def _zoom_current(self) -> None:
        if self.preview.current_path is None:
            return
        self._zoom_dialog = LargeImageDialog(
            self.preview.current_path,
            self,
        )
        self._zoom_dialog.finished.connect(self._release_zoom_dialog)
        self._zoom_dialog.exec()

    def _release_zoom_dialog(self, _result: int) -> None:
        dialog = self._zoom_dialog
        self._zoom_dialog = None
        if dialog is not None:
            dialog.deleteLater()
