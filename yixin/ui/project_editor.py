from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.matching import MatchAsset, build_pairs
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    PAIRING_ALL,
    PAIRING_FILENAME,
    PAIRING_LABELS,
    PAIRING_MANUAL,
    PRIORITIES,
    PRIORITY_LABELS,
    AuthUser,
    PairingInput,
    ProjectAssetInput,
    ProjectDraft,
)
from ..storage import image_metadata
from ..time_utils import local_to_utc_naive, utc_naive_to_local
from .widgets import scan_image_inputs


class ProjectEditorDialog(QDialog):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        *,
        existing: ProjectDraft | None = None,
        project_id: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.project_id = project_id
        self.existing = existing
        self.project_root: Path | None = None
        self.garments: list[ProjectAssetInput] = []
        self.models: list[ProjectAssetInput] = []
        self.manual_pairs: list[PairingInput] = []
        self.setWindowTitle("修改项目" if project_id else "新建运营项目")
        self.resize(980, 760)
        self._build()
        if existing:
            self._load_existing(existing)
        self._refresh_pair_preview()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        form_card = QGroupBox("项目信息")
        form = QFormLayout(form_card)

        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("例如 P2026-001")
        self.name_edit = QLineEdit()
        self.deadline_edit = QDateTimeEdit(
            QDateTime.currentDateTime().addDays(3)
        )
        self.deadline_edit.setCalendarPopup(True)
        self.deadline_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.priority_combo = QComboBox()
        for priority in PRIORITIES:
            self.priority_combo.addItem(PRIORITY_LABELS[priority], priority)
        self.priority_combo.setCurrentIndex(
            self.priority_combo.findData("normal")
        )
        self.brand_edit = QLineEdit()
        self.category_edit = QLineEdit()
        self.note_edit = QTextEdit()
        self.note_edit.setMaximumHeight(76)
        form.addRow("项目编号 *", self.code_edit)
        form.addRow("项目名称 *", self.name_edit)
        form.addRow("截止时间 *", self.deadline_edit)
        form.addRow("优先级", self.priority_combo)
        form.addRow("品牌/平台", self.brand_edit)
        form.addRow("品类", self.category_edit)
        form.addRow("备注", self.note_edit)
        outer.addWidget(form_card)

        folder_card = QGroupBox("共享盘素材")
        folder_layout = QVBoxLayout(folder_card)
        root_row = QHBoxLayout()
        self.folder_label = QLabel("尚未选择项目目录")
        self.folder_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        choose_root = QPushButton("选择项目目录")
        choose_root.clicked.connect(self._choose_project_root)
        root_row.addWidget(self.folder_label, 1)
        root_row.addWidget(choose_root)
        folder_layout.addLayout(root_row)
        asset_row = QHBoxLayout()
        self.garment_list = QListWidget()
        self.model_list = QListWidget()
        garment_column = QVBoxLayout()
        model_column = QVBoxLayout()
        garment_header = QHBoxLayout()
        model_header = QHBoxLayout()
        garment_header.addWidget(QLabel("衣服图"))
        model_header.addWidget(QLabel("模特图"))
        garment_button = QPushButton("选择文件夹")
        model_button = QPushButton("选择文件夹")
        garment_button.clicked.connect(
            lambda: self._choose_asset_folder("garment")
        )
        model_button.clicked.connect(lambda: self._choose_asset_folder("model"))
        garment_header.addWidget(garment_button)
        model_header.addWidget(model_button)
        garment_column.addLayout(garment_header)
        model_column.addLayout(model_header)
        garment_column.addWidget(self.garment_list)
        model_column.addWidget(self.model_list)
        asset_row.addLayout(garment_column, 1)
        asset_row.addLayout(model_column, 1)
        folder_layout.addLayout(asset_row)
        self.asset_summary = QLabel()
        folder_layout.addWidget(self.asset_summary)
        outer.addWidget(folder_card, 1)

        pair_card = QGroupBox("配对方式")
        pair_layout = QVBoxLayout(pair_card)
        mode_row = QHBoxLayout()
        self.mode_combo = QComboBox()
        for mode in (PAIRING_MANUAL, PAIRING_ALL, PAIRING_FILENAME):
            self.mode_combo.addItem(PAIRING_LABELS[mode], mode)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        mode_row.addWidget(QLabel("方式"))
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        pair_layout.addLayout(mode_row)
        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self._build_manual_pairing())
        self.mode_stack.addWidget(
            self._hint_widget(
                "全部组合会为每张衣服图和每张模特图创建一组子任务。"
            )
        )
        self.mode_stack.addWidget(
            self._hint_widget(
                "图片名称以 G001、M001 开头；G/M 后数字相同的图片组成子任务。"
            )
        )
        pair_layout.addWidget(self.mode_stack)
        self.pair_preview = QLabel()
        self.pair_preview.setWordWrap(True)
        pair_layout.addWidget(self.pair_preview)
        outer.addWidget(pair_card, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存项目")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _build_manual_pairing(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.garment_combo = QComboBox()
        self.model_combo = QComboBox()
        add_button = QPushButton("添加配对")
        remove_button = QPushButton("移除所选")
        add_button.clicked.connect(self._add_manual_pair)
        remove_button.clicked.connect(self._remove_manual_pair)
        row.addWidget(QLabel("服装"))
        row.addWidget(self.garment_combo, 1)
        row.addWidget(QLabel("模特"))
        row.addWidget(self.model_combo, 1)
        row.addWidget(add_button)
        row.addWidget(remove_button)
        layout.addLayout(row)
        self.manual_table = QTableWidget(0, 2)
        self.manual_table.setHorizontalHeaderLabels(["服装图", "模特图"])
        self.manual_table.horizontalHeader().setStretchLastSection(True)
        self.manual_table.setMaximumHeight(150)
        layout.addWidget(self.manual_table)
        return widget

    @staticmethod
    def _hint_widget(text: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color:#60708a;")
        layout.addWidget(label)
        layout.addStretch(1)
        return widget

    def _load_existing(self, draft: ProjectDraft) -> None:
        self.code_edit.setText(draft.project_code)
        self.name_edit.setText(draft.name)
        self.deadline_edit.setDateTime(
            QDateTime(
                utc_naive_to_local(draft.deadline).date(),
                utc_naive_to_local(draft.deadline).time(),
            )
        )
        self.priority_combo.setCurrentIndex(
            self.priority_combo.findData(draft.priority)
        )
        self.brand_edit.setText(draft.brand_platform)
        self.category_edit.setText(draft.category)
        self.note_edit.setPlainText(draft.note)
        self.project_root = self.resolver.project_root(
            draft.project_relative_root
        )
        self.folder_label.setText(str(self.project_root))
        self.garments = [
            asset for asset in draft.assets if asset.role == "garment"
        ]
        self.models = [asset for asset in draft.assets if asset.role == "model"]
        self.manual_pairs = list(draft.pairs)
        self.mode_combo.setCurrentIndex(
            self.mode_combo.findData(draft.pairing_mode)
        )
        self._refresh_asset_lists()
        self._refresh_manual_table()

    def _choose_project_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择共享盘项目目录",
        )
        if not selected:
            return
        self.project_root = Path(selected).resolve()
        try:
            self.resolver.relative_to_root(self.project_root)
        except ValueError as exc:
            QMessageBox.warning(self, "目录不在共享盘", str(exc))
            self.project_root = None
            return
        self.folder_label.setText(str(self.project_root))
        garment_folder = self.project_root / "衣服图"
        model_folder = self.project_root / "模特图"
        if garment_folder.is_dir() and model_folder.is_dir():
            self._import_folder("garment", garment_folder)
            self._import_folder("model", model_folder)
        else:
            QMessageBox.information(
                self,
                "未找到标准目录",
                "项目中未找到“衣服图”和“模特图”两个目录，"
                "请分别选择实际的图片文件夹。",
            )

    def _choose_asset_folder(self, role: str) -> None:
        if self.project_root is None:
            QMessageBox.information(self, "请先选择项目", "请先选择共享盘项目目录。")
            return
        selected = QFileDialog.getExistingDirectory(self, "选择图片文件夹")
        if selected:
            self._import_folder(role, Path(selected))

    def _import_folder(self, role: str, folder: Path) -> None:
        paths, errors = scan_image_inputs([folder])
        target = self.garments if role == "garment" else self.models
        known_hashes = {asset.content_hash for asset in target}
        imported: list[ProjectAssetInput] = []
        for path in paths:
            try:
                data = path.read_bytes()
                mime, width, height = image_metadata(data)
                content_hash = hashlib.sha256(data).hexdigest()
                if content_hash in known_hashes:
                    continue
                known_hashes.add(content_hash)
                imported.append(
                    ProjectAssetInput(
                        role=role,
                        relative_path=self._relative_to_project(path),
                        original_name=path.name,
                        content_hash=content_hash,
                        mime=mime,
                        width=width,
                        height=height,
                    )
                )
            except Exception as exc:
                errors.append(f"{path.name}：{exc}")
        if imported:
            target[:] = imported
            self.manual_pairs.clear()
        if errors:
            QMessageBox.warning(
                self,
                "部分文件未导入",
                "\n".join(errors[:8]),
            )
        elif not imported:
            QMessageBox.information(
                self,
                "没有新图片",
                "所选文件夹中没有可导入的新图片。",
            )
        self._refresh_asset_lists()
        self._refresh_manual_table()
        self._refresh_pair_preview()

    def _relative_to_project(self, path: Path) -> str:
        if self.project_root is None:
            raise ValueError("尚未选择项目目录")
        try:
            return path.resolve().relative_to(
                self.project_root.resolve()
            ).as_posix()
        except ValueError as exc:
            raise ValueError(f"{path.name} 不在当前项目目录内") from exc

    def _refresh_asset_lists(self) -> None:
        self.garment_list.clear()
        self.model_list.clear()
        self.garment_combo.clear()
        self.model_combo.clear()
        for index, asset in enumerate(self.garments):
            self.garment_list.addItem(asset.original_name)
            self.garment_combo.addItem(asset.original_name, index)
        for index, asset in enumerate(self.models):
            self.model_list.addItem(asset.original_name)
            self.model_combo.addItem(asset.original_name, index)
        self.asset_summary.setText(
            f"衣服图 {len(self.garments)} 张，模特图 {len(self.models)} 张。"
        )

    def _refresh_manual_table(self) -> None:
        self.manual_table.setRowCount(0)
        for pair in self.manual_pairs:
            if not (
                0 <= pair.garment_index < len(self.garments)
                and 0 <= pair.model_index < len(self.models)
            ):
                continue
            row = self.manual_table.rowCount()
            self.manual_table.insertRow(row)
            self.manual_table.setItem(
                row,
                0,
                QTableWidgetItem(
                    self.garments[pair.garment_index].original_name
                ),
            )
            self.manual_table.setItem(
                row,
                1,
                QTableWidgetItem(self.models[pair.model_index].original_name),
            )

    def _add_manual_pair(self) -> None:
        garment_index = self.garment_combo.currentData()
        model_index = self.model_combo.currentData()
        if garment_index is None or model_index is None:
            return
        pair = PairingInput(int(garment_index), int(model_index))
        if pair not in self.manual_pairs:
            self.manual_pairs.append(pair)
            self._refresh_manual_table()
            self._refresh_pair_preview()

    def _remove_manual_pair(self) -> None:
        row = self.manual_table.currentRow()
        if 0 <= row < len(self.manual_pairs):
            self.manual_pairs.pop(row)
            self._refresh_manual_table()
            self._refresh_pair_preview()

    def _mode_changed(self, index: int) -> None:
        self.mode_stack.setCurrentIndex(index)
        self._refresh_pair_preview()

    def _current_pairs(self):
        mode = self.mode_combo.currentData()
        garments = [
            MatchAsset(item.relative_path, item.original_name)
            for item in self.garments
        ]
        models = [
            MatchAsset(item.relative_path, item.original_name)
            for item in self.models
        ]
        return build_pairs(garments, models, mode, self.manual_pairs)

    def _refresh_pair_preview(self) -> None:
        if not hasattr(self, "pair_preview"):
            return
        result = self._current_pairs()
        if result.errors:
            self.pair_preview.setText(
                f"暂不能提交：{'；'.join(result.errors[:6])}"
            )
            self.pair_preview.setStyleSheet("color:#b23a48;")
        else:
            self.pair_preview.setText(f"将创建 {len(result.pairs)} 个子任务。")
            self.pair_preview.setStyleSheet("color:#2a7a50;")

    def _save(self) -> None:
        if self.project_root is None:
            QMessageBox.warning(self, "缺少项目目录", "请选择共享盘项目目录。")
            return
        if not self.code_edit.text().strip() or not self.name_edit.text().strip():
            QMessageBox.warning(self, "信息不完整", "项目编号和名称不能为空。")
            return
        result = self._current_pairs()
        if result.errors:
            QMessageBox.warning(
                self,
                "配对未完成",
                "\n".join(result.errors),
            )
            return
        try:
            relative_root = self.resolver.relative_to_root(self.project_root)
        except ValueError as exc:
            QMessageBox.warning(self, "目录错误", str(exc))
            return
        missing = [
            asset.original_name
            for asset in (*self.garments, *self.models)
            if not (self.project_root / asset.relative_path).is_file()
        ]
        if missing:
            QMessageBox.warning(
                self,
                "图片文件不存在",
                "以下图片已从共享盘移除，请重新导入：\n" + "\n".join(missing[:10]),
            )
            return
        deadline = local_to_utc_naive(
            self.deadline_edit.dateTime().toPython()
        )
        draft = ProjectDraft(
            project_code=self.code_edit.text().strip(),
            name=self.name_edit.text().strip(),
            deadline=deadline,
            priority=self.priority_combo.currentData(),
            project_relative_root=relative_root,
            pairing_mode=self.mode_combo.currentData(),
            assets=tuple([*self.garments, *self.models]),
            pairs=result.pairs,
            brand_platform=self.brand_edit.text().strip(),
            category=self.category_edit.text().strip(),
            note=self.note_edit.toPlainText().strip(),
        )
        try:
            if self.project_id:
                self.repository.update_project(self.project_id, self.actor, draft)
            else:
                self.project_id = self.repository.create_project(
                    self.actor,
                    draft,
                )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()
