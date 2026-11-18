from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from ..storage import SUPPORTED_IMAGE_SUFFIXES, image_metadata
from .product_code import PRODUCT_CODE_EXAMPLE, ProductCode, parse_product_code
from .types import ProjectAssetInput


DATE_PATTERN = re.compile(
    r"^(?:(?P<year>\d{4})[.\-])?(?P<month>\d{1,2})[.\-](?P<day>\d{1,2})$"
)
COMPACT_DATE_PATTERN = re.compile(
    r"^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})$"
)

GARMENT_DIR_NAMES = ("衣服图", "衣服", "garment")
MODEL_DIR_NAMES = ("模特图", "模特", "model")
SKIP_SCAN_DIR_NAMES = {
    "备份",
    "backup",
    "副本",
    "temp",
    "tmp",
    "临时",
    "缓存",
}


@dataclass(frozen=True)
class DateFolder:
    label: str
    path: Path
    source_date: date


@dataclass(frozen=True)
class ProductFolder:
    product_code: str
    path: Path
    garments: tuple[Path, ...]
    models: tuple[Path, ...]
    errors: tuple[str, ...]
    code_info: ProductCode | None = None
    garment_dir: Path | None = None
    model_dir: Path | None = None


@dataclass(frozen=True)
class AccountDirectory:
    path: Path
    source_date: date
    date_recognized: bool


def resolve_account_directory(
    raw_path: str | Path,
    shared_root: Path,
    *,
    today: date | None = None,
) -> AccountDirectory:
    root = Path(shared_root).expanduser().resolve()
    path = Path(raw_path).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("账号目录必须位于固定共享盘根目录内") from exc
    parsed = parse_date_folder(path.name, today=today)
    return AccountDirectory(
        path=path,
        source_date=parsed or (today or date.today()),
        date_recognized=parsed is not None,
    )


def parse_date_folder(label: str, *, today: date | None = None) -> date | None:
    text = label.strip()
    matched = DATE_PATTERN.fullmatch(text) or COMPACT_DATE_PATTERN.fullmatch(text)
    if matched is None:
        return None
    current = today or date.today()
    year_text = matched.group("year")
    year = int(year_text) if year_text else current.year
    try:
        return date(year, int(matched.group("month")), int(matched.group("day")))
    except ValueError:
        return None


def find_role_directory(
    product_dir: Path,
    candidates: tuple[str, ...],
) -> Path | None:
    """按优先级找衣服图/模特图目录，英文名称不区分大小写。"""
    for name in candidates:
        candidate = product_dir / name
        if candidate.is_dir():
            return candidate
    lowered = {name.casefold() for name in candidates}
    try:
        children = list(product_dir.iterdir())
    except OSError:
        return None
    for child in children:
        if child.is_dir() and child.name.casefold() in lowered:
            return child
    return None


def current_date_folder(account_root: Path) -> DateFolder:
    today = date.today()
    label = f"{today.month}.{today.day}"
    return DateFolder(label, account_root / label, today)


def scan_product_folders(
    date_root: Path,
    *,
    check_product_code: bool = False,
) -> list[ProductFolder]:
    products: list[ProductFolder] = []
    if not date_root.is_dir():
        return products
    for product_dir in sorted(
        (path for path in date_root.iterdir() if path.is_dir()),
        key=lambda path: natural_sort_key(path.name),
    ):
        garment_dir = find_role_directory(product_dir, GARMENT_DIR_NAMES)
        model_dir = find_role_directory(product_dir, MODEL_DIR_NAMES)
        code_info = (
            parse_product_code(product_dir.name)
            if check_product_code
            else None
        )
        garment_paths, garment_errors = (
            _scan_images(garment_dir)
            if garment_dir is not None
            else ([], [])
        )
        model_paths, model_errors = (
            _scan_images(model_dir)
            if model_dir is not None
            else ([], [])
        )
        errors = tuple(
            [
                *garment_errors,
                *model_errors,
                *(
                    ["缺少衣服图目录（支持 衣服图/衣服/garment）"]
                    if garment_dir is None
                    else []
                ),
                *(
                    ["没有可用的衣服图"]
                    if garment_dir is not None and not garment_paths
                    else []
                ),
                *(
                    ["衣服图超过 13 张，无法一次提交"]
                    if len(garment_paths) > 13
                    else []
                ),
                *(
                    [
                        "货号格式不符合内部项目规则，"
                        f"示例：{PRODUCT_CODE_EXAMPLE}"
                    ]
                    if check_product_code and code_info is None
                    else []
                ),
            ]
        )
        products.append(
            ProductFolder(
                product_code=(
                    code_info.base_code
                    if code_info is not None
                    else product_dir.name
                ),
                path=product_dir,
                garments=tuple(garment_paths),
                models=tuple(model_paths),
                errors=errors,
                code_info=code_info,
                garment_dir=garment_dir,
                model_dir=model_dir,
            )
        )
    return _mark_duplicate_codes(products)


def _mark_duplicate_codes(
    products: list[ProductFolder],
) -> list[ProductFolder]:
    grouped: dict[str, list[int]] = {}
    for index, product in enumerate(products):
        if product.code_info is None:
            continue
        grouped.setdefault(product.product_code, []).append(index)
    for indexes in grouped.values():
        if len(indexes) < 2:
            continue
        names = "、".join(products[index].path.name for index in indexes)
        for index in indexes:
            product = products[index]
            products[index] = replace(
                product,
                errors=(
                    *product.errors,
                    f"多个目录对应同一货号（{names}），请只保留一个",
                ),
            )
    return products


def _scan_images(folder: Path) -> tuple[list[Path], list[str]]:
    images: list[Path] = []
    errors: list[str] = []
    for root, directories, filenames in os.walk(folder):
        # 跳过备份/临时目录和隐藏目录，避免把旧图重复算进来。
        directories[:] = [
            name
            for name in directories
            if not name.startswith(".")
            and name.casefold() not in SKIP_SCAN_DIR_NAMES
        ]
        for filename in filenames:
            path = Path(root) / filename
            if path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES:
                images.append(path)
    images.sort(key=lambda item: natural_sort_key(str(item)))
    return images, errors


def natural_sort_key(value: str) -> tuple[tuple[int, str | int], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value.replace("\\", "/"))
        if part
    )


def project_assets(
    project_root: Path,
    garments: tuple[Path, ...] | list[Path],
    models: tuple[Path, ...] | list[Path],
) -> tuple[ProjectAssetInput, ...]:
    assets: list[ProjectAssetInput] = []
    for role, paths in (("garment", garments), ("model", models)):
        for path in paths:
            data = path.read_bytes()
            mime, width, height = image_metadata(data)
            assets.append(
                ProjectAssetInput(
                    role=role,
                    relative_path=path.resolve().relative_to(
                        project_root.resolve()
                    ).as_posix(),
                    original_name=path.name,
                    content_hash=hashlib.sha256(data).hexdigest(),
                    mime=mime,
                    width=width,
                    height=height,
                )
            )
    return tuple(assets)
