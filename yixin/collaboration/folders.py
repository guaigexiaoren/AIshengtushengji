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
    r"^(?:(?P<year>\d{4})\.)?(?P<month>\d{1,2})\.(?P<day>\d{1,2})$"
)


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
    matched = DATE_PATTERN.fullmatch(label.strip())
    if matched is None:
        return None
    current = today or date.today()
    year_text = matched.group("year")
    year = int(year_text) if year_text else current.year
    try:
        return date(year, int(matched.group("month")), int(matched.group("day")))
    except ValueError:
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
        garment_dir = product_dir / "衣服图"
        model_dir = product_dir / "模特图"
        if not check_product_code and (
            not garment_dir.is_dir() or not model_dir.is_dir()
        ):
            continue
        code_info = (
            parse_product_code(product_dir.name)
            if check_product_code
            else None
        )
        garment_paths, garment_errors = (
            _scan_images(garment_dir)
            if garment_dir.is_dir()
            else ([], [])
        )
        model_paths, model_errors = (
            _scan_images(model_dir)
            if model_dir.is_dir()
            else ([], [])
        )
        errors = tuple(
            [
                *garment_errors,
                *model_errors,
                *(
                    ["缺少衣服图目录"]
                    if not garment_dir.is_dir()
                    else []
                ),
                *(
                    ["缺少模特图目录"]
                    if not model_dir.is_dir()
                    else []
                ),
                *(
                    ["没有可用的衣服图"]
                    if garment_dir.is_dir() and not garment_paths
                    else []
                ),
                *(
                    ["没有可用的模特图"]
                    if model_dir.is_dir() and not model_paths
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
    for root, _directories, filenames in os.walk(folder):
        for filename in filenames:
            path = Path(root) / filename
            if path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES:
                images.append(path.resolve())
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
