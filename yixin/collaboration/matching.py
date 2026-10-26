from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .types import PAIRING_ALL, PAIRING_FILENAME, PAIRING_MANUAL, PairingInput


FILENAME_PATTERN = re.compile(r"^([GgMm])(\d+)(?=[_\-\s.]|$)")


@dataclass(frozen=True)
class MatchAsset:
    relative_path: str
    original_name: str


@dataclass(frozen=True)
class MatchResult:
    pairs: tuple[PairingInput, ...]
    errors: tuple[str, ...]


def build_pairs(
    garments: Sequence[MatchAsset],
    models: Sequence[MatchAsset],
    mode: str,
    manual_pairs: Iterable[PairingInput] = (),
) -> MatchResult:
    if not garments or not models:
        return MatchResult((), ("服装图和模特图都至少需要一张",))
    if mode == PAIRING_MANUAL:
        return _manual_pairs(garments, models, manual_pairs)
    if mode == PAIRING_ALL:
        return MatchResult(
            tuple(
                PairingInput(garment_index, model_index)
                for garment_index in range(len(garments))
                for model_index in range(len(models))
            ),
            (),
        )
    if mode == PAIRING_FILENAME:
        return _filename_pairs(garments, models)
    return MatchResult((), ("不支持的配对方式",))


def _manual_pairs(
    garments: Sequence[MatchAsset],
    models: Sequence[MatchAsset],
    pairs: Iterable[PairingInput],
) -> MatchResult:
    errors: list[str] = []
    valid: list[PairingInput] = []
    seen: set[PairingInput] = set()
    for pair in pairs:
        if not 0 <= pair.garment_index < len(garments):
            errors.append("手动配对包含不存在的服装图")
            continue
        if not 0 <= pair.model_index < len(models):
            errors.append("手动配对包含不存在的模特图")
            continue
        if pair in seen:
            continue
        seen.add(pair)
        valid.append(pair)
    if not valid:
        errors.append("请至少添加一组手动配对")
    return MatchResult(tuple(valid), tuple(errors))


def _filename_pairs(
    garments: Sequence[MatchAsset],
    models: Sequence[MatchAsset],
) -> MatchResult:
    garment_codes, garment_errors = _index_codes(garments, "服装")
    model_codes, model_errors = _index_codes(models, "模特")
    errors = garment_errors + model_errors
    pairs: list[PairingInput] = []
    for code in sorted(set(garment_codes) & set(model_codes)):
        for garment_index in garment_codes[code]:
            for model_index in model_codes[code]:
                pairs.append(PairingInput(garment_index, model_index))
    for code in sorted(set(garment_codes) - set(model_codes)):
        errors.append(f"服装编号 {code} 没有对应模特图")
    for code in sorted(set(model_codes) - set(garment_codes)):
        errors.append(f"模特编号 {code} 没有对应服装图")
    if not pairs:
        errors.append("没有找到可配对的 G/M 编号")
    return MatchResult(tuple(pairs), tuple(dict.fromkeys(errors)))


def _index_codes(
    assets: Sequence[MatchAsset],
    role_label: str,
) -> tuple[dict[str, list[int]], list[str]]:
    result: dict[str, list[int]] = {}
    errors: list[str] = []
    expected_prefix = "G" if role_label == "服装" else "M"
    for index, asset in enumerate(assets):
        stem = Path(asset.original_name).stem
        matched = FILENAME_PATTERN.match(stem)
        if matched is None or matched.group(1).upper() != expected_prefix:
            errors.append(
                f"{role_label}图片“{asset.original_name}”缺少 {expected_prefix}数字编号"
            )
            continue
        code = f"{int(matched.group(2)):03d}"
        result.setdefault(code, []).append(index)
    for code, indexes in result.items():
        if len(indexes) > 1:
            errors.append(f"{role_label}编号 {code} 对应多张图片，请改用手动配对")
    return result, errors
