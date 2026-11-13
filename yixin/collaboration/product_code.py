from __future__ import annotations

import re
from dataclasses import dataclass


# 内部项目货号：品牌 2 位 + 年份 2 位 + 季节 1 位 + 分隔字母 + 流水号 +
# 分隔字母 + 品类 2 位 + 颜色 2 位 + 可选尺码 1 位，
# 例如 DV263q25445p80723 或去掉尺码的 DV263q25445p8072。
# 系统里统一只保留到「品类+颜色」，尺码位有或没有都识别为同一货号；
# 货号后面追加的字符（如 -备注、_1）不参与识别，一律忽略。
PRODUCT_CODE_PATTERN = re.compile(
    r"^(?P<brand>[A-Za-z]{2})"
    r"(?P<year>\d{2})"
    r"(?P<season>[1-4])"
    r"(?P<serial_sep>[A-Za-z])"
    r"(?P<serial>\d+)"
    r"(?P<color_sep>[A-Za-z])"
    r"(?P<tail>\d{4,5})"
)

PRODUCT_CODE_EXAMPLE = "DV263q25445p80723"


@dataclass(frozen=True)
class ProductCode:
    brand: str
    year: str
    season: str
    serial_sep: str
    serial: str
    color_sep: str
    style_color: str

    @property
    def base_code(self) -> str:
        return (
            f"{self.brand}{self.year}{self.season}"
            f"{self.serial_sep}{self.serial}"
            f"{self.color_sep}{self.style_color}"
        )


def parse_product_code(value: str) -> ProductCode | None:
    # 只要求货号出现在开头，后面的附加字符忽略。
    matched = PRODUCT_CODE_PATTERN.match(value.strip())
    if matched is None:
        return None
    return ProductCode(
        brand=matched.group("brand").upper(),
        year=matched.group("year"),
        season=matched.group("season"),
        serial_sep=matched.group("serial_sep"),
        serial=matched.group("serial"),
        color_sep=matched.group("color_sep"),
        style_color=matched.group("tail")[:4],
    )
