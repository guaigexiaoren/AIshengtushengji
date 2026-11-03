from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from yixin.paths import AppPaths  # noqa: E402
from yixin.private_config import DATABASE  # noqa: E402
from yixin.collaboration.repository import (  # noqa: E402
    MySQLCentralRepository,
    MySQLConfig,
)


CONFIRM_TEXT = "清空全部数据"
DATA_TABLES = (
    "user_api_keys",
    "user_menu_permissions",
    "user_project_access",
    "task_garments",
    "generation_provider_calls",
    "generation_results",
    "generation_attempts",
    "task_reviews",
    "workflow_events",
    "project_tasks",
    "project_assets",
    "projects",
    "prompt_templates",
    "model_prices",
    "shared_settings",
    "task_code_sequences",
    "api_provider_models",
    "api_providers",
    "users",
)


def _json_value(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _connection():
    return pymysql.connect(
        host=DATABASE["host"],
        port=int(DATABASE["port"]),
        user=DATABASE["user"],
        password=DATABASE["password"].encode("utf-8"),
        database=DATABASE["database"],
        charset=DATABASE["charset"],
        cursorclass=DictCursor,
        connect_timeout=8,
        read_timeout=30,
        write_timeout=30,
        autocommit=False,
    )


def backup_mysql(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    connection = _connection()
    try:
        with connection.cursor() as cursor:
            for table in DATA_TABLES:
                cursor.execute(f"SELECT * FROM `{table}`")
                rows = [
                    {
                        key: _json_value(value)
                        for key, value in row.items()
                    }
                    for row in cursor.fetchall()
                ]
                (destination / f"{table}.json").write_text(
                    json.dumps(
                        rows,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
    finally:
        connection.close()
    return destination


def clear_mysql() -> None:
    connection = _connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            try:
                for table in DATA_TABLES:
                    cursor.execute(f"TRUNCATE TABLE `{table}`")
            finally:
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def backup_local_data(paths: AppPaths, destination: Path) -> Path:
    if paths.root.exists():
        shutil.copytree(paths.root, destination)
    else:
        destination.mkdir(parents=True)
    return destination


def clear_local_data(paths: AppPaths) -> None:
    root = paths.root.resolve()
    if root == root.parent:
        raise ValueError("拒绝清空文件系统根目录")
    if root == Path.home().resolve():
        raise ValueError("拒绝清空用户主目录")
    if root.exists():
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    paths.ensure()


def _default_backup_root() -> Path:
    return Path.home() / "Desktop" / "yixin-data-backups"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="备份并清空亦欣 AI 的 MySQL 与本机数据"
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="备份目录，默认保存到桌面 yixin-data-backups",
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="只备份，不清空数据",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"清空时必须传入“{CONFIRM_TEXT}”",
    )
    args = parser.parse_args()

    paths = AppPaths.default()
    backup_root = (
        args.backup_dir.expanduser().resolve()
        if args.backup_dir
        else _default_backup_root()
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    if paths.is_under(backup_root, paths.root):
        print("备份目录不能位于待清空的数据目录内。", file=sys.stderr)
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / stamp
    backup_dir.mkdir()
    print(f"正在备份 MySQL 到：{backup_dir / 'mysql'}")
    backup_mysql(backup_dir / "mysql")
    print(f"正在备份本机数据到：{backup_dir / 'local-data'}")
    backup_local_data(paths, backup_dir / "local-data")
    print(f"备份完成：{backup_dir}")
    if args.backup_only:
        return 0
    confirmation = args.confirm
    if not confirmation:
        confirmation = input(
            f"此操作不可恢复。请输入“{CONFIRM_TEXT}”继续："
        )
    if confirmation != CONFIRM_TEXT:
        print("确认文本不正确，未清空数据。", file=sys.stderr)
        return 2
    print("正在清空 MySQL…")
    clear_mysql()
    repository = MySQLCentralRepository(MySQLConfig(**DATABASE))
    repository.ensure_schema()
    print("已恢复默认 API 平台、模型路由和共享设置。")
    print(f"正在清空本机数据目录：{paths.root}")
    clear_local_data(paths)
    print("全部数据已清空。下次启动应用时会要求创建首个管理员。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
