# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PySide6.QtCore import QLibraryInfo


translations_dir = Path(
    QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
)
qtbase_translation = translations_dir / "qtbase_zh_CN.qm"


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/app_icon.ico", "assets"),
        ("assets/app_icon.png", "assets"),
        ("yixin/collaboration/schema.sql", "yixin/collaboration"),
        (str(qtbase_translation), "assets/translations"),
    ],
    hiddenimports=["pymysql", "openpyxl", "cryptography"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="亦欣AI生图工作台",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon="assets/app_icon.ico",
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="亦欣AI生图工作台",
)
