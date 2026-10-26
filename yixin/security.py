from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from .constants import MAX_API_KEY_LENGTH, MIN_API_KEY_LENGTH
from .storage import atomic_write_bytes


class SecretStorageError(RuntimeError):
    pass


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob_from_bytes(data: bytes) -> tuple[DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _blob_to_bytes(blob: DATA_BLOB) -> bytes:
    if not blob.pbData or blob.cbData == 0:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_protect(data: bytes, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStorageError("DPAPI 仅支持 Windows")
    input_blob, input_buffer = _blob_from_bytes(data)
    entropy_blob, entropy_buffer = _blob_from_bytes(entropy)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    success = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "亦欣AI生图工作台",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    )
    _ = input_buffer, entropy_buffer
    if not success:
        raise SecretStorageError(f"API Key 加密失败，Windows 错误码 {ctypes.GetLastError()}")
    try:
        return _blob_to_bytes(output_blob)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _dpapi_unprotect(data: bytes, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStorageError("DPAPI 仅支持 Windows")
    input_blob, input_buffer = _blob_from_bytes(data)
    entropy_blob, entropy_buffer = _blob_from_bytes(entropy)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    description = wintypes.LPWSTR()
    success = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    )
    _ = input_buffer, entropy_buffer
    if description:
        kernel32.LocalFree(description)
    if not success:
        raise SecretStorageError(f"API Key 解密失败，Windows 错误码 {ctypes.GetLastError()}")
    try:
        return _blob_to_bytes(output_blob)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def validate_api_key(value: str) -> str:
    key = value.strip()
    if not MIN_API_KEY_LENGTH <= len(key) <= MAX_API_KEY_LENGTH:
        raise ValueError(f"API Key 长度应为 {MIN_API_KEY_LENGTH}–{MAX_API_KEY_LENGTH} 个字符")
    if any(character.isspace() for character in key):
        raise ValueError("API Key 不能包含空格或换行")
    return key


def mask_secret(value: str | None) -> str:
    if not value:
        return "未设置"
    if len(value) <= 8:
        return "已设置（••••）"
    return f"{value[:3]}••••{value[-4:]}"


class SecretStore:
    ENTROPY = b"yixin-ai-image-workbench-v1"

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, api_key: str) -> None:
        clean = validate_api_key(api_key)
        encrypted = _dpapi_protect(clean.encode("utf-8"), self.ENTROPY)
        atomic_write_bytes(self.path, encrypted)

    def load(self, *, strict: bool = False) -> str | None:
        if not self.path.exists():
            return None
        try:
            encrypted = self.path.read_bytes()
            value = _dpapi_unprotect(encrypted, self.ENTROPY).decode("utf-8")
            return validate_api_key(value)
        except Exception as exc:
            if strict:
                raise SecretStorageError("无法解密已保存的 API Key") from exc
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
