from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

from ..logging_utils import register_sensitive_value
from ..private_config import API_KEY_MASTER_SECRET
from ..security import validate_api_key
from .types import AuthUser

if TYPE_CHECKING:
    from .repository import MySQLCentralRepository


class ApiKeyError(RuntimeError):
    pass


def _fernet() -> Fernet:
    digest = hashlib.sha256(API_KEY_MASTER_SECRET.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(value: str) -> str:
    clean = validate_api_key(value)
    return _fernet().encrypt(clean.encode("utf-8")).decode("ascii")


def decrypt_api_key(value: str) -> str:
    try:
        clean = _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise ApiKeyError("API Key 解密失败，请联系管理员重新配置") from exc
    return validate_api_key(clean)


class DatabaseApiKeyStore:
    """Compatibility store used by all generation pages.

    Keys are loaded from MySQL for the logged-in account each time a request
    starts, so administrator replacements apply to queued work.
    """

    def __init__(
        self,
        repository: MySQLCentralRepository,
        actor: AuthUser,
    ) -> None:
        self.repository = repository
        self.actor = actor

    def load(
        self,
        *,
        provider_id: int | None = None,
        strict: bool = False,
    ) -> str | None:
        try:
            value = self.repository.load_actor_api_key(
                self.actor,
                provider_id=provider_id,
            )
        except Exception:
            if strict:
                raise
            return None
        register_sensitive_value(value)
        return value

    def save(self, _api_key: str) -> None:
        raise ApiKeyError("API Key 只能由管理员在管理控制台配置")

    def clear(self) -> None:
        raise ApiKeyError("API Key 只能由管理员在管理控制台清除")
