from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit


# 常见电商链接里代表商品/链接本身的查询参数名。
ID_QUERY_KEYS = (
    "id",
    "goods_id",
    "product_id",
    "item_id",
    "sku_id",
    "spu_id",
    "ad_id",
    "itemid",
    "productid",
    "goodsid",
)


def extract_listing_id(value: str) -> str:
    """从完整上架链接中提取链接 ID；识别不到返回空串。"""
    text = (value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.query:
        query = parse_qs(parsed.query)
        for wanted in ID_QUERY_KEYS:
            for actual, values in query.items():
                if actual.casefold() == wanted and values:
                    candidate = values[0].strip()
                    if candidate:
                        return candidate
    path = parsed.path if (parsed.scheme or parsed.netloc) else text
    segments = [segment for segment in path.split("/") if segment]
    for segment in reversed(segments):
        matched = re.search(r"(\d{6,})", segment)
        if matched is not None:
            return matched.group(1)
    if segments:
        last = segments[-1].split("?")[0].split("#")[0].strip()
        if "." in last:
            last = last.rsplit(".", 1)[0]
        return last
    return ""
