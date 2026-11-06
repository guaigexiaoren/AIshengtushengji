from __future__ import annotations

import logging
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "access_token",
    "refresh_token",
    "secret",
    "password",
    "token",
}
SENSITIVE_QUERY_KEYS = {
    "key",
    "api_key",
    "token",
    "access_token",
    "signature",
    "sig",
    "expires",
    "x-amz-signature",
    "x-amz-credential",
}
BEARER_PATTERN = re.compile(r"(?i)(Bearer\s+)[^\s,;\"']+")
QUERY_SECRET_PATTERN = re.compile(
    r"(?i)([?&](?:key|api_key|token|access_token|signature|sig|expires)=)[^&#\s]+"
)
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")

_lock = threading.Lock()
_sensitive_values: set[str] = set()


def register_sensitive_value(value: str | None) -> None:
    if not value:
        return
    with _lock:
        _sensitive_values.add(value)


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or not parts.query:
        return value
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key.lower() in SENSITIVE_QUERY_KEYS for key, _ in query):
        return value
    safe_query = [
        (key, "[已隐藏]") if key.lower() in SENSITIVE_QUERY_KEYS else (key, item)
        for key, item in query
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(safe_query), parts.fragment)
    )


def sanitize_text(value: Any) -> str:
    text = str(value)
    with _lock:
        secrets = tuple(_sensitive_values)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = BEARER_PATTERN.sub(r"\1[已隐藏]", text)
    text = QUERY_SECRET_PATTERN.sub(r"\1[已隐藏]", text)
    # 链接可能夹在错误信息中间，对所有 URL 都做签名参数脱敏。
    text = URL_PATTERN.sub(lambda match: _redact_url(match.group(0)), text)
    return text


def redact_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in SENSITIVE_KEYS or lowered.endswith("_secret"):
                result[key_text] = "[已隐藏]"
            else:
                result[key_text] = redact_data(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        if value.startswith("data:image/") and ";base64," in value:
            return "[图片数据已省略]"
        if value.startswith(("http://", "https://")):
            return sanitize_text(_redact_url(value))
        return sanitize_text(value)
    return value


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = sanitize_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(path: Path) -> logging.Logger:
    logger = logging.getLogger("yixin")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s")
    )
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    return logger
