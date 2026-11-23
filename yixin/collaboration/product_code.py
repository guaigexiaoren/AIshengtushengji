from __future__ import annotations

import re
from dataclasses import dataclass


# 内部项目货号：品牌 2 位 + 年份 2 位 + 季节 1 位 + 分隔字母 + 流水号 +
# 分隔字母（截止点），例如 DV263q25445p。
# 系统只识别到第二个分隔字母为止，后面的品类、颜色、尺码和备注等字符
# 一律忽略；所以 DV263q25445p80723 与 DV263q25445p8072、DV263q25445p
# 都识别为同一货号 DV263q25445p。
PRODUCT_CODE_PATTERN = re.compile(
    r"^(?P<brand>[A-Za-z]{2})"
    r"(?P<year>[0-9]{2})"
    r"(?P<season>[1-4])"
    r"(?P<serial_sep>[A-Za-z])"
    r"(?P<serial>[0-9]+)"
    r"(?P<color_sep>[A-Za-z])"
)

PRODUCT_CODE_EXAMPLE = "DV263q25445p"


@dataclass(frozen=True)
class ProductCode:
    brand: str
    year: str
    season: str
    serial_sep: str
    serial: str
    color_sep: str

    @property
    def base_code(self) -> str:
        return (
            f"{self.brand}{self.year}{self.season}"
            f"{self.serial_sep}{self.serial}"
            f"{self.color_sep}"
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
    )
