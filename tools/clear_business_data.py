from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from yixin.collaboration.repository import MySQLCentralRepository, MySQLConfig
from yixin.private_config import DATABASE


BUSINESS_TABLES = (
    "generation_results",
    "generation_provider_calls",
    "task_reviews",
    "workflow_events",
    "task_garments",
    "generation_attempts",
    "project_tasks",
    "project_assets",
    "projects",
    "task_code_sequences",
)
AUTO_INCREMENT_TABLES = (
    "generation_results",
    "generation_provider_calls",
    "task_reviews",
    "workflow_events",
    "generation_attempts",
    "project_tasks",
    "project_assets",
    "projects",
)


def counts(cursor) -> dict[str, int]:
    result = {}
    for table in BUSINESS_TABLES:
        cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
        result[table] = int(cursor.fetchone()["count"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="清空任务协同业务数据，保留账号、模板、价格和表结构。"
    )
    parser.add_argument(
        "--confirm",
        required=True,
        help="输入“清空业务数据”才会执行。",
    )
    args = parser.parse_args()
    if args.confirm != "清空业务数据":
        raise SystemExit("确认文本不正确，未删除任何数据。")

    repository = MySQLCentralRepository(MySQLConfig(**DATABASE))
    with repository.connection() as connection:
        with connection.cursor() as cursor:
            before = counts(cursor)
            print("清理前：", before)
            if not any(before.values()):
                print("业务数据已经为空。")
                return
            for table in BUSINESS_TABLES:
                cursor.execute(f"DELETE FROM {table}")
            after = counts(cursor)
            if any(after.values()):
                raise RuntimeError(f"清理后仍有业务数据：{after}")

    with repository.connection() as connection:
        with connection.cursor() as cursor:
            for table in AUTO_INCREMENT_TABLES:
                cursor.execute(f"ALTER TABLE {table} AUTO_INCREMENT = 1")
    print("业务数据已清空，账号、模板、价格、共享设置和表结构已保留。")


if __name__ == "__main__":
    main()
