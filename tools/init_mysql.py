from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pymysql import MySQLError  # noqa: E402

from yixin.collaboration.repository import (  # noqa: E402
    MySQLCentralRepository,
    MySQLConfig,
)
from yixin.collaboration.types import ROLE_ADMIN, AuthUser  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化亦欣 AI 协同数据库")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--admin-account", default="admin")
    parser.add_argument("--admin-name", default="系统管理员")
    parser.add_argument(
        "--reset-admin",
        action="store_true",
        help="用户表已有账号时，重置指定管理员密码",
    )
    args = parser.parse_args()

    password = getpass.getpass("数据库密码：")
    repository = MySQLCentralRepository(
        MySQLConfig(
            host=args.host,
            port=args.port,
            database=args.database,
            user=args.user,
            password=password,
        )
    )
    try:
        version = repository.ensure_schema()
        print(f"表结构初始化完成，版本：{version}")
        users = repository.list_users()
        if users and not args.reset_admin:
            print("用户表已有账号，未创建新管理员。")
            return 0
        temporary = secrets.token_urlsafe(12)
        if users:
            admin = next(
                (user for user in users if user.username == args.admin_account),
                None,
            )
            if admin is None:
                bootstrap = AuthUser(
                    id=0,
                    username="bootstrap",
                    display_name="维护脚本",
                    role=ROLE_ADMIN,
                )
                repository.create_user(
                    bootstrap,
                    args.admin_account,
                    args.admin_name,
                    ROLE_ADMIN,
                    temporary,
                )
            else:
                repository.reset_password(admin, admin.id, temporary)
        else:
            repository.create_initial_admin(
                args.admin_account,
                args.admin_name,
                temporary,
            )
        print(f"管理员账号：{args.admin_account}")
        print(f"临时密码：{temporary}")
        print("首次登录后程序会强制修改密码。")
        return 0
    except MySQLError as exc:
        print(f"数据库操作失败：{exc.args[-1] if exc.args else exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
