from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..storage import SUPPORTED_IMAGE_SUFFIXES, image_metadata
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


def list_date_folders(account_root: Path) -> list[DateFolder]:
    result: list[DateFolder] = []
    if not account_root.is_dir():
        return result
    for path in account_root.iterdir():
        if not path.is_dir():
            continue
        parsed = parse_date_folder(path.name)
        if parsed is not None:
            result.append(DateFolder(path.name, path, parsed))
    result.sort(key=lambda item: item.source_date, reverse=True)
    return result


def current_date_folder(account_root: Path) -> DateFolder:
    today = date.today()
    label = f"{today.month}.{today.day}"
    return DateFolder(label, account_root / label, today)


def scan_product_folders(date_root: Path) -> list[ProductFolder]:
    products: list[ProductFolder] = []
    if not date_root.is_dir():
        return products
    for product_dir in sorted(
        (path for path in date_root.iterdir() if path.is_dir()),
        key=lambda path: natural_sort_key(path.name),
    ):
        garment_dir = product_dir / "衣服图"
        model_dir = product_dir / "模特图"
        if not garment_dir.is_dir() or not model_dir.is_dir():
            continue
        garment_paths, garment_errors = _scan_images(garment_dir)
        model_paths, model_errors = _scan_images(model_dir)
        errors = tuple(
            [
                *garment_errors,
                *model_errors,
                *(
                    ["没有可用的衣服图"]
                    if not garment_paths
                    else []
                ),
                *(
                    ["没有可用的模特图"]
                    if not model_paths
                    else []
                ),
                *(
                    ["衣服图超过 13 张，无法一次提交"]
                    if len(garment_paths) > 13
                    else []
                ),
            ]
        )
        products.append(
            ProductFolder(
                product_code=product_dir.name,
                path=product_dir,
                garments=tuple(garment_paths),
                models=tuple(model_paths),
                errors=errors,
            )
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
