"""生成运营使用手册的界面截图。

运行方式（仓库根目录）：
    QT_QPA_PLATFORM=offscreen py -3.13 docs/manual/make_screenshots.py

截图使用示例数据，写入 docs/screenshots/，不连接 MySQL、不发起真实生图。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QFont,
    QFontDatabase,
    QImage,
    QPainter,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QGroupBox,
    QMainWindow,
    QSplitter,
)

from tests.test_collaboration_pages import (  # noqa: E402
    FakeCentralRepository,
)
from yixin.collaboration.shared_paths import SharedPathResolver  # noqa: E402
from yixin.collaboration.types import (  # noqa: E402
    PROJECT_COMPLETED,
    PROJECT_IN_PROGRESS,
    PROJECT_SKIPPED,
    PROJECT_SUBMITTED,
    PROJECT_TASK_TYPE_BUYER_SHOW,
    PROJECT_TASK_TYPE_NORMAL,
    ROLE_OPERATIONS,
    AuthUser,
    ProjectRecord,
)
from yixin.paging import PageResult  # noqa: E402
from yixin.ui.collaboration_login import LoginDialog  # noqa: E402
from yixin.ui.dialogs import ListingConfirmDialog  # noqa: E402
from yixin.ui.submission_list_page import SubmissionListPage  # noqa: E402
from yixin.ui.task_create_page import TaskCreatePage  # noqa: E402
from yixin.ui.theme import APP_STYLESHEET  # noqa: E402

DOCS = ROOT / "docs"
SHOTS = DOCS / "screenshots"
SAMPLE = DOCS / "assets" / "sample"
SHARE = SAMPLE / "share"
FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")

ACTOR = AuthUser(
    id=3,
    username="linxiaoyu",
    display_name="运营小林",
    role=ROLE_OPERATIONS,
    urgent_quota_per_day=3,
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _garment_image(path: Path, color: str, label: str) -> None:
    image = Image.new("RGB", (600, 800), "#eef2f7")
    draw = ImageDraw.Draw(image)
    # 简单衣服轮廓：肩膀 + 衣身
    draw.polygon(
        [(170, 210), (250, 170), (350, 170), (430, 210),
         (400, 260), (365, 235), (365, 620), (235, 620),
         (235, 235), (200, 260)],
        fill=color,
        outline="#9aa7ba",
        width=4,
    )
    draw.text((40, 700), label, font=_font(40), fill="#4c5a70")
    image.save(path)


def _model_image(path: Path, skin: str, cloth: str, label: str) -> None:
    image = Image.new("RGB", (600, 800), "#f5f1ec")
    draw = ImageDraw.Draw(image)
    draw.ellipse((250, 110, 350, 210), fill=skin, outline="#b9a48f", width=3)
    draw.polygon(
        [(300, 215), (205, 300), (175, 500), (215, 510),
         (245, 380), (245, 660), (355, 660), (355, 380),
         (385, 510), (425, 500), (395, 300)],
        fill=cloth,
        outline="#9aa7ba",
        width=3,
    )
    draw.text((40, 710), label, font=_font(40), fill="#4c5a70")
    image.save(path)


def _make_share_tree() -> None:
    if SHARE.exists():
        shutil.rmtree(SHARE)
    today = date.today()
    day_dir = SHARE / "内部项目" / "春季项目" / f"{today.month}.{today.day}"
    products = {
        "DV263q25445p80723": (
            [
                ("白色基础衬衫.png", "#f2f2f2", "白色衬衫"),
                ("黑色直筒西装裤.png", "#2f3440", "黑色西裤"),
                ("米色短款外套.png", "#d8c3a5", "米色外套"),
            ],
            [
                ("模特小雅.png", "#f0d3bb", "#dbe7f7", "模特小雅"),
                ("模特安然.png", "#e8c4a8", "#e7ded2", "模特安然"),
            ],
        ),
        "DV271a10233p11001": (
            [
                ("浅蓝条纹衬衫.png", "#bcd6ee", "浅蓝衬衫"),
                ("深蓝牛仔裤.png", "#3c5a83", "深蓝牛仔"),
            ],
            [("模特可可.png", "#f0d3bb", "#f7e2e2", "模特可可")],
        ),
        "DV274b20877p90012": (
            [
                ("灰色针织衫.png", "#a8adb5", "灰色针织"),
                ("杏色半身裙.png", "#e6d3bd", "杏色半裙"),
                ("白色T恤.png", "#f5f5f5", "白色T恤"),
                ("藏青阔腿裤.png", "#2c3a55", "藏青阔腿裤"),
            ],
            [
                ("模特子涵.png", "#e8c4a8", "#e3ecd9", "模特子涵"),
                ("模特七月.png", "#f0d3bb", "#efe0ef", "模特七月"),
            ],
        ),
        # 同一前缀的两个目录：演示「重复货号」，不可勾选
        "DV281c30116p90001": (
            [("红色连衣裙.png", "#c94f4f", "红色连衣裙")],
            [],
        ),
        "DV281c30116p90002": (
            [("红色连衣裙改.png", "#b84343", "红色连衣裙（二）")],
            [],
        ),
    }
    for code, (garments, models) in products.items():
        product_dir = day_dir / code
        garment_dir = product_dir / "衣服图"
        model_dir = product_dir / "模特图"
        garment_dir.mkdir(parents=True)
        for filename, color, label in garments:
            _garment_image(garment_dir / filename, color, label)
        if models:
            model_dir.mkdir()
            for filename, skin, cloth, label in models:
                _model_image(model_dir / filename, skin, cloth, label)


def _make_ai_results() -> None:
    """给提交记录页的第一个主任务准备共享盘 AI成品 图片。"""
    today = date.today()
    folder = (
        SHARE
        / "内部项目"
        / "春季项目"
        / f"{today.month}.{today.day}"
        / "DV263q25445p80723"
        / "AI成品"
    )
    folder.mkdir(parents=True, exist_ok=True)
    palettes = (
        ("#dbe7f7", "成品 1"),
        ("#f7e2e2", "成品 2"),
        ("#e3ecd9", "成品 3"),
        ("#efe0ef", "成品 4"),
    )
    for index, (background, label) in enumerate(palettes, start=1):
        image = Image.new("RGB", (720, 960), background)
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 60, 660, 820), fill="#ffffff", outline="#c3cede", width=4)
        draw.ellipse((300, 150, 420, 270), fill="#f0d3bb", outline="#b9a48f", width=4)
        draw.polygon(
            [(360, 280), (240, 380), (220, 700), (500, 700), (480, 380)],
            fill=background,
            outline="#9aa7ba",
            width=4,
        )
        draw.text((80, 850), label, font=_font(34), fill="#4c5a70")
        image.save(folder / f"_r00{index}_01_{index:04d}.png")


def _record(
    project_id: int,
    product_code: str,
    *,
    status: str = PROJECT_SUBMITTED,
    assignee: str = "",
    approved: int = 0,
    tasks: int = 0,
    results: int = 0,
    garments: int = 0,
    models: int = 0,
    task_type: str = PROJECT_TASK_TYPE_NORMAL,
    urgent: bool = False,
    note: str = "",
    listing_url: str = "",
    listing_id: str = "",
    listed: bool = False,
    folder_name: str = "",
) -> ProjectRecord:
    now = datetime.now() - timedelta(hours=6 * project_id)
    today = date.today()
    folder = folder_name or f"{product_code}80723"
    return ProjectRecord(
        id=project_id,
        project_code=f"运营小林-{product_code}-00{project_id}",
        name=product_code,
        deadline=now + timedelta(days=2),
        priority="normal",
        brand_platform="",
        category="",
        note=note,
        status=status,
        owner_id=ACTOR.id,
        owner_name=ACTOR.display_name,
        project_relative_root=(
            f"内部项目/春季项目/{today.month}.{today.day}/{folder}"
        ),
        pairing_mode="all",
        created_at=now,
        updated_at=now,
        task_count=tasks,
        approved_count=approved,
        product_code=product_code,
        source_date=today,
        project_scope="internal",
        project_name="春季项目",
        completed_at=(
            now + timedelta(hours=5) if status == PROJECT_COMPLETED else None
        ),
        assignee_names=assignee,
        result_count=results,
        garment_count=garments,
        model_count=models,
        task_type=task_type,
        is_urgent=urgent,
        listing_url=listing_url,
        listing_id=listing_id,
        listing_confirmed_at=now if listed else None,
        listing_confirmed_by=ACTOR.id if listed else None,
    )


def _records() -> list[ProjectRecord]:
    return [
        _record(
            1,
            "DV263q25445p",
            status=PROJECT_COMPLETED,
            assignee="AI小周",
            approved=4,
            tasks=4,
            results=4,
            garments=4,
            models=2,
            urgent=True,
            note="重点检查领口细节，注意生活场景感。",
            folder_name="DV263q25445p80723",
        ),
        _record(
            2,
            "DV271a10233p",
            status=PROJECT_IN_PROGRESS,
            assignee="AI小陈",
            results=1,
            garments=2,
            models=1,
            note="蓝白配色，裤脚不要露袜。",
        ),
        _record(
            3,
            "DV274b20877p",
            status=PROJECT_SUBMITTED,
            garments=4,
            models=2,
            task_type=PROJECT_TASK_TYPE_BUYER_SHOW,
            note="外景通勤风买家秀。",
        ),
        _record(
            4,
            "DV281c30116p",
            status=PROJECT_COMPLETED,
            assignee="AI小周",
            approved=2,
            tasks=2,
            results=2,
            garments=2,
            models=1,
            listed=True,
            listing_url="https://item.taobao.com/item.htm?id=845123456789",
            listing_id="845123456789",
        ),
        _record(
            5,
            "DV288d40991p",
            status=PROJECT_SKIPPED,
            garments=1,
            models=1,
            note="季节款暂停上架。",
        ),
    ]


class FakeRepository(FakeCentralRepository):
    def __init__(self, projects: list[ProjectRecord]) -> None:
        super().__init__()
        self.submission_projects = projects

    def list_submission_projects_page(self, _actor, _query, page):
        projects = list(self.submission_projects)
        return PageResult(projects, len(projects), page.page, page.page_size)

    def get_shared_setting(self, _key):
        return str(SHARE)


def _settle(app: QApplication, seconds: float = 1.5) -> None:
    """处理事件循环，等待异步缩略图完成。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)


def _grab(
    widget,
    path: Path,
    *,
    scale: int = 2,
    adjust: bool = True,
    crop_height: int | None = None,
) -> None:
    if adjust:
        widget.adjustSize()
    size = widget.size()
    image = QImage(
        size.width() * scale,
        size.height() * scale,
        QImage.Format.Format_ARGB32,
    )
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.scale(scale, scale)
    widget.render(painter, QPoint(0, 0))
    painter.end()
    if crop_height is not None:
        image = image.copy(
            0,
            0,
            image.width(),
            min(image.height(), int(crop_height * scale)),
        )
    if not image.save(str(path)):
        raise RuntimeError(f"截图保存失败：{path}")
    print(f"已生成 {path.relative_to(ROOT)}")


def _page_window(
    page,
    width: int = 1500,
    height: int = 980,
) -> QMainWindow:
    window = QMainWindow()
    window.setCentralWidget(page)
    window.resize(width, height)
    window.show()
    _settle(QApplication.instance(), 0.4)
    return window


def _find_group(page, title: str):
    for group in page.findChildren(QGroupBox):
        if group.title() == title:
            return group
    return None


def _prepare_product_table(page) -> None:
    """让示例表格列宽适配内容，便于在手册中阅读。"""
    splitter = page.findChild(QSplitter)
    if splitter is not None:
        splitter.setSizes([900, 620])
    page.product_table.resizeColumnsToContents()
    page.product_table.horizontalHeader().setStretchLastSection(True)


def shot_login(app: QApplication) -> None:
    dialog = LoginDialog(FakeRepository([]))
    dialog.username.setText("linxiaoyu")
    dialog.password.setText("123456")
    dialog.show()
    _settle(app, 0.3)
    _grab(dialog, SHOTS / "01-login.png")


def shot_task_submit(app: QApplication) -> None:
    page = TaskCreatePage(
        FakeRepository([]),
        SharedPathResolver(SHARE),
        ACTOR,
    )
    window = _page_window(page)
    _settle(app, 2.0)
    _prepare_product_table(page)

    page.product_table.selectRow(0)
    page.task_type_combo.setCurrentIndex(
        page.task_type_combo.findData(PROJECT_TASK_TYPE_BUYER_SHOW)
    )
    page.note_edit.setText("买家秀，注意自然光和生活场景。")
    page.urgent_checkbox.setChecked(True)
    page._store_state_fields()
    # 示例图里的共享盘路径用 UNC 写法，和实际工作环境一致，也避免临时目录路径过长。
    page.account_path.setText(
        r"\\10.6.6.6\AI作图\内部项目\春季项目"
        f"\\{date.today().month}.{date.today().day}"
    )
    _settle(app, 1.5)

    _grab(window, SHOTS / "02-task-submit.png", adjust=False)

    table_group = _find_group(page, "待创建货号")
    if table_group is not None:
        table = page.product_table
        last_row = table.rowCount() - 1
        content_bottom = (
            table.mapTo(table_group, QPoint(0, 0)).y()
            + table.rowViewportPosition(last_row)
            + table.rowHeight(last_row)
            + 48
        )
        _grab(
            table_group,
            SHOTS / "02b-task-submit-table.png",
            adjust=False,
            crop_height=content_bottom,
        )
    preview_group = _find_group(page, "素材预览")
    if preview_group is not None:
        _grab(
            preview_group,
            SHOTS / "02c-task-submit-preview.png",
            adjust=False,
            crop_height=560,
        )

    # 操作栏单独用更窄的窗口截取，打印后字号才看得清。
    window.resize(1000, 800)
    _settle(app, 0.5)
    footer = page.findChild(QFrame, "PageActionBar")
    if footer is not None:
        _grab(
            footer,
            SHOTS / "03-task-submit-fields.png",
            scale=3,
            adjust=False,
        )
    window.close()


def shot_submission_list(app: QApplication) -> None:
    page = SubmissionListPage(
        FakeRepository(_records()),
        SharedPathResolver(SHARE),
        ACTOR,
        "records",
    )
    window = _page_window(page, 2100, 950)
    for column, width in enumerate(
        (210, 120, 70, 90, 110, 85, 130, 60, 75, 125, 135, 135, 60)
    ):
        page.project_table.setColumnWidth(column, width)
    splitter = page.findChild(QSplitter)
    if splitter is not None:
        splitter.setSizes([1500, 600])
    _settle(app, 1.2)
    _grab(window, SHOTS / "04-submission-list.png", adjust=False)

    # 全页截图用于看布局；再裁出左表和右栏两张局部图，打印后才看得清。
    image = Image.open(SHOTS / "04-submission-list.png")
    scale = 2
    image.crop((0, 75 * scale, 1020 * scale, 400 * scale)).save(
        SHOTS / "04b-submission-table.png"
    )
    image.crop(
        (1490 * scale, 0, image.width, 545 * scale)
    ).save(SHOTS / "04c-submission-panel.png")
    window.close()


def shot_listing_dialog(app: QApplication) -> None:
    dialog = ListingConfirmDialog(
        "https://item.taobao.com/item.htm?id=845123456789"
    )
    dialog.resize(640, 240)
    dialog.show()
    _settle(app, 0.3)
    _grab(dialog, SHOTS / "05-listing-dialog.png")


def main() -> int:
    app = QApplication.instance() or QApplication([])
    # 离屏平台不会自动读取系统字体，需显式注册，否则中文显示为方框。
    QFontDatabase.addApplicationFont(str(FONT_PATH))
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLESHEET)

    SHOTS.mkdir(parents=True, exist_ok=True)
    _make_share_tree()
    _make_ai_results()

    shot_login(app)
    shot_task_submit(app)
    shot_submission_list(app)
    shot_listing_dialog(app)
    print("全部截图完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
