from __future__ import annotations

import json
import queue
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Sequence

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from ..constants import (
    BUILTIN_PROMPT_TEMPLATES,
    MODEL_IDS,
    PROVIDER_PROTOCOLS,
)
from ..private_config import SHARED_ROOT
from ..paging import HistoryQuery, PageRequest, PageResult, TaskQuery
from ..security import validate_api_key
from ..time_utils import local_to_utc_naive
from .api_keys import ApiKeyError, decrypt_api_key, encrypt_api_key
from .auth import hash_password, validate_new_password, verify_password
from .reports import (
    REPORT_DATE_COMPLETED,
    REPORT_DATE_CREATED,
    GroupMetrics,
    ReportData,
    ReportFilter,
    ReportTaskRow,
    aggregate_report,
)
from .types import (
    ATTEMPT_FAILED,
    ATTEMPT_CANCELLED,
    ATTEMPT_QUEUED,
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    PROJECT_CANCELLED,
    PROJECT_COMPLETED,
    PROJECT_IN_PROGRESS,
    PROJECT_SUBMITTED,
    ROLE_ADMIN,
    ROLE_AI,
    ROLE_OPERATIONS,
    ROLES,
    TASK_APPROVED,
    TASK_AVAILABLE,
    TASK_AWAITING_ACCEPTANCE,
    TASK_CANCELLED,
    TASK_CLAIMED,
    TASK_EXPANDED,
    TASK_RETURNED,
    TASK_SKIPPED,
    TASK_WORKING,
    AuthUser,
    ApiProvider,
    GeneratedResultInput,
    GenerationPairSpec,
    GenerationAttempt,
    GenerationResult,
    OperationAccountCandidate,
    OperationAccountSyncResult,
    PairingInput,
    ProjectAssetInput,
    ProjectDraft,
    ProjectRecord,
    ProjectTask,
    ProviderModelRoute,
    SplitGenerationItem,
    PROJECT_SCOPES,
    ROLE_DEFAULT_MENUS,
    ROLE_ALLOWED_MENUS,
)

SCHEMA_VERSION = 11
LEGACY_SHARED_ROOT = (
    r"\\fe80--211-32ff-fe87-ba49.ipv6-literal.net\AI作图"
)


class CentralRepositoryError(RuntimeError):
    pass


class SchemaNotInitializedError(CentralRepositoryError):
    pass


class PermissionDeniedError(CentralRepositoryError):
    pass


class StateConflictError(CentralRepositoryError):
    pass


class GenerationSlotBusyError(StateConflictError):
    """账号并发名额已满，排队轮次应保持排队稍后重试。"""


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    charset: str = "utf8mb4"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _ConnectionPool:
    """复用小数量 MySQL 连接，避免每次查询都重新握手认证。"""

    def __init__(self, factory, max_size: int = 8) -> None:
        self._factory = factory
        self._max_size = max_size
        self._free: queue.LifoQueue = queue.LifoQueue(maxsize=max_size)
        self._lock = threading.Lock()
        self._created = 0

    def acquire(self) -> Connection:
        while True:
            try:
                connection = self._free.get_nowait()
            except queue.Empty:
                break
            if self._alive(connection):
                return connection
            self._discard(connection)
        with self._lock:
            can_create = self._created < self._max_size
            if can_create:
                self._created += 1
        if can_create:
            try:
                return self._factory()
            except Exception:
                with self._lock:
                    self._created -= 1
                raise
        try:
            connection = self._free.get(timeout=30)
        except queue.Empty as exc:
            raise CentralRepositoryError(
                "等待数据库连接超时，请稍后重试"
            ) from exc
        if self._alive(connection):
            return connection
        self._discard(connection)
        return self.acquire()

    def release(self, connection: Connection) -> None:
        try:
            self._free.put_nowait(connection)
        except queue.Full:
            self._discard(connection)

    @staticmethod
    def _alive(connection: Connection) -> bool:
        try:
            connection.ping(reconnect=False)
            return True
        except Exception:
            return False

    def _discard(self, connection: Connection) -> None:
        with self._lock:
            self._created = max(0, self._created - 1)
        try:
            connection.close()
        except Exception:
            pass


class MySQLCentralRepository:
    def __init__(self, config: MySQLConfig) -> None:
        self.config = config
        self._pool = _ConnectionPool(self._connect, max_size=16)

    def _connect(self) -> Connection:
        try:
            return pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password.encode("utf-8"),
                database=self.config.database,
                charset=self.config.charset,
                cursorclass=DictCursor,
                connect_timeout=8,
                read_timeout=30,
                write_timeout=30,
                autocommit=False,
            )
        except pymysql.MySQLError as exc:
            raise CentralRepositoryError(
                f"无法连接内网数据库：{_mysql_error_message(exc)}"
            ) from exc

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        connection = self._pool.acquire()
        try:
            yield connection
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            self._pool.release(connection)

    def test_connection(self) -> dict[str, Any]:
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT DATABASE() AS db, VERSION() AS version, "
                        "UTC_TIMESTAMP(6) AS server_time"
                    )
                    return dict(cursor.fetchone())
        except pymysql.MySQLError as exc:
            raise CentralRepositoryError(_mysql_error_message(exc)) from exc

    def schema_version(self) -> int:
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT MAX(version) AS version FROM schema_migrations")
                    row = cursor.fetchone()
                    return int((row or {}).get("version") or 0)
        except pymysql.MySQLError as exc:
            if exc.args and exc.args[0] == 1146:
                raise SchemaNotInitializedError(
                    "协同数据库尚未初始化"
                ) from exc
            raise CentralRepositoryError(_mysql_error_message(exc)) from exc

    def ensure_schema(self) -> int:
        schema = _load_schema()
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    for statement in _split_sql(schema):
                        cursor.execute(statement)
                    self._apply_schema_v2(cursor)
                    self._apply_schema_v3(cursor)
                    self._apply_schema_v4(cursor)
                    self._apply_schema_v5(cursor)
                    self._apply_schema_v6(cursor)
                    self._apply_schema_v7(cursor)
                    self._apply_schema_v8(cursor)
                    self._apply_schema_v9(cursor)
                    self._apply_schema_v10(cursor)
                    self._apply_schema_v11(cursor)
                    self._apply_shared_root_update(cursor)
                    cursor.execute(
                        "INSERT IGNORE INTO schema_migrations(version, applied_at) "
                        "VALUES (%s, %s)",
                        (SCHEMA_VERSION, utc_now()),
                    )
        except pymysql.MySQLError as exc:
            raise CentralRepositoryError(
                f"初始化协同数据库失败：{_mysql_error_message(exc)}"
            ) from exc
        return self.schema_version()

    @staticmethod
    def _apply_schema_v2(cursor) -> None:
        user_columns = {
            "urgent_quota_per_day": "INT NOT NULL DEFAULT 3",
            "max_generation_concurrency": "INT NOT NULL DEFAULT 2",
        }
        for column, definition in user_columns.items():
            _add_column_if_missing(cursor, "users", column, definition)
        project_columns = {
            "source_date": "DATE NULL",
            "product_code": "VARCHAR(200) NOT NULL DEFAULT ''",
            "source_revision": "INT NOT NULL DEFAULT 1",
        }
        for column, definition in project_columns.items():
            _add_column_if_missing(cursor, "projects", column, definition)
        task_columns = {
            "source_revision": "INT NOT NULL DEFAULT 1",
            "is_urgent": "TINYINT(1) NOT NULL DEFAULT 0",
            "urgent_date": "DATE NULL",
            "urgent_by": "BIGINT UNSIGNED NULL",
        }
        for column, definition in task_columns.items():
            _add_column_if_missing(cursor, "project_tasks", column, definition)
        _add_column_if_missing(
            cursor,
            "generation_attempts",
            "source_revision",
            "INT NOT NULL DEFAULT 1",
        )
        result_columns = {
            "source_type": "VARCHAR(24) NOT NULL DEFAULT 'generated'",
            "source_revision": "INT NOT NULL DEFAULT 1",
        }
        for column, definition in result_columns.items():
            _add_column_if_missing(cursor, "generation_results", column, definition)
        cursor.execute(
            """
            UPDATE projects
            SET source_date = COALESCE(source_date, DATE(created_at)),
                product_code = CASE
                    WHEN product_code = '' THEN COALESCE(NULLIF(name, ''), project_code)
                    ELSE product_code
                END
            """
        )
        cursor.execute(
            """
            INSERT IGNORE INTO task_garments(task_id, garment_asset_id, position)
            SELECT id, garment_asset_id, 1
            FROM project_tasks
            """
        )
        cursor.execute(
            """
            INSERT INTO task_code_sequences(
                owner_id, sequence_date, current_value
            )
            SELECT owner_id, source_date, COUNT(*)
            FROM projects
            WHERE source_date IS NOT NULL
            GROUP BY owner_id, source_date
            ON DUPLICATE KEY UPDATE
                current_value = GREATEST(current_value, VALUES(current_value))
            """
        )
        cursor.execute(
            """
            INSERT IGNORE INTO shared_settings(
                setting_key, value_json, updated_by, updated_at
            ) VALUES ('share_root', %s, NULL, %s)
            """,
            (json.dumps(SHARED_ROOT, ensure_ascii=False), utc_now()),
        )

    @staticmethod
    def _apply_schema_v3(cursor) -> None:
        cursor.execute(
            "SELECT COALESCE(MAX(version), 0) AS version "
            "FROM schema_migrations"
        )
        previous_version = int(cursor.fetchone()["version"])
        project_columns = {
            "project_scope": "VARCHAR(24) NOT NULL DEFAULT 'internal'",
            "project_name": "VARCHAR(200) NOT NULL DEFAULT ''",
            "is_urgent": "TINYINT(1) NOT NULL DEFAULT 0",
            "urgent_date": "DATE NULL",
            "urgent_by": "BIGINT UNSIGNED NULL",
        }
        for column, definition in project_columns.items():
            _add_column_if_missing(cursor, "projects", column, definition)
        cursor.execute(
            """
            UPDATE projects
            SET project_name = CASE
                WHEN project_name = '' THEN COALESCE(NULLIF(product_code, ''), name)
                ELSE project_name
            END
            """
        )
        if previous_version >= 3:
            return
        cursor.execute("SELECT id, role FROM users")
        for user in cursor.fetchall():
            for menu_key in ROLE_DEFAULT_MENUS.get(user["role"], ()):
                cursor.execute(
                    """
                    INSERT IGNORE INTO user_menu_permissions(user_id, menu_key)
                    VALUES (%s, %s)
                    """,
                    (user["id"], menu_key),
                )

    @staticmethod
    def _apply_schema_v4(cursor) -> None:
        attempt_columns = {
            "provider_id": "BIGINT UNSIGNED NULL",
            "provider_code": "VARCHAR(64) NOT NULL DEFAULT ''",
            "provider_name": "VARCHAR(100) NOT NULL DEFAULT ''",
            "provider_model": "VARCHAR(160) NOT NULL DEFAULT ''",
        }
        for column, definition in attempt_columns.items():
            _add_column_if_missing(
                cursor,
                "generation_attempts",
                column,
                definition,
            )
        _add_column_if_missing(
            cursor,
            "user_api_keys",
            "provider_id",
            "BIGINT UNSIGNED NOT NULL DEFAULT 1",
        )
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'user_api_keys'
              AND CONSTRAINT_NAME = 'PRIMARY'
            ORDER BY ORDINAL_POSITION
            """
        )
        primary_columns = tuple(
            row["COLUMN_NAME"] for row in cursor.fetchall()
        )
        if primary_columns and primary_columns != ("user_id", "provider_id"):
            cursor.execute(
                "ALTER TABLE user_api_keys DROP PRIMARY KEY, "
                "ADD PRIMARY KEY(user_id, provider_id)"
            )

    @staticmethod
    def _apply_schema_v5(cursor) -> None:
        cursor.execute(
            "SELECT COALESCE(MAX(version), 0) AS version "
            "FROM schema_migrations"
        )
        previous_version = int(cursor.fetchone()["version"])
        task_columns = {
            "parent_task_id": "BIGINT UNSIGNED NULL",
            "split_at": "DATETIME(6) NULL",
        }
        for column, definition in task_columns.items():
            _add_column_if_missing(
                cursor,
                "project_tasks",
                column,
                definition,
            )
        if previous_version >= 5:
            return
        cursor.execute(
            """
            INSERT IGNORE INTO user_menu_permissions(user_id, menu_key)
            SELECT user_id, 'assign_tasks'
            FROM user_menu_permissions
            WHERE menu_key = 'task_center'
            """
        )
        cursor.execute(
            """
            INSERT IGNORE INTO user_menu_permissions(user_id, menu_key)
            SELECT user_id, 'ai_generate'
            FROM user_menu_permissions
            WHERE menu_key = 'task_center'
            """
        )
        cursor.execute(
            """
            DELETE FROM user_menu_permissions
            WHERE menu_key IN (
                'task_center', 'submitted', 'pending', 'completed'
            )
            """
        )

    @staticmethod
    def _apply_schema_v6(cursor) -> None:
        attempt_columns = {
            "requested_provider_code":
                "VARCHAR(64) NOT NULL DEFAULT ''",
            "requested_provider_name":
                "VARCHAR(100) NOT NULL DEFAULT ''",
        }
        for column, definition in attempt_columns.items():
            _add_column_if_missing(
                cursor,
                "generation_attempts",
                column,
                definition,
            )

    @staticmethod
    def _apply_schema_v7(cursor) -> None:
        cursor.execute(
            "SELECT COALESCE(MAX(version), 0) AS version "
            "FROM schema_migrations"
        )
        previous_version = int(cursor.fetchone()["version"])
        if previous_version >= 7:
            return
        cursor.execute(
            """
            INSERT IGNORE INTO user_menu_permissions(user_id, menu_key)
            SELECT id, 'submission_history'
            FROM users
            WHERE role IN (%s, %s)
            """,
            (ROLE_OPERATIONS, ROLE_ADMIN),
        )

    @staticmethod
    def _apply_schema_v8(cursor) -> None:
        _add_index_if_missing(
            cursor,
            "projects",
            "idx_projects_scope_name_date",
            "(project_scope, project_name, source_date, id)",
        )
        _add_index_if_missing(
            cursor,
            "projects",
            "idx_projects_created_id",
            "(created_at, id)",
        )
        _add_index_if_missing(
            cursor,
            "project_tasks",
            "idx_project_tasks_project_status_created",
            "(project_id, status, created_at, id)",
        )
        _add_index_if_missing(
            cursor,
            "project_tasks",
            "idx_project_tasks_assignee_status_created",
            "(assignee_id, status, created_at, id)",
        )
        _add_index_if_missing(
            cursor,
            "generation_attempts",
            "idx_attempts_created_id",
            "(created_at, id)",
        )
        _add_index_if_missing(
            cursor,
            "generation_results",
            "idx_generation_results_result_path",
            "(relative_path)",
        )
        _add_index_if_missing(
            cursor,
            "generation_results",
            "idx_generation_results_thumb_path",
            "(thumbnail_relative_path)",
        )
        cursor.execute(
            """
            DELETE FROM user_menu_permissions
            WHERE menu_key = 'submission_history'
              AND user_id IN (
                  SELECT id FROM users WHERE role = %s
              )
            """,
            (ROLE_AI,),
        )

    @staticmethod
    def _apply_schema_v9(cursor) -> None:
        cursor.execute(
            "SELECT COALESCE(MAX(version), 0) AS version "
            "FROM schema_migrations"
        )
        previous_version = int(cursor.fetchone()["version"])
        if previous_version >= 9:
            return
        cursor.execute(
            """
            SELECT display_name, COUNT(*) AS count
            FROM users
            GROUP BY display_name
            HAVING COUNT(*) > 1
            ORDER BY display_name
            """
        )
        duplicates = cursor.fetchall()
        if duplicates:
            names = "、".join(
                f"{row['display_name']}（{row['count']} 个）"
                for row in duplicates
            )
            raise CentralRepositoryError(
                "账号显示名称存在重复，无法升级数据库。请先修改重复账号："
                + names
            )
        _add_index_if_missing(
            cursor,
            "users",
            "uk_users_display_name",
            "(display_name)",
            unique=True,
        )

    @staticmethod
    def _apply_schema_v10(cursor) -> None:
        _add_column_if_missing(
            cursor,
            "users",
            "deleted_at",
            "DATETIME(6) NULL",
        )
        _drop_index_if_exists(cursor, "users", "username")
        _drop_index_if_exists(cursor, "users", "uk_users_display_name")
        _add_column_if_missing(
            cursor,
            "users",
            "active_username",
            "VARCHAR(64) GENERATED ALWAYS AS "
            "(IF(deleted_at IS NULL, username, NULL)) STORED",
        )
        _add_column_if_missing(
            cursor,
            "users",
            "active_display_name",
            "VARCHAR(100) GENERATED ALWAYS AS "
            "(IF(deleted_at IS NULL, display_name, NULL)) STORED",
        )
        _add_index_if_missing(
            cursor,
            "users",
            "uk_users_active_username",
            "(active_username)",
            unique=True,
        )
        _add_index_if_missing(
            cursor,
            "users",
            "uk_users_active_display_name",
            "(active_display_name)",
            unique=True,
        )

    @staticmethod
    def _apply_schema_v11(cursor) -> None:
        cursor.execute(
            "SELECT COALESCE(MAX(version), 0) AS version "
            "FROM schema_migrations"
        )
        previous_version = int(cursor.fetchone()["version"])
        if previous_version >= 11:
            return
        cursor.execute(
            """
            INSERT IGNORE INTO user_menu_permissions(user_id, menu_key)
            SELECT id, 'reports'
            FROM users
            WHERE role = %s AND deleted_at IS NULL
            """,
            (ROLE_ADMIN,),
        )

    @staticmethod
    def _apply_shared_root_update(cursor) -> None:
        cursor.execute(
            """
            SELECT value_json FROM shared_settings
            WHERE setting_key = 'share_root'
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if row is None:
            return
        try:
            current_root = json.loads(row["value_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if str(current_root).casefold() != LEGACY_SHARED_ROOT.casefold():
            return
        cursor.execute(
            """
            UPDATE shared_settings
            SET value_json = %s, updated_at = %s
            WHERE setting_key = 'share_root'
            """,
            (json.dumps(SHARED_ROOT, ensure_ascii=False), utc_now()),
        )

    def create_initial_admin(
        self,
        username: str,
        display_name: str,
        password: str,
    ) -> AuthUser:
        display_name = _validate_display_name(display_name)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM users WHERE deleted_at IS NULL LIMIT 1"
                )
                if cursor.fetchone() is not None:
                    raise StateConflictError("用户表已有账号，不能重复初始化管理员")
                user_id = self._insert_user(
                    cursor,
                    username,
                    display_name,
                    ROLE_ADMIN,
                    password,
                    must_change_password=True,
                )
                self._seed_user_menu_permissions(
                    cursor,
                    user_id,
                    ROLE_ADMIN,
                )
                self._seed_defaults(cursor)
        return AuthUser(
            id=user_id,
            username=username,
            display_name=display_name,
            role=ROLE_ADMIN,
            must_change_password=True,
        )

    def authenticate_user(self, username: str, password: str) -> AuthUser:
        clean_username = username.strip()
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM users "
                        "WHERE username = %s AND deleted_at IS NULL",
                        (clean_username,),
                    )
                    row = cursor.fetchone()
                    if row is None or not verify_password(password, row["password_hash"]):
                        raise PermissionDeniedError("账号或密码不正确")
                    if not bool(row["is_active"]):
                        raise PermissionDeniedError("账号已停用，请联系管理员")
                    cursor.execute(
                        "UPDATE users SET last_login_at = %s WHERE id = %s",
                        (utc_now(), row["id"]),
                    )
                    return _auth_user(row)
        except pymysql.MySQLError as exc:
            raise CentralRepositoryError(_mysql_error_message(exc)) from exc

    def change_password(
        self,
        user_id: int,
        old_password: str,
        new_password: str,
    ) -> None:
        clean = validate_new_password(new_password)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT password_hash FROM users "
                    "WHERE id = %s AND deleted_at IS NULL",
                    (user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise PermissionDeniedError("账号不存在")
                if not verify_password(old_password, row["password_hash"]):
                    raise PermissionDeniedError("原密码不正确")
                cursor.execute(
                    """
                    UPDATE users
                    SET password_hash = %s, must_change_password = 0, updated_at = %s
                    WHERE id = %s
                    """,
                    (hash_password(clean), utc_now(), user_id),
                )

    def list_users(self, role: str | None = None) -> list[AuthUser]:
        query = "SELECT * FROM users WHERE deleted_at IS NULL"
        params: tuple[Any, ...] = ()
        if role:
            query += " AND role = %s"
            params = (role,)
        query += " ORDER BY is_active DESC, role, display_name"
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return [_auth_user(row) for row in cursor.fetchall()]

    def load_user_access(self, user: AuthUser) -> AuthUser:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT menu_key FROM user_menu_permissions
                    WHERE user_id = %s ORDER BY menu_key
                    """,
                    (user.id,),
                )
                menus = tuple(
                    row["menu_key"] for row in cursor.fetchall()
                )
                cursor.execute(
                    """
                    SELECT project_scope, project_name
                    FROM user_project_access
                    WHERE user_id = %s
                    ORDER BY project_scope, project_name
                    """,
                    (user.id,),
                )
                projects = tuple(
                    (row["project_scope"], row["project_name"])
                    for row in cursor.fetchall()
                )
        return AuthUser(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            must_change_password=user.must_change_password,
            is_active=user.is_active,
            urgent_quota_per_day=user.urgent_quota_per_day,
            max_generation_concurrency=user.max_generation_concurrency,
            menu_permissions=menus,
            project_access=projects,
        )

    def user_menu_permissions(self, user_id: int) -> tuple[str, ...]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT menu_key FROM user_menu_permissions
                    WHERE user_id = %s ORDER BY menu_key
                    """,
                    (user_id,),
                )
                return tuple(
                    row["menu_key"] for row in cursor.fetchall()
                )

    def user_project_access(
        self,
        user_id: int,
    ) -> tuple[tuple[str, str], ...]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT project_scope, project_name
                    FROM user_project_access
                    WHERE user_id = %s
                    ORDER BY project_scope, project_name
                    """,
                    (user_id,),
                )
                return tuple(
                    (row["project_scope"], row["project_name"])
                    for row in cursor.fetchall()
                )

    def list_shared_projects(self) -> dict[str, tuple[str, ...]]:
        root = Path(
            self.get_shared_setting("share_root") or SHARED_ROOT
        ).expanduser()
        result: dict[str, tuple[str, ...]] = {}
        for scope, folder_name in (
            ("internal", "内部项目"),
            ("external", "外部项目"),
        ):
            folder = root / folder_name
            names = (
                tuple(
                    sorted(
                        (
                            path.name
                            for path in folder.iterdir()
                            if path.is_dir()
                        ),
                        key=str.casefold,
                    )
                )
                if folder.is_dir()
                else ()
            )
            result[scope] = names
        return result

    def list_accessible_projects(
        self,
        actor: AuthUser,
    ) -> tuple[tuple[str, str], ...]:
        if actor.role == ROLE_OPERATIONS:
            return actor.project_access or self.user_project_access(actor.id)
        discovered = self.list_shared_projects()
        return tuple(
            (scope, name)
            for scope in PROJECT_SCOPES
            for name in discovered.get(scope, ())
        )

    def create_user(
        self,
        actor: AuthUser,
        username: str,
        display_name: str,
        role: str,
        temporary_password: str,
        *,
        urgent_quota_per_day: int = 3,
        max_generation_concurrency: int = 2,
        menu_permissions: Sequence[str] | None = None,
        project_access: Sequence[tuple[str, str]] | None = None,
    ) -> AuthUser:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以新增账号")
        username = username.strip()
        display_name = _validate_display_name(display_name)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            raise ValueError("账号只能包含字母、数字、点、下划线或短横线，长度 3–64")
        if not display_name:
            raise ValueError("姓名不能为空")
        if role not in ROLES:
            raise ValueError("不支持的账号角色")
        if not 0 <= urgent_quota_per_day <= 99:
            raise ValueError("加急额度必须在 0 到 99 之间")
        if not 1 <= max_generation_concurrency <= 10:
            raise ValueError("生成并发数必须在 1 到 10 之间")
        validate_new_password(temporary_password)
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM users "
                        "WHERE display_name = %s AND deleted_at IS NULL "
                        "LIMIT 1",
                        (display_name,),
                    )
                    if cursor.fetchone() is not None:
                        raise StateConflictError("显示名称或项目名已被其他账号使用")
                    user_id = self._insert_user(
                        cursor,
                        username,
                        display_name,
                        role,
                        temporary_password,
                        must_change_password=True,
                    )
                    cursor.execute(
                        """
                        UPDATE users
                        SET urgent_quota_per_day = %s,
                            max_generation_concurrency = %s
                        WHERE id = %s
                        """,
                        (
                            urgent_quota_per_day,
                            max_generation_concurrency,
                            user_id,
                        ),
                    )
                    selected_menus = (
                        tuple(
                            dict.fromkeys(
                                key
                                for key in menu_permissions
                                if key in ROLE_ALLOWED_MENUS.get(role, ())
                            )
                        )
                        if menu_permissions is not None
                        else ROLE_DEFAULT_MENUS.get(role, ())
                    )
                    for menu_key in selected_menus:
                        cursor.execute(
                            """
                            INSERT INTO user_menu_permissions(user_id, menu_key)
                            VALUES (%s, %s)
                            """,
                            (user_id, menu_key),
                        )
                    for scope, project_name in dict.fromkeys(
                        project_access or ()
                    ):
                        if (
                            scope not in PROJECT_SCOPES
                            or not str(project_name).strip()
                        ):
                            continue
                        cursor.execute(
                            """
                            INSERT INTO user_project_access(
                                user_id, project_scope, project_name
                            ) VALUES (%s, %s, %s)
                            """,
                            (
                                user_id,
                                scope,
                                str(project_name).strip(),
                            ),
                        )
        except pymysql.err.IntegrityError as exc:
            raise StateConflictError("账号已存在") from exc
        return AuthUser(
            user_id,
            username,
            display_name,
            role,
            True,
            True,
            urgent_quota_per_day,
            max_generation_concurrency,
            selected_menus,
            tuple(dict.fromkeys(project_access or ())),
        )

    def preview_operation_accounts(
        self,
        actor: AuthUser,
    ) -> tuple[OperationAccountCandidate, ...]:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以创建运营账号")
        folders = self.list_shared_projects()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                return self._operation_account_candidates(
                    cursor,
                    folders,
                )

    def sync_operation_accounts_from_shared(
        self,
        actor: AuthUser,
    ) -> OperationAccountSyncResult:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以创建运营账号")
        folders = self.list_shared_projects()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                candidates = self._operation_account_candidates(
                    cursor,
                    folders,
                )
                conflicts = tuple(
                    item.display_name
                    for item in candidates
                    if item.status == "conflict"
                )
                if conflicts:
                    raise StateConflictError(
                        "以下项目存在账号冲突，本次创建已全部取消："
                        + "、".join(conflicts)
                    )
                created: list[str] = []
                skipped = [
                    item.display_name
                    for item in candidates
                    if item.status == "skip"
                ]
                for item in candidates:
                    if item.status != "create":
                        continue
                    user_id = self._insert_user(
                        cursor,
                        item.username,
                        item.display_name,
                        ROLE_OPERATIONS,
                        "123456",
                        must_change_password=True,
                    )
                    for menu_key in ROLE_DEFAULT_MENUS[ROLE_OPERATIONS]:
                        cursor.execute(
                            """
                            INSERT INTO user_menu_permissions(
                                user_id, menu_key
                            ) VALUES (%s, %s)
                            """,
                            (user_id, menu_key),
                        )
                    for scope, name in item.project_access:
                        cursor.execute(
                            """
                            INSERT INTO user_project_access(
                                user_id, project_scope, project_name
                            ) VALUES (%s, %s, %s)
                            """,
                            (user_id, scope, name),
                        )
                    created.append(item.username)
        return OperationAccountSyncResult(
            created=tuple(created),
            skipped=tuple(skipped),
            conflicts=(),
        )

    @staticmethod
    def _operation_account_candidates(
        cursor,
        folders: dict[str, tuple[str, ...]],
    ) -> tuple[OperationAccountCandidate, ...]:
        projects_by_name: dict[str, tuple[str, list[str]]] = {}
        for scope in PROJECT_SCOPES:
            for name in folders.get(scope, ()):
                clean = str(name).strip()
                if not clean:
                    continue
                key = clean.casefold()
                if key not in projects_by_name:
                    projects_by_name[key] = (clean, [])
                projects_by_name[key][1].append(scope)

        cursor.execute(
            """
            SELECT id, username, display_name, role, is_active
            FROM users
            WHERE deleted_at IS NULL
            """
        )
        users = cursor.fetchall()
        users_by_display = {
            str(row["display_name"]).casefold(): row for row in users
        }
        used_usernames = {
            str(row["username"]).casefold() for row in users
        }
        next_number = 1
        for username in used_usernames:
            match = re.fullmatch(r"yy(\d+)", username)
            if match:
                next_number = max(next_number, int(match.group(1)) + 1)

        cursor.execute(
            """
            SELECT u.display_name, upa.project_scope, upa.project_name
            FROM users u
            JOIN user_project_access upa ON upa.user_id = u.id
            WHERE u.role = %s AND u.is_active = 1
              AND u.deleted_at IS NULL
            """,
            (ROLE_OPERATIONS,),
        )
        existing_access: dict[str, set[tuple[str, str]]] = {}
        for row in cursor.fetchall():
            existing_access.setdefault(
                str(row["display_name"]).casefold(),
                set(),
            ).add(
                (row["project_scope"], row["project_name"])
            )

        result: list[OperationAccountCandidate] = []
        for key in sorted(projects_by_name):
            display_name, scopes = projects_by_name[key]
            access = tuple((scope, display_name) for scope in scopes)
            existing_user = users_by_display.get(key)
            if existing_user is None:
                username = f"YY{next_number:02d}"
                while username.casefold() in used_usernames:
                    next_number += 1
                    username = f"YY{next_number:02d}"
                used_usernames.add(username.casefold())
                next_number += 1
                status = "create"
                reason = ""
            elif (
                existing_user["role"] == ROLE_OPERATIONS
                and bool(existing_user["is_active"])
                and set(access)
                <= existing_access.get(key, set())
            ):
                username = str(existing_user["username"])
                status = "skip"
                reason = "账号和项目权限已存在"
            else:
                username = str(existing_user["username"])
                status = "conflict"
                reason = "显示名称已被其他账号使用或项目权限不完整"
            result.append(
                OperationAccountCandidate(
                    username=username,
                    display_name=display_name,
                    project_access=access,
                    status=status,
                    reason=reason,
                )
            )
        return tuple(result)

    def set_user_active(
        self,
        actor: AuthUser,
        user_id: int,
        active: bool,
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以启停账号")
        if user_id == actor.id and not active:
            raise ValueError("不能停用当前登录账号")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT role, is_active FROM users "
                    "WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                    (user_id,),
                )
                target = cursor.fetchone()
                if target is None:
                    raise ValueError("账号不存在")
                if (
                    target["role"] == ROLE_ADMIN
                    and bool(target["is_active"])
                    and not active
                ):
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS count FROM users
                        WHERE role = %s AND is_active = 1
                          AND deleted_at IS NULL AND id <> %s
                        """,
                        (ROLE_ADMIN, user_id),
                    )
                    if int(cursor.fetchone()["count"]) == 0:
                        raise ValueError("必须保留至少一个启用的管理员账号")
                cursor.execute(
                    "UPDATE users SET is_active = %s, updated_at = %s "
                    "WHERE id = %s AND deleted_at IS NULL",
                    (int(active), utc_now(), user_id),
                )

    def delete_user(self, actor: AuthUser, user_id: int) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以删除账号")
        if user_id == actor.id:
            raise ValueError("不能删除当前登录账号")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, role, is_active, deleted_at
                    FROM users
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (user_id,),
                )
                target = cursor.fetchone()
                if target is None or target["deleted_at"] is not None:
                    raise ValueError("账号不存在")
                if target["role"] == ROLE_ADMIN and bool(target["is_active"]):
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS count FROM users
                        WHERE role = %s AND is_active = 1
                          AND deleted_at IS NULL AND id <> %s
                        """,
                        (ROLE_ADMIN, user_id),
                    )
                    if int(cursor.fetchone()["count"]) == 0:
                        raise ValueError(
                            "必须保留至少一个启用的管理员账号"
                        )
                cursor.execute(
                    """
                    SELECT task_code
                    FROM project_tasks
                    WHERE assignee_id = %s
                      AND status NOT IN (%s, %s, %s, %s)
                    ORDER BY id
                    LIMIT 10
                    """,
                    (
                        user_id,
                        TASK_APPROVED,
                        TASK_SKIPPED,
                        TASK_CANCELLED,
                        TASK_EXPANDED,
                    ),
                )
                claimed = cursor.fetchall()
                if claimed:
                    task_codes = "、".join(
                        row["task_code"] for row in claimed
                    )
                    raise StateConflictError(
                        "账号仍有已领取任务，请先完成、取消或改派："
                        + task_codes
                    )
                cursor.execute(
                    """
                    SELECT t.task_code
                    FROM generation_attempts a
                    JOIN project_tasks t ON t.id = a.task_id
                    WHERE a.operator_id = %s
                      AND a.status IN (%s, %s)
                    ORDER BY a.id
                    LIMIT 10
                    """,
                    (user_id, ATTEMPT_QUEUED, ATTEMPT_RUNNING),
                )
                active_attempts = cursor.fetchall()
                if active_attempts:
                    task_codes = "、".join(
                        row["task_code"] for row in active_attempts
                    )
                    raise StateConflictError(
                        "账号仍有排队中或生成中的任务，"
                        "请先等待完成或取消："
                        + task_codes
                    )
                cursor.execute(
                    """
                    SELECT DISTINCT project_id
                    FROM project_tasks
                    WHERE assignee_id = %s AND status = %s
                    """,
                    (user_id, TASK_AVAILABLE),
                )
                released_project_ids = [
                    int(row["project_id"])
                    for row in cursor.fetchall()
                ]
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET assignee_id = NULL, updated_at = %s
                    WHERE assignee_id = %s AND status = %s
                    """,
                    (now, user_id, TASK_AVAILABLE),
                )
                for project_id in released_project_ids:
                    self._update_project_status(cursor, project_id)
                cursor.execute(
                    "DELETE FROM user_api_keys WHERE user_id = %s",
                    (user_id,),
                )
                cursor.execute(
                    "DELETE FROM user_menu_permissions WHERE user_id = %s",
                    (user_id,),
                )
                cursor.execute(
                    "DELETE FROM user_project_access WHERE user_id = %s",
                    (user_id,),
                )
                cursor.execute(
                    """
                    UPDATE users
                    SET is_active = 0, deleted_at = %s, updated_at = %s
                    WHERE id = %s AND deleted_at IS NULL
                    """,
                    (now, now, user_id),
                )

    def set_user_policies(
        self,
        actor: AuthUser,
        user_id: int,
        *,
        urgent_quota_per_day: int,
        max_generation_concurrency: int,
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以修改账号策略")
        if not 0 <= urgent_quota_per_day <= 99:
            raise ValueError("加急额度必须在 0 到 99 之间")
        if not 1 <= max_generation_concurrency <= 10:
            raise ValueError("生成并发数必须在 1 到 10 之间")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE users
                    SET urgent_quota_per_day = %s,
                        max_generation_concurrency = %s,
                        updated_at = %s
                    WHERE id = %s AND deleted_at IS NULL
                    """,
                    (
                        urgent_quota_per_day,
                        max_generation_concurrency,
                        utc_now(),
                        user_id,
                    ),
                )

    def update_user(
        self,
        actor: AuthUser,
        user_id: int,
        *,
        username: str,
        display_name: str,
        role: str,
        is_active: bool,
        urgent_quota_per_day: int,
        max_generation_concurrency: int,
        menu_permissions: Sequence[str] | None = None,
        project_access: Sequence[tuple[str, str]] | None = None,
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以编辑账号")
        clean_username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", clean_username):
            raise ValueError("账号只能包含字母、数字、点、下划线或短横线，长度 3–64")
        clean_display_name = _validate_display_name(display_name)
        if role not in ROLES:
            raise ValueError("不支持的账号角色")
        if not 0 <= urgent_quota_per_day <= 99:
            raise ValueError("加急额度必须在 0 到 99 之间")
        if not 1 <= max_generation_concurrency <= 10:
            raise ValueError("生成并发数必须在 1 到 10 之间")
        if user_id == actor.id and (not is_active or role != ROLE_ADMIN):
            raise ValueError("不能停用当前账号或取消自己的管理员角色")
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM users "
                        "WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                        (user_id,),
                    )
                    if cursor.fetchone() is None:
                        raise ValueError("账号不存在")
                    cursor.execute(
                        """
                        SELECT id FROM users
                        WHERE username = %s AND id <> %s
                          AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        (clean_username, user_id),
                    )
                    if cursor.fetchone() is not None:
                        raise StateConflictError("登录账号已存在")
                    cursor.execute(
                        """
                        SELECT id FROM users
                        WHERE display_name = %s AND id <> %s
                          AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        (clean_display_name, user_id),
                    )
                    if cursor.fetchone() is not None:
                        raise StateConflictError("显示名称或项目名已被其他账号使用")
                    if role != ROLE_ADMIN or not is_active:
                        cursor.execute(
                            """
                            SELECT COUNT(*) AS count FROM users
                            WHERE role = %s AND is_active = 1
                              AND deleted_at IS NULL AND id <> %s
                            """,
                            (ROLE_ADMIN, user_id),
                        )
                        if int(cursor.fetchone()["count"]) == 0:
                            raise ValueError("必须保留至少一个启用的管理员账号")
                    cursor.execute(
                        """
                        UPDATE users
                        SET username = %s, display_name = %s, role = %s,
                            is_active = %s, urgent_quota_per_day = %s,
                            max_generation_concurrency = %s, updated_at = %s
                        WHERE id = %s AND deleted_at IS NULL
                        """,
                        (
                            clean_username,
                            clean_display_name,
                            role,
                            int(is_active),
                            urgent_quota_per_day,
                            max_generation_concurrency,
                            utc_now(),
                            user_id,
                        ),
                    )
                    if menu_permissions is not None:
                        allowed = ROLE_ALLOWED_MENUS.get(role, ())
                        selected = tuple(
                            key
                            for key in dict.fromkeys(menu_permissions)
                            if key in allowed
                        )
                        cursor.execute(
                            "DELETE FROM user_menu_permissions WHERE user_id = %s",
                            (user_id,),
                        )
                        for menu_key in selected:
                            cursor.execute(
                                """
                                INSERT INTO user_menu_permissions(
                                    user_id, menu_key
                                ) VALUES (%s, %s)
                                """,
                                (user_id, menu_key),
                            )
                    if project_access is not None:
                        selected_projects = tuple(
                            dict.fromkeys(
                                (
                                    str(scope),
                                    str(name).strip(),
                                )
                                for scope, name in project_access
                                if scope in PROJECT_SCOPES and str(name).strip()
                            )
                        )
                        cursor.execute(
                            "DELETE FROM user_project_access WHERE user_id = %s",
                            (user_id,),
                        )
                        for scope, name in selected_projects:
                            cursor.execute(
                                """
                                INSERT INTO user_project_access(
                                    user_id, project_scope, project_name
                                ) VALUES (%s, %s, %s)
                                """,
                                (user_id, scope, name),
                            )
        except pymysql.err.IntegrityError as exc:
            raise StateConflictError("账号或显示名称已存在") from exc

    def save_api_key(
        self,
        actor: AuthUser,
        user_id: int,
        api_key: str,
        *,
        provider_id: int = 1,
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以配置 API Key")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, is_enabled
                    FROM api_providers
                    WHERE id = %s
                    """,
                    (provider_id,),
                )
                provider = cursor.fetchone()
                if provider is None:
                    raise ValueError("API 平台不存在")
                if not bool(provider["is_enabled"]):
                    raise ValueError("API 平台已停用")
                cursor.execute(
                    "SELECT role, is_active FROM users "
                    "WHERE id = %s AND deleted_at IS NULL",
                    (user_id,),
                )
                target = cursor.fetchone()
                if target is None:
                    raise ValueError("账号不存在")
                if target["role"] not in (ROLE_AI, ROLE_ADMIN):
                    raise ValueError("只有 AI 制作或管理员账号需要 API Key")
                if not bool(target["is_active"]):
                    raise ValueError("账号已停用")
                encrypted = encrypt_api_key(api_key)
                suffix = validate_api_key(api_key)[-4:]
                now = utc_now()
                cursor.execute(
                    """
                    INSERT INTO user_api_keys(
                        user_id, provider_id, encrypted_key, key_suffix, updated_by,
                        updated_at, last_test_status, last_test_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'format_ok', %s)
                    ON DUPLICATE KEY UPDATE
                        encrypted_key = VALUES(encrypted_key),
                        key_suffix = VALUES(key_suffix),
                        updated_by = VALUES(updated_by),
                        updated_at = VALUES(updated_at),
                        last_test_status = VALUES(last_test_status),
                        last_test_at = VALUES(last_test_at)
                    """,
                    (
                        user_id,
                        provider_id,
                        encrypted,
                        suffix,
                        actor.id,
                        now,
                        now,
                    ),
                )

    def clear_api_key(
        self,
        actor: AuthUser,
        user_id: int,
        *,
        provider_id: int | None = None,
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以清除 API Key")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                if provider_id is None:
                    cursor.execute(
                        "DELETE FROM user_api_keys WHERE user_id = %s",
                        (user_id,),
                    )
                else:
                    cursor.execute(
                        """
                        DELETE FROM user_api_keys
                        WHERE user_id = %s AND provider_id = %s
                        """,
                        (user_id, provider_id),
                    )

    def api_key_statuses(self) -> dict[int, dict[str, Any]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, provider_id, key_suffix, updated_at,
                           last_test_status, last_test_at
                    FROM user_api_keys
                    ORDER BY updated_at, provider_id
                    """
                )
                result: dict[int, dict[str, Any]] = {}
                for row in cursor.fetchall():
                    result[int(row["user_id"])] = dict(row)
                return result

    def api_key_statuses_by_provider(
        self,
    ) -> dict[tuple[int, int], dict[str, Any]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, provider_id, key_suffix, updated_at,
                           last_test_status, last_test_at
                    FROM user_api_keys
                    """
                )
                return {
                    (int(row["user_id"]), int(row["provider_id"])): dict(row)
                    for row in cursor.fetchall()
                }

    def api_key_status(
        self,
        user_id: int,
        provider_id: int,
    ) -> dict[str, Any] | None:
        return self.api_key_statuses_by_provider().get(
            (user_id, provider_id)
        )

    def load_actor_api_key(
        self,
        actor: AuthUser,
        *,
        provider_id: int | None = None,
    ) -> str:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise ApiKeyError("当前账号不需要配置生图 API Key")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                if provider_id is None:
                    cursor.execute(
                        """
                        SELECT u.is_active, (
                            SELECT k.encrypted_key
                            FROM user_api_keys k
                            JOIN api_providers p ON p.id = k.provider_id
                            WHERE k.user_id = u.id
                              AND p.is_enabled = 1
                            ORDER BY p.priority, p.id
                            LIMIT 1
                        ) AS encrypted_key
                        FROM users u
                        WHERE u.id = %s
                        """,
                        (actor.id,),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT u.is_active, k.encrypted_key
                        FROM users u
                        LEFT JOIN user_api_keys k
                          ON k.user_id = u.id AND k.provider_id = %s
                        WHERE u.id = %s
                        """,
                        (provider_id, actor.id),
                    )
                row = cursor.fetchone()
        if row is None or not bool(row["is_active"]):
            raise ApiKeyError("账号已停用")
        if not row["encrypted_key"]:
            raise ApiKeyError("尚未配置 API Key，请联系管理员")
        return decrypt_api_key(row["encrypted_key"])

    def list_api_providers(
        self,
        *,
        include_disabled: bool = True,
    ) -> list[ApiProvider]:
        where = "" if include_disabled else "WHERE is_enabled = 1"
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT *
                    FROM api_providers
                    {where}
                    ORDER BY priority, id
                    """
                )
                return [
                    _api_provider(row)
                    for row in cursor.fetchall()
                ]

    def list_provider_models(
        self,
        provider_id: int,
        *,
        include_disabled: bool = True,
    ) -> list[dict[str, Any]]:
        condition = "" if include_disabled else "AND m.is_enabled = 1"
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT m.*
                    FROM api_provider_models m
                    WHERE m.provider_id = %s {condition}
                    ORDER BY FIELD(
                        m.logical_model,
                        'gemini-nano-banana-2.1',
                        'gemini-3.1-flash-image',
                        'gemini-3-pro-image'
                    ), m.id
                    """,
                    (provider_id,),
                )
                return [dict(row) for row in cursor.fetchall()]

    def list_provider_routes(
        self,
        logical_model: str,
        *,
        provider_code: str | None = None,
    ) -> list[ProviderModelRoute]:
        if logical_model not in MODEL_IDS:
            raise ValueError("不支持的模型")
        provider_filter = ""
        parameters: list[Any] = [logical_model]
        if provider_code:
            provider_filter = "AND p.provider_code = %s"
            parameters.append(provider_code)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT p.id AS provider_id,
                           p.provider_code,
                           p.display_name AS provider_name,
                           p.base_url,
                           p.default_protocol,
                           m.logical_model,
                           m.provider_model,
                           m.protocol,
                           m.max_reference_images,
                           m.images_per_request,
                           p.priority AS priority
                    FROM api_provider_models m
                    JOIN api_providers p ON p.id = m.provider_id
                    WHERE m.logical_model = %s
                      AND m.is_enabled = 1
                      AND p.is_enabled = 1
                      {provider_filter}
                    ORDER BY p.priority, m.priority, p.id, m.id
                    """,
                    tuple(parameters),
                )
                return [
                    _provider_model_route(row)
                    for row in cursor.fetchall()
                ]

    def update_api_provider(
        self,
        actor: AuthUser,
        provider_id: int,
        *,
        display_name: str,
        base_url: str,
        default_protocol: str,
        enabled: bool,
        priority: int,
        models: Sequence[dict[str, Any]],
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以修改 API 平台")
        clean_name = display_name.strip()
        clean_url = base_url.strip().rstrip("/")
        if not clean_name:
            raise ValueError("平台名称不能为空")
        if not clean_url.startswith(("https://", "http://")):
            raise ValueError("API 地址必须以 http:// 或 https:// 开头")
        if default_protocol not in PROVIDER_PROTOCOLS:
            raise ValueError("不支持的 API 协议")
        if priority < 0:
            raise ValueError("平台优先级不能为负数")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM api_providers WHERE id = %s",
                    (provider_id,),
                )
                if cursor.fetchone() is None:
                    raise ValueError("API 平台不存在")
                cursor.execute(
                    """
                    UPDATE api_providers
                    SET display_name = %s, base_url = %s,
                        default_protocol = %s, is_enabled = %s,
                        priority = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        clean_name,
                        clean_url,
                        default_protocol,
                        int(enabled),
                        int(priority),
                        now,
                        provider_id,
                    ),
                )
                for model in models:
                    logical_model = str(model.get("logical_model") or "")
                    provider_model = str(
                        model.get("provider_model") or ""
                    ).strip()
                    protocol = str(model.get("protocol") or "")
                    if logical_model not in MODEL_IDS:
                        raise ValueError("模型映射中包含不支持的模型")
                    if not provider_model:
                        raise ValueError("平台模型名不能为空")
                    if protocol not in PROVIDER_PROTOCOLS:
                        raise ValueError("模型映射中包含不支持的协议")
                    max_references = int(
                        model.get("max_reference_images") or 0
                    )
                    images_per_request = int(
                        model.get("images_per_request") or 0
                    )
                    if not 0 <= max_references <= 14:
                        raise ValueError("最大参考图数必须在 0 到 14 之间")
                    if not 1 <= images_per_request <= 4:
                        raise ValueError("单次出图数必须在 1 到 4 之间")
                    cursor.execute(
                        """
                        INSERT INTO api_provider_models(
                            provider_id, logical_model, provider_model,
                            protocol, max_reference_images,
                            images_per_request, is_enabled, priority,
                            created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON DUPLICATE KEY UPDATE
                            provider_model = VALUES(provider_model),
                            protocol = VALUES(protocol),
                            max_reference_images =
                                VALUES(max_reference_images),
                            images_per_request =
                                VALUES(images_per_request),
                            is_enabled = VALUES(is_enabled),
                            priority = VALUES(priority),
                            updated_at = VALUES(updated_at)
                        """,
                        (
                            provider_id,
                            logical_model,
                            provider_model,
                            protocol,
                            max_references,
                            images_per_request,
                            int(bool(model.get("enabled", True))),
                            int(model.get("priority") or priority),
                            now,
                            now,
                        ),
                    )

    def record_provider_call(
        self,
        attempt_id: int,
        actor: AuthUser,
        *,
        provider_id: int | None,
        provider_name: str,
        provider_model: str,
        status: str,
        request_id: str = "",
        remote_task_id: str = "",
        status_code: int | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                attempt = self._attempt_row_for_update(cursor, attempt_id)
                if (
                    int(attempt["operator_id"]) != actor.id
                    and actor.role != ROLE_ADMIN
                ):
                    raise PermissionDeniedError("不能修改其他人的生成记录")
                if status != "skipped":
                    cursor.execute(
                        """
                        UPDATE generation_attempts
                        SET provider_id = %s, provider_code = (
                                SELECT provider_code FROM api_providers
                                WHERE id = %s
                            ),
                            provider_name = %s, provider_model = %s
                        WHERE id = %s
                        """,
                        (
                            provider_id,
                            provider_id,
                            provider_name[:100],
                            provider_model[:160],
                            attempt_id,
                        ),
                    )
                cursor.execute(
                    """
                    INSERT INTO generation_provider_calls(
                        attempt_id, provider_id, provider_name,
                        provider_model, status, request_id,
                        remote_task_id, status_code, error_code,
                        error_message, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        attempt_id,
                        provider_id,
                        provider_name[:100],
                        provider_model[:160],
                        status,
                        request_id[:255],
                        remote_task_id[:255],
                        status_code,
                        error_code[:100],
                        error_message,
                        utc_now(),
                    ),
                )

    def reset_password(
        self,
        actor: AuthUser,
        user_id: int,
        temporary_password: str,
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以重置密码")
        validate_new_password(temporary_password)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE users
                    SET password_hash = %s, must_change_password = 1, updated_at = %s
                    WHERE id = %s AND deleted_at IS NULL
                    """,
                    (hash_password(temporary_password), utc_now(), user_id),
                )

    def create_project(self, actor: AuthUser, draft: ProjectDraft) -> int:
        if actor.role not in (ROLE_OPERATIONS, ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能提交项目")
        self._validate_draft(draft)
        now = utc_now()
        source_date = draft.source_date or date.today()
        product_code = (
            draft.product_code.strip()
            or draft.name.strip()
            or Path(draft.project_relative_root).name
        )
        access_project_name = (
            draft.access_project_name.strip() or product_code
        )
        if draft.project_scope not in PROJECT_SCOPES:
            raise ValueError("项目类型不正确")
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    if actor.role == ROLE_OPERATIONS:
                        self._assert_project_access(
                            cursor,
                            actor.id,
                            draft.project_scope,
                            access_project_name,
                        )
                    task_code = draft.project_code.strip()
                    if not task_code:
                        while True:
                            sequence = self._next_task_sequence(
                                cursor,
                                actor.id,
                                source_date,
                            )
                            task_code = (
                                f"{actor.display_name}-{product_code}-{sequence:03d}"
                            )
                            cursor.execute(
                                "SELECT 1 FROM projects WHERE project_code = %s",
                                (task_code,),
                            )
                            if cursor.fetchone() is None:
                                break
                    deadline = local_to_utc_naive(
                        datetime.combine(source_date, time.max)
                    )
                    if draft.is_urgent:
                        urgent_day = date.today()
                        cursor.execute(
                            "SELECT urgent_quota_per_day FROM users "
                            "WHERE id = %s FOR UPDATE",
                            (actor.id,),
                        )
                        quota = int(cursor.fetchone()["urgent_quota_per_day"])
                        # 用锁定读取统计，避免 REPEATABLE READ 下读到旧快照导致超额。
                        cursor.execute(
                            """
                            SELECT id FROM projects
                            WHERE owner_id = %s AND urgent_date = %s
                              AND is_urgent = 1
                            FOR UPDATE
                            """,
                            (actor.id, urgent_day),
                        )
                        if len(cursor.fetchall()) >= quota:
                            raise StateConflictError(
                                f"今日加急额度已用完（上限 {quota} 个）"
                            )
                    cursor.execute(
                        """
                        INSERT INTO projects(
                            project_code, name, deadline, priority, brand_platform,
                            category, note, status, owner_id, project_relative_root,
                            pairing_mode, source_date, product_code,
                            project_scope, project_name, is_urgent,
                            urgent_date, urgent_by,
                            source_revision, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, 'normal', '', '', %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, 1, %s, %s
                        )
                        """,
                        (
                            task_code,
                            product_code,
                            deadline,
                            draft.note.strip(),
                            PROJECT_SUBMITTED,
                            actor.id,
                            draft.project_relative_root,
                            draft.pairing_mode,
                            source_date,
                            product_code,
                            draft.project_scope,
                            access_project_name,
                            int(draft.is_urgent),
                            urgent_day if draft.is_urgent else None,
                            actor.id if draft.is_urgent else None,
                            now,
                            now,
                        ),
                    )
                    project_id = int(cursor.lastrowid)
                    self._insert_assets_and_tasks(
                        cursor,
                        project_id,
                        draft,
                        actor.id,
                        task_code,
                        now,
                    )
                    self._event(
                        cursor,
                        project_id,
                        None,
                        actor.id,
                        "project_submitted",
                        "",
                        PROJECT_SUBMITTED,
                        {"task_code": task_code, "task_count": len(draft.pairs)},
                    )
                    return project_id
        except pymysql.err.IntegrityError as exc:
            if "project_code" in str(exc):
                raise StateConflictError("任务编码已存在") from exc
            raise
        except pymysql.MySQLError as exc:
            raise CentralRepositoryError(_mysql_error_message(exc)) from exc

    def update_project(
        self,
        project_id: int,
        actor: AuthUser,
        draft: ProjectDraft,
    ) -> None:
        self._validate_draft(draft)
        if draft.project_scope not in PROJECT_SCOPES:
            raise ValueError("项目类型不正确")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                project = self._project_row_for_update(cursor, project_id)
                if actor.role != ROLE_ADMIN and int(project["owner_id"]) != actor.id:
                    raise PermissionDeniedError("只能修改自己提交的项目")
                if actor.role == ROLE_OPERATIONS:
                    self._assert_project_access(
                        cursor,
                        actor.id,
                        draft.project_scope,
                        draft.access_project_name.strip()
                        or draft.name.strip(),
                    )
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count FROM project_tasks
                    WHERE project_id = %s AND status <> %s
                    """,
                    (project_id, TASK_AVAILABLE),
                )
                if int(cursor.fetchone()["count"]):
                    raise StateConflictError("任务已被领取，项目不能再修改")
                if project["status"] in (PROJECT_CANCELLED, PROJECT_COMPLETED):
                    raise StateConflictError("已结束项目不能修改")
                cursor.execute("DELETE FROM project_tasks WHERE project_id = %s", (project_id,))
                cursor.execute(
                    "DELETE FROM project_assets WHERE project_id = %s",
                    (project_id,),
                )
                cursor.execute(
                    """
                    UPDATE projects
                    SET project_code = %s, name = %s, deadline = %s, priority = %s,
                        brand_platform = %s, category = %s, note = %s,
                        project_relative_root = %s, pairing_mode = %s,
                        source_date = %s, product_code = %s,
                        project_scope = %s, project_name = %s,
                        source_revision = source_revision + 1,
                        status = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        draft.project_code.strip(),
                        draft.name.strip(),
                        local_to_utc_naive(
                            datetime.combine(
                                draft.source_date or date.today(),
                                time.max,
                            )
                        ),
                        "normal",
                        "",
                        "",
                        draft.note.strip(),
                        draft.project_relative_root,
                        draft.pairing_mode,
                        draft.source_date or date.today(),
                        draft.product_code or draft.name,
                        draft.project_scope,
                        draft.access_project_name.strip() or draft.name,
                        PROJECT_SUBMITTED,
                        now,
                        project_id,
                    ),
                )
                self._insert_assets_and_tasks(
                    cursor,
                    project_id,
                    draft,
                    int(project["owner_id"]),
                    project["project_code"],
                    now,
                )
                self._event(
                    cursor,
                    project_id,
                    None,
                    actor.id,
                    "project_updated",
                    project["status"],
                    PROJECT_SUBMITTED,
                    {"task_count": len(draft.pairs)},
                )

    def list_projects(
        self,
        limit: int = 5000,
        actor: AuthUser | None = None,
    ) -> list[ProjectRecord]:
        conditions = ["1 = 1"]
        params: list[Any] = [
            TASK_APPROVED,
            TASK_AWAITING_ACCEPTANCE,
        ]
        if actor is not None and actor.role == ROLE_OPERATIONS:
            conditions.extend(
                (
                    "p.owner_id = %s",
                    """
                    EXISTS (
                        SELECT 1 FROM user_project_access upa
                        WHERE upa.user_id = %s
                          AND upa.project_scope = p.project_scope
                          AND upa.project_name = p.project_name
                    )
                    """,
                )
            )
            params.extend((actor.id, actor.id))
        params.append(limit)
        query = """
            SELECT p.*, owner.display_name AS owner_name,
                   COUNT(t.id) AS task_count,
                   COALESCE(SUM(t.status = %s), 0) AS approved_count,
                   COALESCE(SUM(t.status = %s), 0) AS awaiting_count,
                   COALESCE(p.is_urgent, 0) AS urgent_count
            FROM projects p
            JOIN users owner ON owner.id = p.owner_id
            LEFT JOIN project_tasks t
              ON t.project_id = p.id AND t.status <> 'expanded'
            WHERE
            """ + " AND ".join(conditions) + """
            GROUP BY p.id, owner.display_name
            ORDER BY
                p.is_urgent DESC,
                p.source_date DESC, p.id DESC
            LIMIT %s
        """
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    tuple(params),
                )
                return [_project_record(row) for row in cursor.fetchall()]

    @staticmethod
    def _exception_task_sql() -> str:
        return """
            (
                t.status = %s
                OR (t.status = %s AND t.created_at < %s)
                OR (
                    t.status = %s
                    AND COALESCE(
                        t.updated_at,
                        t.work_started_at,
                        t.created_at
                    ) < %s
                )
                OR EXISTS (
                    SELECT 1
                    FROM generation_attempts failed_attempt
                    WHERE failed_attempt.task_id = t.id
                      AND failed_attempt.status = %s
                      AND failed_attempt.created_at >= %s
                      AND failed_attempt.attempt_no = (
                          SELECT MAX(latest_attempt.attempt_no)
                          FROM generation_attempts latest_attempt
                          WHERE latest_attempt.task_id = t.id
                      )
                )
            )
            """

    def list_exception_projects(
        self,
        cutoff: datetime,
        *,
        limit: int = 5000,
    ) -> list[ProjectRecord]:
        exception_sql = self._exception_task_sql()
        query = f"""
            SELECT p.*, owner.display_name AS owner_name,
                   COUNT(t.id) AS task_count,
                   COALESCE(SUM(t.status = %s), 0) AS approved_count,
                   COALESCE(SUM(t.status = %s), 0) AS awaiting_count,
                   COALESCE(p.is_urgent, 0) AS urgent_count
            FROM projects p
            JOIN users owner ON owner.id = p.owner_id
            LEFT JOIN project_tasks t
              ON t.project_id = p.id AND t.status <> %s
            WHERE p.status = %s OR EXISTS (
                SELECT 1 FROM project_tasks t
                WHERE t.project_id = p.id
                  AND {exception_sql}
            )
            GROUP BY p.id, owner.display_name
            ORDER BY p.updated_at DESC, p.id DESC
            LIMIT %s
        """
        params = (
            TASK_APPROVED,
            TASK_AWAITING_ACCEPTANCE,
            TASK_EXPANDED,
            PROJECT_CANCELLED,
            TASK_RETURNED,
            TASK_AVAILABLE,
            cutoff,
            TASK_WORKING,
            cutoff,
            ATTEMPT_FAILED,
            cutoff,
            limit,
        )
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return [
                    _project_record(row)
                    for row in cursor.fetchall()
                ]

    def list_exception_tasks(
        self,
        project_id: int,
        cutoff: datetime,
    ) -> list[ProjectTask]:
        exception_sql = self._exception_task_sql()
        query = (
            self._task_select_sql()
            + " WHERE t.project_id = %s AND "
            + exception_sql
            + " ORDER BY p.is_urgent DESC, t.task_no"
        )
        params = (
            project_id,
            TASK_RETURNED,
            TASK_AVAILABLE,
            cutoff,
            TASK_WORKING,
            cutoff,
            ATTEMPT_FAILED,
            cutoff,
        )
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return [_project_task(row) for row in cursor.fetchall()]

    def list_board_projects_page(
        self,
        actor: AuthUser,
        statuses: Sequence[str],
        query: TaskQuery,
        page: PageRequest,
    ) -> PageResult[ProjectRecord]:
        clean_statuses = tuple(dict.fromkeys(str(value) for value in statuses))
        if not clean_statuses:
            return PageResult([], 0, page.page, page.page_size)
        status_placeholders = ", ".join(["%s"] * len(clean_statuses))
        task_conditions = [
            "t.project_id = p.id",
            f"t.status IN ({status_placeholders})",
        ]
        task_params: list[Any] = list(clean_statuses)
        if query.start_date is not None:
            task_conditions.append("t.created_at >= %s")
            task_params.append(
                local_to_utc_naive(
                    datetime.combine(query.start_date, time.min)
                )
            )
        if query.end_date is not None:
            task_conditions.append("t.created_at <= %s")
            task_params.append(
                local_to_utc_naive(
                    datetime.combine(query.end_date, time.max)
                )
            )
        keyword = query.keyword.strip()
        if keyword:
            pattern = f"%{keyword}%"
            task_conditions.append(
                "("
                "t.task_code LIKE %s OR "
                "p.project_code LIKE %s OR p.product_code LIKE %s OR "
                "p.name LIKE %s OR p.note LIKE %s OR "
                "owner.display_name LIKE %s OR "
                "COALESCE(assignee.display_name, '') LIKE %s"
                ")"
            )
            task_params.extend((pattern,) * 7)
        conditions = [
            "EXISTS ("
            "SELECT 1 FROM project_tasks t "
            "LEFT JOIN users assignee ON assignee.id = t.assignee_id "
            "WHERE "
            + " AND ".join(task_conditions)
            + ")"
        ]
        params = list(task_params)
        if actor.role == ROLE_OPERATIONS:
            conditions.extend(
                (
                    "p.owner_id = %s",
                    """
                    EXISTS (
                        SELECT 1 FROM user_project_access upa
                        WHERE upa.user_id = %s
                          AND upa.project_scope = p.project_scope
                          AND upa.project_name = p.project_name
                    )
                    """,
                )
            )
            params.extend((actor.id, actor.id))
        where_sql = " AND ".join(conditions)
        sort_fields = {
            "project_code": "p.project_code",
            "product_code": "p.product_code",
            "owner_name": "owner.display_name",
            "source_date": "p.source_date",
            "status": "p.status",
            "created_at": "p.created_at",
            "is_urgent": "p.is_urgent",
            "progress": "approved_count / GREATEST(task_count, 1)",
        }
        sort_field = sort_fields.get(page.sort_field, "p.created_at")
        direction = "ASC" if page.sort_direction == "asc" else "DESC"
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM projects p
                    JOIN users owner ON owner.id = p.owner_id
                    WHERE {where_sql}
                    """,
                    tuple(params),
                )
                total = int(cursor.fetchone()["count"])
                cursor.execute(
                    f"""
                    SELECT p.*, owner.display_name AS owner_name,
                           COUNT(t.id) AS task_count,
                           COALESCE(SUM(t.status = %s), 0)
                               AS approved_count,
                           COALESCE(SUM(t.status = %s), 0)
                               AS awaiting_count,
                           COALESCE(p.is_urgent, 0) AS urgent_count
                    FROM projects p
                    JOIN users owner ON owner.id = p.owner_id
                    LEFT JOIN project_tasks t
                      ON t.project_id = p.id AND t.status <> %s
                    WHERE {where_sql}
                    GROUP BY p.id, owner.display_name
                    ORDER BY {sort_field} {direction}, p.id {direction}
                    LIMIT %s OFFSET %s
                    """,
                    (
                        TASK_APPROVED,
                        TASK_AWAITING_ACCEPTANCE,
                        TASK_EXPANDED,
                        *params,
                        page.page_size,
                        page.offset,
                    ),
                )
                rows = cursor.fetchall()
        return PageResult(
            [_project_record(row) for row in rows],
            total,
            page.page,
            page.page_size,
        )

    def list_access_project_names(self, actor: AuthUser) -> list[str]:
        conditions = ["p.project_name <> ''"]
        params: list[Any] = []
        if actor.role == ROLE_OPERATIONS:
            conditions.extend(
                (
                    "p.owner_id = %s",
                    """
                    EXISTS (
                        SELECT 1 FROM user_project_access upa
                        WHERE upa.user_id = %s
                          AND upa.project_scope = p.project_scope
                          AND upa.project_name = p.project_name
                    )
                    """,
                )
            )
            params.extend((actor.id, actor.id))
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT p.project_name
                    FROM projects p
                    WHERE
                    """
                    + " AND ".join(conditions)
                    + " ORDER BY p.project_name",
                    tuple(params),
                )
                return [
                    str(row["project_name"])
                    for row in cursor.fetchall()
                ]

    def list_board_tasks_for_project_page(
        self,
        project_id: int,
        actor: AuthUser,
        statuses: Sequence[str],
        query: TaskQuery,
        page: PageRequest,
    ) -> PageResult[ProjectTask]:
        clean_statuses = tuple(dict.fromkeys(str(value) for value in statuses))
        if not clean_statuses:
            return PageResult([], 0, page.page, page.page_size)
        placeholders = ", ".join(["%s"] * len(clean_statuses))
        conditions = [
            "t.project_id = %s",
            f"t.status IN ({placeholders})",
        ]
        params: list[Any] = [project_id, *clean_statuses]
        if query.start_date is not None:
            conditions.append("t.created_at >= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(query.start_date, time.min)
                )
            )
        if query.end_date is not None:
            conditions.append("t.created_at <= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(query.end_date, time.max)
                )
            )
        keyword = query.keyword.strip()
        if keyword:
            pattern = f"%{keyword}%"
            conditions.append(
                "("
                "t.task_code LIKE %s OR p.project_code LIKE %s OR "
                "p.product_code LIKE %s OR p.name LIKE %s OR "
                "p.note LIKE %s OR "
                "COALESCE(assignee.display_name, '') LIKE %s"
                ")"
            )
            params.extend((pattern,) * 6)
        if actor.role == ROLE_OPERATIONS:
            conditions.extend(
                (
                    "p.owner_id = %s",
                    """
                    EXISTS (
                        SELECT 1 FROM user_project_access upa
                        WHERE upa.user_id = %s
                          AND upa.project_scope = p.project_scope
                          AND upa.project_name = p.project_name
                    )
                    """,
                )
            )
            params.extend((actor.id, actor.id))
        where_sql = " AND ".join(conditions)
        sort_fields = {
            "task_code": "t.task_code",
            "status": "t.status",
            "assignee_name": "assignee.display_name",
            "created_at": "t.created_at",
            "updated_at": "t.updated_at",
            "garment_count": (
                "JSON_LENGTH(COALESCE(garment_sets.garments_json, JSON_ARRAY()))"
            ),
            "is_urgent": "p.is_urgent",
        }
        sort_field = sort_fields.get(page.sort_field, "t.created_at")
        direction = "ASC" if page.sort_direction == "asc" else "DESC"
        count_from = """
            FROM project_tasks t
            JOIN projects p ON p.id = t.project_id
            LEFT JOIN users assignee ON assignee.id = t.assignee_id
        """
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) AS count {count_from} "
                    f"WHERE {where_sql}",
                    tuple(params),
                )
                total = int(cursor.fetchone()["count"])
                cursor.execute(
                    self._task_select_sql()
                    + f" WHERE {where_sql}"
                    + f" ORDER BY {sort_field} {direction}, t.id {direction}"
                    + " LIMIT %s OFFSET %s",
                    (*params, page.page_size, page.offset),
                )
                rows = cursor.fetchall()
        return PageResult(
            [_project_task(row) for row in rows],
            total,
            page.page,
            page.page_size,
        )

    def get_project(self, project_id: int) -> ProjectRecord | None:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                row = self._project_row(cursor, project_id)
                return _project_record(row) if row else None

    def find_tasks_by_product(
        self,
        owner_id: int,
        source_date: date,
        product_code: str,
    ) -> list[ProjectRecord]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT p.*, owner.display_name AS owner_name,
                           COUNT(t.id) AS task_count,
                           COALESCE(SUM(t.status = %s), 0) AS approved_count,
                           COALESCE(SUM(t.status = %s), 0) AS awaiting_count,
                           COALESCE(p.is_urgent, 0) AS urgent_count
                    FROM projects p
                    JOIN users owner ON owner.id = p.owner_id
                    LEFT JOIN project_tasks t
                      ON t.project_id = p.id AND t.status <> 'expanded'
                    WHERE p.owner_id = %s AND p.source_date = %s
                      AND (
                        p.product_code = %s
                        OR p.product_code LIKE %s
                      )
                      AND p.status <> %s
                    GROUP BY p.id, owner.display_name
                    ORDER BY p.id DESC
                    """,
                    (
                        TASK_APPROVED,
                        TASK_AWAITING_ACCEPTANCE,
                        owner_id,
                        source_date,
                        product_code,
                        f"{product_code}_",
                        PROJECT_CANCELLED,
                    ),
                )
                return [_project_record(row) for row in cursor.fetchall()]

    def list_project_tasks(self, project_id: int) -> list[ProjectTask]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    self._task_select_sql()
                    + " WHERE t.project_id = %s "
                    + "ORDER BY p.is_urgent DESC, t.task_no",
                    (project_id,),
                )
                return [_project_task(row) for row in cursor.fetchall()]

    def list_board_tasks(
        self,
        actor: AuthUser,
        *,
        include_finished: bool = False,
        limit: int = 1000,
    ) -> list[ProjectTask]:
        conditions = ["1 = 1"]
        params: list[Any] = []
        if not include_finished:
            conditions.append("t.status NOT IN (%s, %s)")
            params.extend((TASK_APPROVED, TASK_CANCELLED))
        if actor.role == ROLE_OPERATIONS:
            conditions.extend(
                (
                    "p.owner_id = %s",
                    """
                    EXISTS (
                        SELECT 1 FROM user_project_access upa
                        WHERE upa.user_id = %s
                          AND upa.project_scope = p.project_scope
                          AND upa.project_name = p.project_name
                    )
                    """,
                )
            )
            params.extend((actor.id, actor.id))
        query = (
            self._task_select_sql()
            + " WHERE "
            + " AND ".join(conditions)
            + """
              ORDER BY
                p.is_urgent DESC, p.source_date DESC, p.id, t.task_no
              LIMIT %s
            """
        )
        params.append(limit)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, tuple(params))
                return [_project_task(row) for row in cursor.fetchall()]

    def get_task(self, task_id: int) -> ProjectTask | None:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    self._task_select_sql() + " WHERE t.id = %s",
                    (task_id,),
                )
                row = cursor.fetchone()
                return _project_task(row) if row else None

    def list_assignment_projects(
        self,
        actor: AuthUser,
        *,
        project_name: str | None = None,
        limit: int = 5000,
    ) -> list[ProjectRecord]:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能查看待分配任务")
        project_condition = ""
        params: list[Any] = [
            TASK_APPROVED,
            TASK_AWAITING_ACCEPTANCE,
            TASK_AVAILABLE,
            PROJECT_COMPLETED,
            PROJECT_CANCELLED,
            TASK_EXPANDED,
        ]
        if project_name:
            project_condition = " AND p.project_name = %s"
            params.append(project_name)
        params.append(limit)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT p.*, owner.display_name AS owner_name,
                           COUNT(t.id) AS task_count,
                           COALESCE(SUM(t.status = %s), 0)
                               AS approved_count,
                           COALESCE(SUM(t.status = %s), 0)
                               AS awaiting_count,
                           COALESCE(p.is_urgent, 0) AS urgent_count,
                           COALESCE(SUM(
                               t.status = %s
                               AND t.assignee_id IS NULL
                           ), 0) AS available_count
                    FROM projects p
                    JOIN users owner ON owner.id = p.owner_id
                    JOIN project_tasks t ON t.project_id = p.id
                    WHERE p.status NOT IN (%s, %s)
                      AND t.status <> %s
                      {project_condition}
                    GROUP BY p.id, owner.display_name
                    HAVING task_count > 0
                       AND available_count = task_count
                    ORDER BY p.is_urgent DESC, p.source_date, p.id
                    LIMIT %s
                    """,
                    tuple(params),
                )
                return [
                    _project_record(row)
                    for row in cursor.fetchall()
                ]

    def list_assignment_project_counts(
        self,
        actor: AuthUser,
    ) -> list[tuple[str, int]]:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能查看待分配任务")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT matched.project_name,
                           COUNT(*) AS project_count
                    FROM (
                        SELECT p.id, p.project_name
                        FROM projects p
                        JOIN project_tasks t ON t.project_id = p.id
                        WHERE p.status NOT IN (%s, %s)
                          AND p.project_name <> ''
                          AND t.status <> %s
                        GROUP BY p.id, p.project_name
                        HAVING COUNT(*) > 0
                           AND COUNT(*) = SUM(
                               t.status = %s
                               AND t.assignee_id IS NULL
                           )
                    ) matched
                    GROUP BY matched.project_name
                    ORDER BY matched.project_name
                    """,
                    (
                        PROJECT_COMPLETED,
                        PROJECT_CANCELLED,
                        TASK_EXPANDED,
                        TASK_AVAILABLE,
                    ),
                )
                return [
                    (str(row["project_name"]), int(row["project_count"]))
                    for row in cursor.fetchall()
                ]

    def list_assignment_projects_page(
        self,
        actor: AuthUser,
        query: TaskQuery,
        page: PageRequest,
    ) -> PageResult[ProjectRecord]:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能查看待分配任务")
        conditions = [
            "p.status NOT IN (%s, %s)",
            "t.status <> %s",
        ]
        params: list[Any] = [
            PROJECT_COMPLETED,
            PROJECT_CANCELLED,
            TASK_EXPANDED,
        ]
        if query.start_date is not None:
            conditions.append("p.created_at >= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(query.start_date, time.min)
                )
            )
        if query.end_date is not None:
            conditions.append("p.created_at <= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(query.end_date, time.max)
                )
            )
        if query.project_name:
            conditions.append("p.project_name = %s")
            params.append(query.project_name)
        keyword = query.keyword.strip()
        if keyword:
            pattern = f"%{keyword}%"
            conditions.append(
                "("
                "p.project_code LIKE %s OR p.product_code LIKE %s OR "
                "p.name LIKE %s OR p.project_name LIKE %s OR "
                "p.note LIKE %s"
                ")"
            )
            params.extend((pattern,) * 5)
        where_sql = " AND ".join(conditions)
        base = f"""
            SELECT p.*, owner.display_name AS owner_name,
                   COUNT(t.id) AS task_count,
                   COALESCE(SUM(t.status = %s), 0)
                       AS approved_count,
                   COALESCE(SUM(t.status = %s), 0)
                       AS awaiting_count,
                   COALESCE(p.is_urgent, 0) AS urgent_count,
                   COALESCE(SUM(
                       t.status = %s AND t.assignee_id IS NULL
                   ), 0) AS available_count
            FROM projects p
            JOIN users owner ON owner.id = p.owner_id
            JOIN project_tasks t ON t.project_id = p.id
            WHERE {where_sql}
            GROUP BY p.id, owner.display_name
            HAVING task_count > 0
               AND available_count = task_count
        """
        base_params = (
            TASK_APPROVED,
            TASK_AWAITING_ACCEPTANCE,
            TASK_AVAILABLE,
            *params,
        )
        sort_fields = {
            "project_code": "p.project_code",
            "product_code": "p.product_code",
            "project_name": "p.project_name",
            "task_count": "task_count",
            "is_urgent": "p.is_urgent",
            "created_at": "p.created_at",
        }
        sort_field = sort_fields.get(
            page.sort_field,
            "p.source_date",
        )
        direction = "ASC" if page.sort_direction == "asc" else "DESC"
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) AS count FROM ({base}) matched",
                    base_params,
                )
                total = int(cursor.fetchone()["count"])
                cursor.execute(
                    base
                    + f"""
                    ORDER BY p.is_urgent DESC, {sort_field} {direction},
                             p.id {direction}
                    LIMIT %s OFFSET %s
                    """,
                    (*base_params, page.page_size, page.offset),
                )
                rows = cursor.fetchall()
        return PageResult(
            [_project_record(row) for row in rows],
            total,
            page.page,
            page.page_size,
        )

    def claim_projects(
        self,
        project_ids: Sequence[int],
        actor: AuthUser,
    ) -> tuple[int, ...]:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("只有 AI 制作人员或管理员可以领取任务")
        clean_ids = tuple(
            sorted(
                dict.fromkeys(
                    int(project_id)
                    for project_id in project_ids
                )
            )
        )
        if not clean_ids:
            raise ValueError("请选择至少一个主任务")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                for project_id in clean_ids:
                    cursor.execute(
                        "SELECT * FROM projects WHERE id = %s FOR UPDATE",
                        (project_id,),
                    )
                    project = cursor.fetchone()
                    if project is None:
                        raise StateConflictError("主任务不存在")
                    if project["status"] in (
                        PROJECT_COMPLETED,
                        PROJECT_CANCELLED,
                    ):
                        raise StateConflictError(
                            f"{project['project_code']} 已结束，不能领取"
                        )
                    cursor.execute(
                        """
                        SELECT id, task_code, status, assignee_id
                        FROM project_tasks
                        WHERE project_id = %s AND status <> %s
                        FOR UPDATE
                        """,
                        (project_id, TASK_EXPANDED),
                    )
                    tasks = cursor.fetchall()
                    if not tasks:
                        raise StateConflictError(
                            f"{project['project_code']} 没有可领取子任务"
                        )
                    conflicts = [
                        task for task in tasks
                        if task["status"] != TASK_AVAILABLE
                        or task["assignee_id"] is not None
                    ]
                    if conflicts:
                        raise StateConflictError(
                            f"{project['project_code']} 已有子任务被领取，"
                            "本次整组领取已取消"
                        )
                for project_id in clean_ids:
                    cursor.execute(
                        """
                        UPDATE project_tasks
                        SET assignee_id = %s, status = %s,
                            claimed_at = %s, updated_at = %s
                        WHERE project_id = %s AND status = %s
                        """,
                        (
                            actor.id,
                            TASK_CLAIMED,
                            now,
                            now,
                            project_id,
                            TASK_AVAILABLE,
                        ),
                    )
                    self._update_project_status(cursor, project_id)
                    self._event(
                        cursor,
                        project_id,
                        None,
                        actor.id,
                        "project_claimed",
                        PROJECT_SUBMITTED,
                        PROJECT_IN_PROGRESS,
                        {"claimed_by": actor.id},
                    )
        return clean_ids

    def mark_task_skipped(
        self,
        task_id: int,
        actor: AuthUser,
    ) -> None:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能跳过制作任务")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                if int(task["assignee_id"] or 0) != actor.id:
                    raise PermissionDeniedError("该子任务不属于当前账号")
                if task["status"] in (
                    TASK_APPROVED,
                    TASK_SKIPPED,
                    TASK_CANCELLED,
                    TASK_EXPANDED,
                ):
                    return
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s, final_quantity = 0,
                        approved_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (TASK_SKIPPED, now, now, task_id),
                )
                self._update_project_status(
                    cursor,
                    int(task["project_id"]),
                )
                self._event(
                    cursor,
                    int(task["project_id"]),
                    task_id,
                    actor.id,
                    "task_skipped",
                    task["status"],
                    TASK_SKIPPED,
                    {},
                )

    def cancel_queued_generation_attempt(
        self,
        attempt_id: int,
        actor: AuthUser,
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                attempt = self._attempt_row_for_update(cursor, attempt_id)
                if (
                    int(attempt["operator_id"]) != actor.id
                    and actor.role != ROLE_ADMIN
                ):
                    raise PermissionDeniedError(
                        "不能取消其他人的生成轮次"
                    )
                if attempt["status"] != ATTEMPT_QUEUED:
                    raise StateConflictError("只有排队中的轮次可以取消")
                task = self._task_row_for_update(
                    cursor,
                    int(attempt["task_id"]),
                )
                cursor.execute(
                    """
                    UPDATE generation_attempts
                    SET status = %s, error_code = 'cancelled',
                        error_message = '用户取消排队', finished_at = %s
                    WHERE id = %s
                    """,
                    (ATTEMPT_CANCELLED, now, attempt_id),
                )
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count FROM generation_attempts
                    WHERE task_id = %s AND id <> %s
                      AND status IN (%s, %s)
                    """,
                    (
                        int(attempt["task_id"]),
                        attempt_id,
                        ATTEMPT_QUEUED,
                        ATTEMPT_RUNNING,
                    ),
                )
                if int(cursor.fetchone()["count"]) == 0:
                    cursor.execute(
                        """
                        UPDATE project_tasks
                        SET status = %s, updated_at = %s
                        WHERE id = %s AND status = %s
                        """,
                        (
                            TASK_CLAIMED,
                            now,
                            int(attempt["task_id"]),
                            TASK_WORKING,
                        ),
                    )
                self._update_project_status(
                    cursor,
                    int(attempt["project_id"]),
                )
                self._event(
                    cursor,
                    int(attempt["project_id"]),
                    int(attempt["task_id"]),
                    actor.id,
                    "generation_cancelled",
                    task["status"],
                    task["status"],
                    {"attempt_id": attempt_id},
                )

    def project_progress_counts(
        self,
        project_ids: Sequence[int],
    ) -> dict[int, tuple[int, int]]:
        clean_ids = tuple(
            dict.fromkeys(int(value) for value in project_ids)
        )
        if not clean_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(clean_ids))
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT project_id,
                           COUNT(*) AS total_count,
                           COALESCE(SUM(
                               status IN (%s, %s)
                           ), 0) AS completed_count
                    FROM project_tasks
                    WHERE project_id IN ({placeholders})
                      AND status <> %s
                    GROUP BY project_id
                    """,
                    (
                        TASK_APPROVED,
                        TASK_SKIPPED,
                        *clean_ids,
                        TASK_EXPANDED,
                    ),
                )
                return {
                    int(row["project_id"]): (
                        int(row["completed_count"]),
                        int(row["total_count"]),
                    )
                    for row in cursor.fetchall()
                }

    def list_ai_generation_tasks(
        self,
        actor: AuthUser,
        *,
        limit: int = 5000,
    ) -> list[ProjectTask]:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能进入 AI 生图")
        conditions = [
            "t.status NOT IN (%s, %s, %s, %s, %s)",
            "p.status NOT IN (%s, %s)",
        ]
        params: list[Any] = [
            TASK_APPROVED,
            TASK_SKIPPED,
            TASK_CANCELLED,
            TASK_EXPANDED,
            TASK_AWAITING_ACCEPTANCE,
            PROJECT_COMPLETED,
            PROJECT_CANCELLED,
        ]
        if actor.role != ROLE_ADMIN:
            conditions.append("t.assignee_id = %s")
            params.append(actor.id)
        params.append(limit)
        query = (
            self._task_select_sql()
            + " WHERE "
            + " AND ".join(conditions)
            + """
              AND (
                  t.parent_task_id IS NULL
                  OR (
                      t.parent_task_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1
                          FROM generation_attempts failed
                          WHERE failed.task_id = t.id
                            AND failed.status IN (%s, %s)
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM generation_attempts succeeded
                          WHERE succeeded.task_id = t.id
                            AND succeeded.status = %s
                      )
                  )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM generation_attempts active
                  WHERE active.task_id = t.id
                    AND active.status IN (%s, %s)
              )
              ORDER BY p.is_urgent DESC, p.source_date, p.id, t.task_no
              LIMIT %s
            """
        )
        params_with_attempts = [
            *params[:-1],
            ATTEMPT_FAILED,
            ATTEMPT_CANCELLED,
            ATTEMPT_SUCCEEDED,
            ATTEMPT_QUEUED,
            ATTEMPT_RUNNING,
            limit,
        ]
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, tuple(params_with_attempts))
                return [
                    _project_task(row)
                    for row in cursor.fetchall()
                ]

    def list_ai_generation_tasks_page(
        self,
        actor: AuthUser,
        query: TaskQuery,
        page: PageRequest,
    ) -> PageResult[ProjectTask]:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能进入 AI 生图")
        conditions = [
            "t.status NOT IN (%s, %s, %s, %s, %s)",
            "p.status NOT IN (%s, %s)",
        ]
        params: list[Any] = [
            TASK_APPROVED,
            TASK_SKIPPED,
            TASK_CANCELLED,
            TASK_EXPANDED,
            TASK_AWAITING_ACCEPTANCE,
            PROJECT_COMPLETED,
            PROJECT_CANCELLED,
        ]
        if actor.role != ROLE_ADMIN:
            conditions.append("t.assignee_id = %s")
            params.append(actor.id)
        if query.start_date is not None:
            conditions.append("t.created_at >= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(query.start_date, time.min)
                )
            )
        if query.end_date is not None:
            conditions.append("t.created_at <= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(query.end_date, time.max)
                )
            )
        keyword = query.keyword.strip()
        if keyword:
            pattern = f"%{keyword}%"
            conditions.append(
                "("
                "t.task_code LIKE %s OR p.project_code LIKE %s OR "
                "p.product_code LIKE %s OR p.name LIKE %s OR "
                "p.project_name LIKE %s OR "
                "COALESCE(assignee.display_name, '') LIKE %s"
                ")"
            )
            params.extend((pattern,) * 6)
        scope_sql = (
            " WHERE "
            + " AND ".join(conditions)
            + """
              AND (
                  t.parent_task_id IS NULL
                  OR (
                      t.parent_task_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1
                          FROM generation_attempts failed
                          WHERE failed.task_id = t.id
                            AND failed.status IN (%s, %s)
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM generation_attempts succeeded
                          WHERE succeeded.task_id = t.id
                            AND succeeded.status = %s
                      )
                  )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM generation_attempts active
                  WHERE active.task_id = t.id
                    AND active.status IN (%s, %s)
              )
            """
        )
        base = self._task_select_sql() + scope_sql
        base_params = (
            *params,
            ATTEMPT_FAILED,
            ATTEMPT_CANCELLED,
            ATTEMPT_SUCCEEDED,
            ATTEMPT_QUEUED,
            ATTEMPT_RUNNING,
        )
        sort_fields = {
            "task_code": "t.task_code",
            "product_code": "p.product_code",
            "project_name": "p.project_name",
            "status": "t.status",
            "created_at": "t.created_at",
        }
        sort_field = sort_fields.get(page.sort_field, "t.created_at")
        direction = "ASC" if page.sort_direction == "asc" else "DESC"
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM project_tasks t
                    JOIN projects p ON p.id = t.project_id
                    LEFT JOIN users assignee
                      ON assignee.id = t.assignee_id
                    """
                    + scope_sql,
                    base_params,
                )
                total = int(cursor.fetchone()["count"])
                cursor.execute(
                    base
                    + f"""
                    ORDER BY p.is_urgent DESC, {sort_field} {direction},
                             p.id, t.task_no
                    LIMIT %s OFFSET %s
                    """,
                    (*base_params, page.page_size, page.offset),
                )
                rows = cursor.fetchall()
        return PageResult(
            [_project_task(row) for row in rows],
            total,
            page.page,
            page.page_size,
        )

    def split_and_queue_generations(
        self,
        source_task_id: int,
        actor: AuthUser,
        *,
        pairs: Sequence[GenerationPairSpec],
    ) -> list[SplitGenerationItem]:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能生成项目图片")
        clean_pairs = tuple(pairs)
        if not clean_pairs:
            raise ValueError("至少需要一个配对任务")
        now = utc_now()
        results: list[SplitGenerationItem] = []
        with self.connection() as connection:
            with connection.cursor() as cursor:
                source = self._task_row_for_update(cursor, source_task_id)
                if source.get("parent_task_id") is not None:
                    raise StateConflictError("拆后子任务不能再拆分")
                if source["status"] not in (
                    TASK_CLAIMED,
                    TASK_WORKING,
                    TASK_RETURNED,
                ) and not (
                    actor.role == ROLE_ADMIN
                    and source["status"] == TASK_AVAILABLE
                ):
                    raise StateConflictError("当前子任务不能生成")
                if (
                    int(source["assignee_id"] or 0) != actor.id
                    and actor.role != ROLE_ADMIN
                ):
                    raise PermissionDeniedError("该子任务不属于当前账号")
                child_assignee_id = int(
                    source["assignee_id"] or actor.id
                )
                if (
                    actor.role == ROLE_ADMIN
                    and source["status"] == TASK_AVAILABLE
                ):
                    child_assignee_id = actor.id
                cursor.execute(
                    """
                    SELECT tg.garment_asset_id, asset.content_hash
                    FROM task_garments tg
                    JOIN project_assets asset
                      ON asset.id = tg.garment_asset_id
                    WHERE tg.task_id = %s
                    ORDER BY tg.position
                    FOR UPDATE
                    """,
                    (source_task_id,),
                )
                garment_rows = cursor.fetchall()
                if not garment_rows:
                    raise StateConflictError("原子任务没有衣服素材")
                hash_to_asset = {
                    row["content_hash"]: int(row["garment_asset_id"])
                    for row in garment_rows
                }
                fallback_garment_id = int(garment_rows[0]["garment_asset_id"])
                cursor.execute(
                    """
                    SELECT task_no
                    FROM project_tasks
                    WHERE project_id = %s
                    ORDER BY task_no DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (int(source["project_id"]),),
                )
                last_task = cursor.fetchone()
                next_task_no = (
                    int(last_task["task_no"]) + 1
                    if last_task is not None
                    else 1
                )
                for pair in clean_pairs:
                    prompt = pair.prompt_text.strip()
                    if not prompt and not pair.skipped:
                        raise ValueError("提示词不能为空")
                    if pair.model not in MODEL_IDS:
                        raise ValueError("不支持的模型")
                    if not 1 <= pair.n <= 4:
                        raise ValueError("每次生成数量必须在 1 到 4 之间")
                    if pair.unit_price < 0:
                        raise ValueError("模型单价不能为负数")
                    if not 1 <= len(pair.garment_hashes) <= 13:
                        raise ValueError("每个配对必须包含 1 到 13 件衣服")
                    selected_assets = tuple(
                        dict.fromkeys(
                            hash_to_asset.get(content_hash)
                            for content_hash in pair.garment_hashes
                            if hash_to_asset.get(content_hash) is not None
                        )
                    )
                    if not selected_assets:
                        selected_assets = (fallback_garment_id,)
                    task_no = next_task_no
                    next_task_no += 1
                    child_code = (
                        f"{source['project_code']}-{task_no:02d}"
                    )
                    child_status = (
                        TASK_SKIPPED if pair.skipped else TASK_CLAIMED
                    )
                    cursor.execute(
                        """
                        INSERT INTO project_tasks(
                            task_code, project_id, task_no,
                            garment_asset_id, model_asset_id, status,
                            assignee_id, prompt_text, model,
                            aspect_ratio, resolution, n, final_quantity,
                            source_revision, parent_task_id,
                            created_at, updated_at, claimed_at,
                            approved_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, 0, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            child_code,
                            int(source["project_id"]),
                            task_no,
                            selected_assets[0],
                            int(source["model_asset_id"]),
                            child_status,
                            child_assignee_id,
                            prompt,
                            pair.model,
                            pair.aspect_ratio,
                            pair.resolution,
                            pair.n,
                            int(source["source_revision"] or 1),
                            source_task_id,
                            now,
                            now,
                            now,
                            now if pair.skipped else None,
                        ),
                    )
                    child_task_id = int(cursor.lastrowid)
                    for position, garment_asset_id in enumerate(
                        selected_assets,
                        start=1,
                    ):
                        cursor.execute(
                            """
                            INSERT INTO task_garments(
                                task_id, garment_asset_id, position
                            ) VALUES (%s, %s, %s)
                            """,
                            (
                                child_task_id,
                                garment_asset_id,
                                position,
                            ),
                        )
                    attempt_id: int | None = None
                    if not pair.skipped:
                        attempt_id = self._insert_attempt(
                            cursor,
                            task_id=child_task_id,
                            attempt_no=1,
                            operator_id=actor.id,
                            model=pair.model,
                            prompt_text=prompt,
                            aspect_ratio=pair.aspect_ratio,
                            resolution=pair.resolution,
                            n=pair.n,
                            status=ATTEMPT_QUEUED,
                            source_revision=int(
                                source["source_revision"] or 1
                            ),
                            unit_price=pair.unit_price,
                            created_at=now,
                            requested_provider_code=(
                                pair.requested_provider_code
                            ),
                            requested_provider_name=(
                                pair.requested_provider_name
                            ),
                        )
                        self._event(
                            cursor,
                            int(source["project_id"]),
                            child_task_id,
                            actor.id,
                            "generation_queued",
                            TASK_CLAIMED,
                            TASK_CLAIMED,
                            {
                                "attempt_id": attempt_id,
                                "parent_task_id": source_task_id,
                            },
                        )
                    results.append(
                        SplitGenerationItem(
                            task_id=child_task_id,
                            task_code=child_code,
                            attempt_id=attempt_id,
                            skipped=pair.skipped,
                        )
                    )
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s, split_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (TASK_EXPANDED, now, now, source_task_id),
                )
                self._update_project_status(
                    cursor,
                    int(source["project_id"]),
                )
                self._event(
                    cursor,
                    int(source["project_id"]),
                    source_task_id,
                    actor.id,
                    "task_split",
                    source["status"],
                    TASK_EXPANDED,
                    {
                        "child_count": len(results),
                        "skipped_count": sum(
                            item.skipped for item in results
                        ),
                    },
                )
        return results

    def claim_task(self, task_id: int, actor: AuthUser) -> ProjectTask:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("只有 AI 制作人员或管理员可以领取任务")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                row = self._task_row_for_update(cursor, task_id)
                if row["project_status"] in (PROJECT_CANCELLED, PROJECT_COMPLETED):
                    raise StateConflictError("项目已结束，不能领取任务")
                status = row["status"]
                assignee_id = row["assignee_id"]
                if status == TASK_AVAILABLE:
                    pass
                elif status == TASK_RETURNED and (
                    int(assignee_id) == actor.id or actor.role == ROLE_ADMIN
                ):
                    pass
                else:
                    raise StateConflictError("该任务已被其他人员领取")
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s, assignee_id = %s, claimed_at = %s, updated_at = %s
                    WHERE id = %s AND status = %s
                    """,
                    (TASK_CLAIMED, actor.id, now, now, task_id, status),
                )
                if cursor.rowcount != 1:
                    raise StateConflictError("任务已被其他人抢先领取")
                self._update_project_status(cursor, int(row["project_id"]))
                self._event(
                    cursor,
                    int(row["project_id"]),
                    task_id,
                    actor.id,
                    "task_claimed",
                    status,
                    TASK_CLAIMED,
                    {},
                )
        task = self.get_task(task_id)
        if task is None:
            raise StateConflictError("任务领取后无法读取")
        return task

    def get_account_policy(self, user_id: int) -> dict[str, int]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT urgent_quota_per_day, max_generation_concurrency
                    FROM users WHERE id = %s AND deleted_at IS NULL
                    """,
                    (user_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise ValueError("账号不存在")
        return {
            "urgent_quota_per_day": int(row["urgent_quota_per_day"]),
            "max_generation_concurrency": int(
                row["max_generation_concurrency"]
            ),
        }

    def set_project_urgent(
        self,
        project_id: int,
        actor: AuthUser,
        urgent: bool,
    ) -> None:
        today = date.today()
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                project = self._project_row_for_update(cursor, project_id)
                if (
                    actor.role != ROLE_ADMIN
                    and int(project["owner_id"]) != actor.id
                ):
                    raise PermissionDeniedError("只能设置自己任务的加急")
                if project["status"] in (
                    PROJECT_CANCELLED,
                    PROJECT_COMPLETED,
                ):
                    raise StateConflictError("已结束任务不能修改加急")
                if urgent and not bool(project["is_urgent"]):
                    cursor.execute(
                        "SELECT urgent_quota_per_day FROM users "
                        "WHERE id = %s FOR UPDATE",
                        (int(project["owner_id"]),),
                    )
                    quota = int(
                        cursor.fetchone()["urgent_quota_per_day"]
                    )
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS count FROM projects
                        WHERE owner_id = %s AND urgent_date = %s
                          AND is_urgent = 1 AND id <> %s
                        """,
                        (int(project["owner_id"]), today, project_id),
                    )
                    if int(cursor.fetchone()["count"]) >= quota:
                        raise StateConflictError(
                            f"今日加急额度已用完（上限 {quota} 个）"
                        )
                cursor.execute(
                    """
                    UPDATE projects
                    SET is_urgent = %s, urgent_date = %s,
                        urgent_by = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        int(urgent),
                        today if urgent else None,
                        actor.id if urgent else None,
                        now,
                        project_id,
                    ),
                )
                self._event(
                    cursor,
                    project_id,
                    None,
                    actor.id,
                    "project_urgent" if urgent else "project_urgent_cleared",
                    project["status"],
                    project["status"],
                    {
                        "urgent": urgent,
                        "urgent_date": today.isoformat() if urgent else "",
                    },
                )

    def remaining_urgent_quota(self, actor: AuthUser, day: date) -> int:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                        "SELECT urgent_quota_per_day FROM users "
                        "WHERE id = %s AND deleted_at IS NULL",
                    (actor.id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("账号不存在")
                quota = int(row["urgent_quota_per_day"])
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count FROM projects
                    WHERE owner_id = %s AND urgent_date = %s
                      AND is_urgent = 1
                    """,
                    (actor.id, day),
                )
                used = int(cursor.fetchone()["count"])
                return max(0, quota - used)

    def update_subtask_sources(
        self,
        task_id: int,
        actor: AuthUser,
        *,
        garment_paths: Sequence[str],
        model_path: str,
        new_assets: Sequence[ProjectAssetInput] = (),
    ) -> None:
        garment_paths = tuple(dict.fromkeys(path.strip() for path in garment_paths))
        if not garment_paths:
            raise ValueError("子任务至少需要一张衣服图")
        if len(garment_paths) > 13:
            raise ValueError("子任务最多支持 13 张衣服图")
        if not model_path.strip():
            raise ValueError("子任务必须选择一张模特图")
        pending_assets = tuple(
            dict.fromkeys(
                (
                    asset.role,
                    asset.relative_path,
                    asset.original_name,
                    asset.content_hash,
                    asset.mime,
                    asset.width,
                    asset.height,
                )
                for asset in new_assets
            )
        )
        for asset in pending_assets:
            if asset[0] not in ("garment", "model"):
                raise ValueError("新增素材角色不正确")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                if actor.role != ROLE_ADMIN and int(task["owner_id"]) != actor.id:
                    raise PermissionDeniedError("只能修改自己任务下的子任务")
                if task["status"] in (
                    TASK_AWAITING_ACCEPTANCE,
                    TASK_APPROVED,
                    TASK_CANCELLED,
                ):
                    raise StateConflictError("待审核或已完成的子任务不能修改素材")
                added_count = 0
                for asset in pending_assets:
                    cursor.execute(
                        """
                        SELECT id FROM project_assets
                        WHERE project_id = %s AND role = %s
                          AND relative_path = %s
                        LIMIT 1
                        """,
                        (task["project_id"], asset[0], asset[1]),
                    )
                    if cursor.fetchone() is not None:
                        continue
                    cursor.execute(
                        """
                        INSERT INTO project_assets(
                            project_id, role, relative_path, original_name,
                            content_hash, mime, width, height, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            task["project_id"],
                            *asset,
                            utc_now(),
                        ),
                    )
                    added_count += 1
                placeholders = ", ".join(["%s"] * len(garment_paths))
                cursor.execute(
                    f"""
                    SELECT id, relative_path
                    FROM project_assets
                    WHERE project_id = %s AND role = 'garment'
                      AND relative_path IN ({placeholders})
                    """,
                    (task["project_id"], *garment_paths),
                )
                garment_rows = cursor.fetchall()
                garment_by_path = {
                    row["relative_path"]: int(row["id"])
                    for row in garment_rows
                }
                if set(garment_by_path) != set(garment_paths):
                    raise ValueError("部分衣服图不属于当前任务")
                cursor.execute(
                    """
                    SELECT id FROM project_assets
                    WHERE project_id = %s AND role = 'model'
                      AND relative_path = %s
                    """,
                    (task["project_id"], model_path),
                )
                model_row = cursor.fetchone()
                if model_row is None:
                    raise ValueError("模特图不属于当前任务")
                garment_ids = [garment_by_path[path] for path in garment_paths]
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET garment_asset_id = %s, model_asset_id = %s,
                        source_revision = source_revision + 1,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (garment_ids[0], int(model_row["id"]), utc_now(), task_id),
                )
                cursor.execute(
                    "DELETE FROM task_garments WHERE task_id = %s",
                    (task_id,),
                )
                for position, asset_id in enumerate(garment_ids, start=1):
                    cursor.execute(
                        """
                        INSERT INTO task_garments(
                            task_id, garment_asset_id, position
                        ) VALUES (%s, %s, %s)
                        """,
                        (task_id, asset_id, position),
                    )
                cursor.execute(
                    """
                    UPDATE projects
                    SET source_revision = source_revision + 1,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (utc_now(), task["project_id"]),
                )
                self._event(
                    cursor,
                    int(task["project_id"]),
                    task_id,
                    actor.id,
                    "subtask_sources_updated",
                    task["status"],
                    task["status"],
                    {
                        "garments": list(garment_paths),
                        "model": model_path,
                        "added_assets": added_count,
                    },
                )

    @staticmethod
    def _next_attempt_no(cursor, task_id: int) -> int:
        cursor.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no "
            "FROM generation_attempts WHERE task_id = %s",
            (task_id,),
        )
        return int(cursor.fetchone()["next_no"])

    @staticmethod
    def _insert_attempt(
        cursor,
        *,
        task_id: int,
        attempt_no: int,
        operator_id: int,
        model: str,
        prompt_text: str,
        aspect_ratio: str,
        resolution: str | None,
        n: int,
        status: str,
        source_revision: int,
        unit_price: Decimal,
        created_at: datetime,
        started_at: datetime | None = None,
        requested_provider_code: str = "",
        requested_provider_name: str = "",
    ) -> int:
        cursor.execute(
            """
            INSERT INTO generation_attempts(
                task_id, attempt_no, operator_id, model, prompt_text,
                aspect_ratio, resolution, n, status, source_revision,
                unit_price, requested_provider_code, requested_provider_name,
                created_at, started_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            """,
            (
                task_id,
                attempt_no,
                operator_id,
                model,
                prompt_text,
                aspect_ratio,
                resolution,
                n,
                status,
                source_revision,
                unit_price,
                requested_provider_code,
                requested_provider_name,
                created_at,
                started_at,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _assign_task_to_actor(
        cursor,
        task_id: int,
        actor_id: int,
        now: datetime,
        *,
        keep_claimed_at: bool = False,
    ) -> None:
        claimed_at_sql = (
            "COALESCE(claimed_at, %s)" if keep_claimed_at else "%s"
        )
        cursor.execute(
            f"""
            UPDATE project_tasks
            SET assignee_id = %s, status = %s,
                claimed_at = {claimed_at_sql}, updated_at = %s
            WHERE id = %s
            """,
            (actor_id, TASK_CLAIMED, now, now, task_id),
        )

    @staticmethod
    def _update_task_generation_params(
        cursor,
        task_id: int,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str,
        resolution: str | None,
        n: int,
        now: datetime,
        start_working: bool = False,
        work_started_at: datetime | None = None,
    ) -> None:
        if start_working:
            cursor.execute(
                """
                UPDATE project_tasks
                SET status = %s, prompt_text = %s, model = %s,
                    aspect_ratio = %s, resolution = %s, n = %s,
                    work_started_at = %s, updated_at = %s
                WHERE id = %s
                """,
                (
                    TASK_WORKING,
                    prompt,
                    model,
                    aspect_ratio,
                    resolution,
                    n,
                    work_started_at,
                    now,
                    task_id,
                ),
            )
            return
        cursor.execute(
            """
            UPDATE project_tasks
            SET prompt_text = %s, model = %s, aspect_ratio = %s,
                resolution = %s, n = %s, updated_at = %s
            WHERE id = %s
            """,
            (prompt, model, aspect_ratio, resolution, n, now, task_id),
        )

    @staticmethod
    def _attempt_record(
        *,
        attempt_id: int,
        task_id: int,
        attempt_no: int,
        actor: AuthUser,
        model: str,
        prompt_text: str,
        aspect_ratio: str,
        resolution: str | None,
        n: int,
        status: str,
        source_revision: int,
        unit_price: Decimal,
        created_at: datetime,
        operator_name: str = "",
        requested_provider_code: str = "",
        requested_provider_name: str = "",
    ) -> GenerationAttempt:
        return GenerationAttempt(
            id=attempt_id,
            task_id=task_id,
            attempt_no=attempt_no,
            operator_id=actor.id,
            model=model,
            prompt_text=prompt_text,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            n=n,
            status=status,
            source_revision=source_revision,
            unit_price=unit_price,
            generated_image_count=0,
            operator_name=operator_name,
            created_at=created_at,
            requested_provider_code=requested_provider_code,
            requested_provider_name=requested_provider_name,
        )

    def create_generation_attempt(
        self,
        task_id: int,
        actor: AuthUser,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str,
        resolution: str | None,
        n: int,
        unit_price: Decimal,
        requested_provider_code: str = "",
        requested_provider_name: str = "",
    ) -> GenerationAttempt:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("提示词不能为空")
        if model not in MODEL_IDS:
            raise ValueError("不支持的模型")
        if not 1 <= n <= 4:
            raise ValueError("每次生成数量必须在 1 到 4 之间")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                if actor.role not in (ROLE_AI, ROLE_ADMIN):
                    raise PermissionDeniedError("当前角色不能调用生图接口")
                if (
                    int(task["assignee_id"] or 0) != actor.id
                    and actor.role != ROLE_ADMIN
                ):
                    raise PermissionDeniedError("请先领取该任务")
                if actor.role == ROLE_ADMIN and not task["assignee_id"]:
                    self._assign_task_to_actor(
                        cursor,
                        task_id,
                        actor.id,
                        now,
                        keep_claimed_at=True,
                    )
                    task["status"] = TASK_CLAIMED
                if task["status"] not in (TASK_CLAIMED, TASK_WORKING, TASK_RETURNED):
                    raise StateConflictError("当前任务状态不能生成图片")
                self._assert_generation_slot(cursor, actor.id)
                attempt_no = self._next_attempt_no(cursor, task_id)
                attempt_id = self._insert_attempt(
                    cursor,
                    task_id=task_id,
                    attempt_no=attempt_no,
                    operator_id=actor.id,
                    model=model,
                    prompt_text=prompt,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    n=n,
                    status=ATTEMPT_RUNNING,
                    source_revision=int(task.get("source_revision") or 1),
                    unit_price=unit_price,
                    created_at=now,
                    started_at=now,
                    requested_provider_code=requested_provider_code,
                    requested_provider_name=requested_provider_name,
                )
                previous_status = task["status"]
                work_started_at = task["work_started_at"] or now
                self._update_task_generation_params(
                    cursor,
                    task_id,
                    prompt=prompt,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    n=n,
                    now=now,
                    start_working=True,
                    work_started_at=work_started_at,
                )
                self._update_project_status(cursor, int(task["project_id"]))
                self._event(
                    cursor,
                    int(task["project_id"]),
                    task_id,
                    actor.id,
                    "generation_started",
                    previous_status,
                    TASK_WORKING,
                    {
                        "attempt_id": attempt_id,
                        "attempt_no": attempt_no,
                        "model": model,
                    },
                )
        return self._attempt_record(
            attempt_id=attempt_id,
            task_id=task_id,
            attempt_no=attempt_no,
            actor=actor,
            model=model,
            prompt_text=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            n=n,
            status=ATTEMPT_RUNNING,
            source_revision=int(task.get("source_revision") or 1),
            unit_price=unit_price,
            created_at=now,
            requested_provider_code=requested_provider_code,
            requested_provider_name=requested_provider_name,
        )

    def queue_generation_attempt(
        self,
        task_id: int,
        actor: AuthUser,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str,
        resolution: str | None,
        n: int,
        unit_price: Decimal,
        requested_provider_code: str = "",
        requested_provider_name: str = "",
        allow_reopen: bool = False,
    ) -> GenerationAttempt:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("提示词不能为空")
        if model not in MODEL_IDS:
            raise ValueError("不支持的模型")
        if not 1 <= n <= 4:
            raise ValueError("每次生成数量必须在 1 到 4 之间")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                if actor.role not in (ROLE_AI, ROLE_ADMIN):
                    raise PermissionDeniedError("当前角色不能调用生图接口")
                if (
                    int(task["assignee_id"] or 0) != actor.id
                    and actor.role != ROLE_ADMIN
                ):
                    raise PermissionDeniedError("请先领取该任务")
                if actor.role == ROLE_ADMIN and not task["assignee_id"]:
                    self._assign_task_to_actor(
                        cursor,
                        task_id,
                        actor.id,
                        now,
                        keep_claimed_at=True,
                    )
                    task["status"] = TASK_CLAIMED
                reopen = (
                    task["status"] in (TASK_APPROVED, TASK_SKIPPED)
                    and allow_reopen
                )
                if (
                    task["status"] in (
                        TASK_AWAITING_ACCEPTANCE,
                        TASK_CANCELLED,
                    )
                    or (
                        task["status"] in (TASK_APPROVED, TASK_SKIPPED)
                        and not allow_reopen
                    )
                ):
                    raise StateConflictError("当前任务状态不能加入生成队列")
                if reopen:
                    cursor.execute(
                        """
                        UPDATE project_tasks
                        SET status = %s, approved_at = NULL,
                            final_quantity = 0, updated_at = %s
                        WHERE id = %s
                        """,
                        (TASK_CLAIMED, now, task_id),
                    )
                attempt_no = self._next_attempt_no(cursor, task_id)
                attempt_id = self._insert_attempt(
                    cursor,
                    task_id=task_id,
                    attempt_no=attempt_no,
                    operator_id=actor.id,
                    model=model,
                    prompt_text=prompt,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    n=n,
                    status=ATTEMPT_QUEUED,
                    source_revision=int(task["source_revision"] or 1),
                    unit_price=unit_price,
                    created_at=now,
                    requested_provider_code=requested_provider_code,
                    requested_provider_name=requested_provider_name,
                )
                self._update_task_generation_params(
                    cursor,
                    task_id,
                    prompt=prompt,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    n=n,
                    now=now,
                )
                if reopen:
                    self._update_project_status(
                        cursor,
                        int(task["project_id"]),
                    )
                    self._event(
                        cursor,
                        int(task["project_id"]),
                        task_id,
                        actor.id,
                        "task_reopened_for_retry",
                        task["status"],
                        TASK_CLAIMED,
                        {"attempt_id": attempt_id},
                    )
                else:
                    self._update_project_status(
                        cursor,
                        int(task["project_id"]),
                    )
                self._event(
                    cursor,
                    int(task["project_id"]),
                    task_id,
                    actor.id,
                    "generation_queued",
                    task["status"],
                    task["status"],
                    {"attempt_id": attempt_id, "attempt_no": attempt_no},
                )
        return self._attempt_record(
            attempt_id=attempt_id,
            task_id=task_id,
            attempt_no=attempt_no,
            actor=actor,
            model=model,
            prompt_text=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            n=n,
            status=ATTEMPT_QUEUED,
            source_revision=int(task["source_revision"] or 1),
            unit_price=unit_price,
            created_at=now,
            operator_name=actor.display_name,
            requested_provider_code=requested_provider_code,
            requested_provider_name=requested_provider_name,
        )

    def claim_and_queue_generation(
        self,
        task_id: int,
        actor: AuthUser,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str,
        resolution: str | None,
        n: int,
        unit_price: Decimal,
        requested_provider_code: str = "",
        requested_provider_name: str = "",
    ) -> GenerationAttempt:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("提示词不能为空")
        if model not in MODEL_IDS:
            raise ValueError("不支持的模型")
        if not 1 <= n <= 4:
            raise ValueError("每次生成数量必须在 1 到 4 之间")
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能领取并生成任务")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                if task["status"] in (
                    TASK_AWAITING_ACCEPTANCE,
                    TASK_APPROVED,
                    TASK_CANCELLED,
                ):
                    raise StateConflictError("任务已进入审核或已经结束")
                assignee_id = task["assignee_id"]
                claimed_here = False
                if task["status"] == TASK_AVAILABLE:
                    claimed_here = True
                elif task["status"] == TASK_RETURNED and (
                    int(assignee_id or 0) == actor.id or actor.role == ROLE_ADMIN
                ):
                    # 管理员接手别人的退回任务时，把任务改派给自己。
                    claimed_here = int(assignee_id or 0) != actor.id
                elif int(assignee_id or 0) == actor.id and task["status"] in (
                    TASK_CLAIMED,
                    TASK_WORKING,
                    TASK_RETURNED,
                ):
                    claimed_here = False
                else:
                    raise StateConflictError("任务已被其他人员领取")
                if claimed_here:
                    self._assign_task_to_actor(
                        cursor,
                        task_id,
                        actor.id,
                        now,
                    )
                    self._event(
                        cursor,
                        int(task["project_id"]),
                        task_id,
                        actor.id,
                        "task_claimed",
                        task["status"],
                        TASK_CLAIMED,
                        {},
                    )
                attempt_no = self._next_attempt_no(cursor, task_id)
                attempt_id = self._insert_attempt(
                    cursor,
                    task_id=task_id,
                    attempt_no=attempt_no,
                    operator_id=actor.id,
                    model=model,
                    prompt_text=prompt,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    n=n,
                    status=ATTEMPT_QUEUED,
                    source_revision=int(task["source_revision"] or 1),
                    unit_price=unit_price,
                    created_at=now,
                    requested_provider_code=requested_provider_code,
                    requested_provider_name=requested_provider_name,
                )
                self._update_task_generation_params(
                    cursor,
                    task_id,
                    prompt=prompt,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    n=n,
                    now=now,
                )
                self._update_project_status(
                    cursor,
                    int(task["project_id"]),
                )
                event_status = TASK_CLAIMED if claimed_here else task["status"]
                self._event(
                    cursor,
                    int(task["project_id"]),
                    task_id,
                    actor.id,
                    "generation_queued",
                    event_status,
                    event_status,
                    {
                        "attempt_id": attempt_id,
                        "attempt_no": attempt_no,
                        "claimed_here": claimed_here,
                    },
                )
        return self._attempt_record(
            attempt_id=attempt_id,
            task_id=task_id,
            attempt_no=attempt_no,
            actor=actor,
            model=model,
            prompt_text=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            n=n,
            status=ATTEMPT_QUEUED,
            source_revision=int(task["source_revision"] or 1),
            unit_price=unit_price,
            created_at=now,
            operator_name=actor.display_name,
            requested_provider_code=requested_provider_code,
            requested_provider_name=requested_provider_name,
        )

    def start_queued_generation_attempt(
        self,
        attempt_id: int,
        actor: AuthUser,
    ) -> GenerationAttempt:
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                attempt = self._attempt_row_for_update(cursor, attempt_id)
                if int(attempt["operator_id"]) != actor.id:
                    raise PermissionDeniedError("不能启动其他人的生成任务")
                if attempt["status"] != ATTEMPT_QUEUED:
                    raise StateConflictError("生成任务已经启动或结束")
                task = self._task_row_for_update(cursor, int(attempt["task_id"]))
                if (
                    int(task["assignee_id"] or 0) != actor.id
                    and actor.role != ROLE_ADMIN
                ):
                    raise PermissionDeniedError("任务已不再由当前账号处理")
                if task["status"] in (
                    TASK_AWAITING_ACCEPTANCE,
                    TASK_APPROVED,
                    TASK_CANCELLED,
                ):
                    raise StateConflictError("任务已进入审核或已经结束")
                if int(attempt["source_revision"] or 1) != int(
                    task["source_revision"] or 1
                ):
                    raise StateConflictError(
                        "排队期间素材已变更，请重新加入生成队列"
                    )
                self._assert_generation_slot(cursor, actor.id)
                cursor.execute(
                    """
                    UPDATE generation_attempts
                    SET status = %s, started_at = %s
                    WHERE id = %s
                    """,
                    (ATTEMPT_RUNNING, now, attempt_id),
                )
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s, prompt_text = %s, model = %s,
                        aspect_ratio = %s, resolution = %s, n = %s,
                        work_started_at = COALESCE(work_started_at, %s),
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        TASK_WORKING,
                        attempt["prompt_text"],
                        attempt["model"],
                        attempt["aspect_ratio"],
                        attempt["resolution"],
                        attempt["n"],
                        now,
                        now,
                        task["id"],
                    ),
                )
                self._update_project_status(
                    cursor,
                    int(task["project_id"]),
                )
                self._event(
                    cursor,
                    int(task["project_id"]),
                    int(task["id"]),
                    actor.id,
                    "generation_started",
                    task["status"],
                    TASK_WORKING,
                    {"attempt_id": attempt_id},
                )
        return _generation_attempt(
            {
                **attempt,
                "status": ATTEMPT_RUNNING,
                "started_at": now,
                "operator_name": actor.display_name,
            }
        )

    def list_queued_attempts(self, operator_id: int) -> list[GenerationAttempt]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT ga.*, u.display_name AS operator_name
                    FROM generation_attempts ga
                    JOIN users u ON u.id = ga.operator_id
                    WHERE ga.operator_id = %s AND ga.status = %s
                    ORDER BY ga.id
                    """,
                    (operator_id, ATTEMPT_QUEUED),
                )
                return [_generation_attempt(row) for row in cursor.fetchall()]

    def create_manual_attempt(
        self,
        task_id: int,
        actor: AuthUser,
    ) -> GenerationAttempt:
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                if actor.role not in (ROLE_AI, ROLE_ADMIN):
                    raise PermissionDeniedError("当前角色不能上传成品")
                assignee_id = int(task["assignee_id"] or 0)
                claimed_here = False
                if task["status"] == TASK_AVAILABLE:
                    claimed_here = True
                elif task["status"] == TASK_RETURNED and (
                    assignee_id == actor.id or actor.role == ROLE_ADMIN
                ):
                    # 管理员接手别人的退回任务时，把任务改派给自己。
                    claimed_here = assignee_id != actor.id
                elif (
                    (assignee_id == actor.id or actor.role == ROLE_ADMIN)
                    and task["status"] in (
                    TASK_CLAIMED,
                    TASK_WORKING,
                    )
                ):
                    claimed_here = False
                else:
                    raise PermissionDeniedError("任务已被其他制作人员领取")
                if task["status"] in (
                    TASK_AWAITING_ACCEPTANCE,
                    TASK_APPROVED,
                    TASK_CANCELLED,
                ):
                    raise StateConflictError("当前子任务状态不能上传成品")
                if claimed_here:
                    self._assign_task_to_actor(
                        cursor,
                        task_id,
                        actor.id,
                        now,
                    )
                    self._event(
                        cursor,
                        int(task["project_id"]),
                        task_id,
                        actor.id,
                        "task_claimed",
                        task["status"],
                        TASK_CLAIMED,
                        {"claimed_for_manual_upload": True},
                    )
                self._assert_generation_slot(cursor, actor.id)
                attempt_no = self._next_attempt_no(cursor, task_id)
                attempt_id = self._insert_attempt(
                    cursor,
                    task_id=task_id,
                    attempt_no=attempt_no,
                    operator_id=actor.id,
                    model="manual_ps",
                    prompt_text=task["prompt_text"],
                    aspect_ratio=task["aspect_ratio"],
                    resolution=task["resolution"],
                    n=0,
                    status=ATTEMPT_RUNNING,
                    source_revision=int(task["source_revision"] or 1),
                    unit_price=Decimal("0"),
                    created_at=now,
                    started_at=now,
                )
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (TASK_WORKING, now, task_id),
                )
                self._update_project_status(
                    cursor,
                    int(task["project_id"]),
                )
                self._event(
                    cursor,
                    int(task["project_id"]),
                    task_id,
                    actor.id,
                    "manual_upload_started",
                    task["status"],
                    TASK_WORKING,
                    {"attempt_id": attempt_id, "attempt_no": attempt_no},
                )
        return self._attempt_record(
            attempt_id=attempt_id,
            task_id=task_id,
            attempt_no=attempt_no,
            actor=actor,
            model="manual_ps",
            prompt_text=task["prompt_text"],
            aspect_ratio=task["aspect_ratio"],
            resolution=task["resolution"],
            n=0,
            status=ATTEMPT_RUNNING,
            source_revision=int(task["source_revision"] or 1),
            unit_price=Decimal("0"),
            created_at=now,
        )

    def complete_generation_attempt(
        self,
        attempt_id: int,
        actor: AuthUser,
        *,
        request_id: str,
        remote_task_id: str,
        raw_response: dict[str, Any],
        input_tokens: int | None,
        output_tokens: int | None,
        results: Sequence[GeneratedResultInput],
        source_type: str = "generated",
        provider_id: int | None = None,
        provider_code: str = "",
        provider_name: str = "",
        provider_model: str = "",
    ) -> None:
        if not results:
            raise ValueError("生成结果不能为空")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                attempt = self._attempt_row_for_update(cursor, attempt_id)
                if int(attempt["operator_id"]) != actor.id and actor.role != ROLE_ADMIN:
                    raise PermissionDeniedError("不能修改其他人的生成记录")
                if attempt["status"] != ATTEMPT_RUNNING:
                    raise StateConflictError("生成记录状态已经变化")
                task = self._task_row_for_update(
                    cursor,
                    int(attempt["task_id"]),
                )
                if (
                    task["status"] in (TASK_CANCELLED, TASK_APPROVED)
                    or task["project_status"] == PROJECT_CANCELLED
                ):
                    raise StateConflictError(
                        "项目或子任务已取消，本次结果不再登记"
                    )
                if task["status"] != TASK_WORKING:
                    raise StateConflictError(
                        "子任务已不处于制作中，本次结果不再登记"
                    )
                if int(attempt["source_revision"] or 1) != int(
                    task["source_revision"] or 1
                ):
                    raise StateConflictError(
                        "生成期间素材已变更，本次结果已失效"
                    )
                cursor.execute(
                    """
                    UPDATE generation_attempts
                    SET status = %s, generated_image_count = %s,
                        request_id = %s, remote_task_id = %s,
                        raw_response_json = %s, input_tokens = %s,
                        output_tokens = %s, provider_id = %s,
                        provider_code = %s, provider_name = %s,
                        provider_model = %s, finished_at = %s
                    WHERE id = %s
                    """,
                    (
                        ATTEMPT_SUCCEEDED,
                        len(results),
                        request_id,
                        remote_task_id,
                        json.dumps(raw_response, ensure_ascii=False, sort_keys=True),
                        input_tokens,
                        output_tokens,
                        provider_id,
                        provider_code,
                        provider_name,
                        provider_model,
                        now,
                        attempt_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE generation_results gr
                    JOIN generation_attempts ga ON ga.id = gr.attempt_id
                    SET gr.is_final = 0,
                        gr.expire_at = CASE
                            WHEN gr.expire_at IS NULL THEN
                                DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 30 DAY)
                            ELSE gr.expire_at
                        END
                    WHERE ga.task_id = %s
                    """,
                    (int(attempt["task_id"]),),
                )
                for result in results:
                    cursor.execute(
                        """
                        INSERT INTO generation_results(
                            attempt_id, result_index, relative_path,
                            thumbnail_relative_path, content_hash, mime,
                            width, height, is_final, source_type,
                            source_revision, created_at, expire_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, 1, %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            attempt_id,
                            result.result_index,
                            result.relative_path,
                            result.thumbnail_relative_path,
                            result.content_hash,
                            result.mime,
                            result.width,
                            result.height,
                            source_type,
                            int(attempt.get("source_revision") or 1),
                            now,
                            None,
                        ),
                    )
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s, final_quantity = %s,
                        submitted_at = %s, approved_at = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        TASK_APPROVED,
                        len(results),
                        now,
                        now,
                        now,
                        int(attempt["task_id"]),
                    ),
                )
                self._update_project_status(
                    cursor,
                    int(attempt["project_id"]),
                )
                self._event(
                    cursor,
                    int(attempt["project_id"]),
                    int(attempt["task_id"]),
                    actor.id,
                    "generation_succeeded",
                    TASK_WORKING,
                    TASK_APPROVED,
                    {
                        "attempt_id": attempt_id,
                        "image_count": len(results),
                    },
                )

    def fail_generation_attempt(
        self,
        attempt_id: int,
        actor: AuthUser,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                attempt = self._attempt_row_for_update(cursor, attempt_id)
                if int(attempt["operator_id"]) != actor.id and actor.role != ROLE_ADMIN:
                    raise PermissionDeniedError("不能修改其他人的生成记录")
                if attempt["status"] not in (ATTEMPT_RUNNING, ATTEMPT_QUEUED):
                    return
                task = self._task_row_for_update(
                    cursor,
                    int(attempt["task_id"]),
                )
                cursor.execute(
                    """
                    UPDATE generation_attempts
                    SET status = %s, error_code = %s, error_message = %s,
                        finished_at = %s
                    WHERE id = %s
                    """,
                    (
                        ATTEMPT_FAILED,
                        error_code[:100],
                        error_message,
                        now,
                        attempt_id,
                    ),
                )
                if (
                    task["status"] == TASK_CANCELLED
                    or task["project_status"] == PROJECT_CANCELLED
                ):
                    return
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = CASE
                            WHEN status = %s THEN %s
                            ELSE status
                        END,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        TASK_WORKING,
                        TASK_CLAIMED,
                        now,
                        int(attempt["task_id"]),
                    ),
                )
                self._update_project_status(
                    cursor,
                    int(attempt["project_id"]),
                )
                self._event(
                    cursor,
                    int(attempt["project_id"]),
                    int(attempt["task_id"]),
                    actor.id,
                    "generation_failed",
                    TASK_WORKING,
                    TASK_WORKING,
                    {
                        "attempt_id": attempt_id,
                        "error_code": error_code,
                    },
                )

    def submit_for_acceptance(
        self,
        task_id: int,
        actor: AuthUser,
        *,
        final_result_ids: Sequence[int],
        final_quantity: int,
    ) -> None:
        clean_ids = tuple(dict.fromkeys(int(value) for value in final_result_ids))
        if not clean_ids:
            raise ValueError("请至少选择一张最终图片")
        if final_quantity != len(clean_ids):
            raise ValueError("最终图片数量必须与选中的图片数一致")
        if final_quantity < 1:
            raise ValueError("最终图片数量至少为 1")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                if actor.role not in (ROLE_AI, ROLE_ADMIN):
                    raise PermissionDeniedError("只有 AI 制作人员可以提交自审结果")
                if int(task["assignee_id"] or 0) != actor.id:
                    raise PermissionDeniedError("只能提交自己领取的任务")
                if task["status"] not in (TASK_CLAIMED, TASK_WORKING, TASK_RETURNED):
                    raise StateConflictError("当前任务状态不能提交验收")
                placeholders = ", ".join(["%s"] * len(clean_ids))
                cursor.execute(
                    f"""
                    SELECT gr.id, gr.source_revision
                    FROM generation_results gr
                    JOIN generation_attempts ga ON ga.id = gr.attempt_id
                    WHERE ga.task_id = %s AND ga.status = %s
                      AND gr.id IN ({placeholders})
                    """,
                    (task_id, ATTEMPT_SUCCEEDED, *clean_ids),
                )
                found_rows = cursor.fetchall()
                found_ids = {int(row["id"]) for row in found_rows}
                if found_ids != set(clean_ids):
                    raise ValueError("选择的图片不属于当前任务或生成未成功")
                current_revision = int(task.get("source_revision") or 1)
                if any(
                    int(row.get("source_revision") or 1) != current_revision
                    for row in found_rows
                ):
                    raise StateConflictError(
                        "所选图片生成后素材已变更，请重新生成或上传 PS 成品"
                    )
                cursor.execute(
                    """
                    UPDATE generation_results gr
                    JOIN generation_attempts ga ON ga.id = gr.attempt_id
                    SET gr.is_final = 0,
                        gr.expire_at = CASE
                            WHEN gr.expire_at IS NULL THEN
                                DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 30 DAY)
                            ELSE gr.expire_at
                        END
                    WHERE ga.task_id = %s
                    """,
                    (task_id,),
                )
                cursor.execute(
                    f"""
                    UPDATE generation_results
                    SET is_final = 1, expire_at = NULL
                    WHERE id IN ({placeholders})
                    """,
                    clean_ids,
                )
                previous_status = task["status"]
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s, final_quantity = %s, submitted_at = %s,
                        returned_at = NULL, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        TASK_AWAITING_ACCEPTANCE,
                        final_quantity,
                        now,
                        now,
                        task_id,
                    ),
                )
                self._update_project_status(cursor, int(task["project_id"]))
                self._event(
                    cursor,
                    int(task["project_id"]),
                    task_id,
                    actor.id,
                    "task_submitted_for_acceptance",
                    previous_status,
                    TASK_AWAITING_ACCEPTANCE,
                    {"final_image_count": final_quantity},
                )

    def review_task(
        self,
        task_id: int,
        actor: AuthUser,
        *,
        approved: bool,
        reason: str = "",
    ) -> None:
        reason = reason.strip()
        if not approved and not reason:
            raise ValueError("退回任务时必须填写原因")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                if actor.role != ROLE_ADMIN and int(task["owner_id"]) != actor.id:
                    raise PermissionDeniedError("只能验收自己提交的项目")
                if task["status"] != TASK_AWAITING_ACCEPTANCE:
                    raise StateConflictError("任务尚未提交验收")
                new_status = TASK_APPROVED if approved else TASK_RETURNED
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s,
                        approved_at = %s,
                        returned_at = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        new_status,
                        now if approved else None,
                        None if approved else now,
                        now,
                        task_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO task_reviews(
                        task_id, reviewer_id, decision, reason, created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        task_id,
                        actor.id,
                        "approve" if approved else "return",
                        reason,
                        now,
                    ),
                )
                self._update_project_status(cursor, int(task["project_id"]))
                self._event(
                    cursor,
                    int(task["project_id"]),
                    task_id,
                    actor.id,
                    "task_approved" if approved else "task_returned",
                    TASK_AWAITING_ACCEPTANCE,
                    new_status,
                    {"reason": reason},
                )

    def skip_remaining_subtasks(
        self,
        project_id: int,
        actor: AuthUser,
        *,
        reason: str,
    ) -> int:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能结束剩余子任务")
        reason = reason.strip()
        if not reason:
            raise ValueError("必须填写剩余子任务不制作的原因")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                self._project_row_for_update(cursor, project_id)
                cursor.execute(
                    """
                    SELECT id, status, assignee_id
                    FROM project_tasks
                    WHERE project_id = %s
                    ORDER BY task_no
                    FOR UPDATE
                    """,
                    (project_id,),
                )
                tasks = cursor.fetchall()
                if not tasks:
                    raise ValueError("当前主任务没有子任务")
                completed = [
                    task
                    for task in tasks
                    if task["status"] in (
                        TASK_AWAITING_ACCEPTANCE,
                        TASK_APPROVED,
                    )
                ]
                if not completed:
                    raise StateConflictError(
                        "请先至少提交一个子任务，再结束剩余制作"
                    )
                if actor.role != ROLE_ADMIN and not any(
                    int(task["assignee_id"] or 0) == actor.id
                    for task in completed
                ):
                    raise PermissionDeniedError(
                        "只能结束自己已领取主任务下的剩余子任务"
                    )
                active = [
                    task
                    for task in tasks
                    if task["status"]
                    in (
                        TASK_AVAILABLE,
                        TASK_CLAIMED,
                        TASK_WORKING,
                        TASK_RETURNED,
                    )
                ]
                if not active:
                    raise StateConflictError("没有需要结束的剩余子任务")
                if actor.role != ROLE_ADMIN:
                    occupied = [
                        task
                        for task in active
                        if task["assignee_id"] is not None
                        and int(task["assignee_id"]) != actor.id
                    ]
                    if occupied:
                        raise StateConflictError(
                            "部分剩余子任务已被其他制作人员领取，请联系管理员处理"
                        )
                for task in active:
                    cursor.execute(
                        """
                        UPDATE project_tasks
                        SET status = %s, updated_at = %s
                        WHERE id = %s
                        """,
                        (TASK_SKIPPED, now, task["id"]),
                    )
                    self._event(
                        cursor,
                        project_id,
                        int(task["id"]),
                        actor.id,
                        "subtask_skipped",
                        task["status"],
                        TASK_SKIPPED,
                        {"reason": reason},
                    )
                self._update_project_status(cursor, project_id)
                return len(active)

    def reassign_task(self, task_id: int, actor: AuthUser, target_user_id: int) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以改派任务")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                cursor.execute(
                    "SELECT role, is_active FROM users "
                    "WHERE id = %s AND deleted_at IS NULL",
                    (target_user_id,),
                )
                target = cursor.fetchone()
                if target is None or target["role"] not in (ROLE_AI, ROLE_ADMIN):
                    raise ValueError("目标账号不是 AI 制作人员")
                if not bool(target["is_active"]):
                    raise ValueError("目标账号已停用")
                if task["status"] == TASK_CANCELLED:
                    raise StateConflictError("已取消任务不能改派")
                previous_status = task["status"]
                new_status = TASK_RETURNED
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET assignee_id = %s, status = %s, returned_at = %s,
                        approved_at = NULL, updated_at = %s
                    WHERE id = %s
                    """,
                    (target_user_id, new_status, now, now, task_id),
                )
                self._update_project_status(cursor, int(task["project_id"]))
                self._event(
                    cursor,
                    int(task["project_id"]),
                    task_id,
                    actor.id,
                    "task_reassigned",
                    previous_status,
                    new_status,
                    {"target_user_id": target_user_id},
                )

    def cancel_project(self, project_id: int, actor: AuthUser) -> None:
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                project = self._project_row_for_update(cursor, project_id)
                if actor.role != ROLE_ADMIN and int(project["owner_id"]) != actor.id:
                    raise PermissionDeniedError("只能取消自己提交的项目")
                if project["status"] in (PROJECT_COMPLETED, PROJECT_CANCELLED):
                    raise StateConflictError("项目已经结束")
                if actor.role != ROLE_ADMIN:
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS count FROM project_tasks
                        WHERE project_id = %s AND status <> %s
                        """,
                        (project_id, TASK_AVAILABLE),
                    )
                    if int(cursor.fetchone()["count"]):
                        raise StateConflictError("任务已被领取，请联系管理员取消项目")
                cursor.execute(
                    """
                    UPDATE projects
                    SET status = %s, cancelled_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (PROJECT_CANCELLED, now, now, project_id),
                )
                cursor.execute(
                    "UPDATE project_tasks SET status = %s, updated_at = %s "
                    "WHERE project_id = %s",
                    (TASK_CANCELLED, now, project_id),
                )
                cursor.execute(
                    """
                    UPDATE generation_attempts ga
                    JOIN project_tasks t ON t.id = ga.task_id
                    SET ga.status = %s,
                        ga.error_code = 'project_cancelled',
                        ga.error_message = '所属项目已取消',
                        ga.finished_at = %s
                    WHERE t.project_id = %s
                      AND ga.status IN (%s, %s)
                    """,
                    (
                        ATTEMPT_CANCELLED,
                        now,
                        project_id,
                        ATTEMPT_QUEUED,
                        ATTEMPT_RUNNING,
                    ),
                )
                self._event(
                    cursor,
                    project_id,
                    None,
                    actor.id,
                    "project_cancelled",
                    project["status"],
                    PROJECT_CANCELLED,
                    {},
                )

    def reopen_project(self, project_id: int, actor: AuthUser, reason: str) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以重开项目")
        if not reason.strip():
            raise ValueError("重开项目必须填写原因")
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                project = self._project_row_for_update(cursor, project_id)
                cursor.execute(
                    """
                    UPDATE projects
                    SET status = %s, completed_at = NULL, cancelled_at = NULL,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (PROJECT_IN_PROGRESS, now, project_id),
                )
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s, approved_at = NULL, returned_at = %s,
                        updated_at = %s
                    WHERE project_id = %s AND status IN (%s, %s)
                    """,
                    (
                        TASK_RETURNED,
                        now,
                        now,
                        project_id,
                        TASK_APPROVED,
                        TASK_AWAITING_ACCEPTANCE,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET status = %s, assignee_id = NULL, approved_at = NULL,
                        returned_at = NULL, updated_at = %s
                    WHERE project_id = %s AND status = %s
                    """,
                    (
                        TASK_AVAILABLE,
                        now,
                        project_id,
                        TASK_CANCELLED,
                    ),
                )
                self._event(
                    cursor,
                    project_id,
                    None,
                    actor.id,
                    "project_reopened",
                    project["status"],
                    PROJECT_IN_PROGRESS,
                    {"reason": reason.strip()},
                )

    def list_attempts(self, task_id: int) -> list[GenerationAttempt]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT ga.*, u.display_name AS operator_name
                    FROM generation_attempts ga
                    JOIN users u ON u.id = ga.operator_id
                    WHERE ga.task_id = %s
                    ORDER BY ga.attempt_no DESC
                    """,
                    (task_id,),
                )
                return [_generation_attempt(row) for row in cursor.fetchall()]

    def generation_attempt_status(self, attempt_id: int) -> str:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status FROM generation_attempts WHERE id = %s",
                    (attempt_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise ValueError("生成轮次不存在")
        return str(row["status"])

    def list_project_attempts(
        self,
        project_id: int,
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT ga.*, u.display_name AS operator_name,
                           t.task_code, t.project_id,
                           p.project_code, p.product_code,
                           p.project_name AS access_project_name,
                           COUNT(gr.id) AS result_count
                    FROM generation_attempts ga
                    JOIN project_tasks t ON t.id = ga.task_id
                    JOIN projects p ON p.id = t.project_id
                    JOIN users u ON u.id = ga.operator_id
                    LEFT JOIN generation_results gr ON gr.attempt_id = ga.id
                    WHERE t.project_id = %s
                    GROUP BY ga.id, u.display_name, t.task_code,
                             t.project_id, p.project_code, p.product_code,
                             p.project_name
                    ORDER BY ga.created_at DESC, ga.id DESC
                    """,
                    (project_id,),
                )
                return [dict(row) for row in cursor.fetchall()]

    def list_project_attempt_history(
        self,
        actor: AuthUser,
        *,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能查看项目生成历史")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT ga.*, u.display_name AS operator_name,
                           t.task_code, t.project_id,
                           p.project_code, p.product_code,
                           p.name AS project_name,
                           p.project_name AS access_project_name,
                           COUNT(gr.id) AS result_count,
                           COALESCE(SUM(gr.source_type = 'manual_ps'), 0)
                               AS manual_result_count
                    FROM generation_attempts ga
                    JOIN project_tasks t ON t.id = ga.task_id
                    JOIN projects p ON p.id = t.project_id
                    JOIN users u ON u.id = ga.operator_id
                    LEFT JOIN generation_results gr ON gr.attempt_id = ga.id
                    GROUP BY ga.id, u.display_name, t.task_code,
                             t.project_id, p.project_code, p.product_code,
                             p.name, p.project_name
                    ORDER BY ga.created_at DESC, ga.id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [dict(row) for row in cursor.fetchall()]

    def list_project_attempt_history_page(
        self,
        actor: AuthUser,
        query: HistoryQuery,
        page: PageRequest,
    ) -> PageResult[dict[str, Any]]:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise PermissionDeniedError("当前角色不能查看项目生成历史")
        conditions = ["1 = 1"]
        params: list[Any] = []
        if query.project_name:
            conditions.append("p.project_name = %s")
            params.append(query.project_name)
        if query.status:
            if query.status == "manual_ps":
                conditions.extend(
                    (
                        "ga.status = %s",
                        """
                        EXISTS (
                            SELECT 1 FROM generation_results manual_result
                            WHERE manual_result.attempt_id = ga.id
                              AND manual_result.source_type = 'manual_ps'
                        )
                        """,
                    )
                )
                params.append(ATTEMPT_SUCCEEDED)
            else:
                conditions.append("ga.status = %s")
                params.append(query.status)
        if query.start_date is not None:
            conditions.append("ga.created_at >= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(query.start_date, time.min)
                )
            )
        if query.end_date is not None:
            conditions.append("ga.created_at <= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(query.end_date, time.max)
                )
            )
        keyword = query.keyword.strip()
        if keyword:
            pattern = f"%{keyword}%"
            conditions.append(
                "("
                "ga.prompt_text LIKE %s OR ga.model LIKE %s OR "
                "ga.provider_name LIKE %s OR t.task_code LIKE %s OR "
                "p.project_code LIKE %s OR p.product_code LIKE %s OR "
                "p.name LIKE %s OR p.project_name LIKE %s OR "
                "u.display_name LIKE %s"
                ")"
            )
            params.extend((pattern,) * 9)
        where_sql = " AND ".join(conditions)
        base = f"""
            SELECT ga.*, u.display_name AS operator_name,
                   t.task_code, t.project_id, t.assignee_id,
                   p.project_code, p.product_code,
                   p.name AS project_name,
                   p.project_name AS access_project_name,
                   COUNT(gr.id) AS result_count,
                   COALESCE(SUM(gr.source_type = 'manual_ps'), 0)
                       AS manual_result_count
            FROM generation_attempts ga
            JOIN project_tasks t ON t.id = ga.task_id
            JOIN projects p ON p.id = t.project_id
            JOIN users u ON u.id = ga.operator_id
            LEFT JOIN generation_results gr ON gr.attempt_id = ga.id
            WHERE {where_sql}
            GROUP BY ga.id, u.display_name, t.task_code,
                     t.project_id, p.project_code, p.product_code,
                     p.name, p.project_name, t.assignee_id
        """
        sort_fields = {
            "created_at": "ga.created_at",
            "status": "ga.status",
            "main_code": "p.project_code",
            "secondary_code": "t.task_code",
            "product_code": "p.product_code",
            "operator_name": "u.display_name",
            "model": "ga.model",
            "kind": "'project'",
            "result_count": "result_count",
        }
        sort_field = sort_fields.get(page.sort_field, "ga.created_at")
        direction = "ASC" if page.sort_direction == "asc" else "DESC"
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM generation_attempts ga
                    JOIN project_tasks t ON t.id = ga.task_id
                    JOIN projects p ON p.id = t.project_id
                    JOIN users u ON u.id = ga.operator_id
                    WHERE
                    """
                    + where_sql,
                    tuple(params),
                )
                total = int(cursor.fetchone()["count"])
                cursor.execute(
                    base
                    + f"""
                    ORDER BY {sort_field} {direction}, ga.id {direction}
                    LIMIT %s OFFSET %s
                    """,
                    (*params, page.page_size, page.offset),
                )
                rows = cursor.fetchall()
        return PageResult(
            [dict(row) for row in rows],
            total,
            page.page,
            page.page_size,
        )

    def list_attempt_results(self, attempt_id: int) -> list[GenerationResult]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM generation_results
                    WHERE attempt_id = %s
                    ORDER BY result_index
                    """,
                    (attempt_id,),
                )
                return [_generation_result(row) for row in cursor.fetchall()]

    def recover_stale_generation_attempts(self, hours: int = 24) -> int:
        cutoff = utc_now() - timedelta(hours=hours)
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, task_id FROM generation_attempts
                    WHERE status = %s AND started_at < %s
                    FOR UPDATE
                    """,
                    (ATTEMPT_RUNNING, cutoff),
                )
                stale = cursor.fetchall()
                if not stale:
                    return 0
                attempt_ids = tuple(int(row["id"]) for row in stale)
                attempt_placeholders = ", ".join(["%s"] * len(attempt_ids))
                cursor.execute(
                    f"""
                    UPDATE generation_attempts
                    SET status = %s,
                        error_code = 'stale_running',
                        error_message = '客户端中断，生成任务已自动结束',
                        finished_at = %s
                    WHERE id IN ({attempt_placeholders})
                    """,
                    (ATTEMPT_FAILED, now, *attempt_ids),
                )
                task_ids = tuple({int(row["task_id"]) for row in stale})
                task_placeholders = ", ".join(["%s"] * len(task_ids))
                cursor.execute(
                    f"""
                    SELECT id, project_id FROM project_tasks
                    WHERE id IN ({task_placeholders}) AND status = %s
                    """,
                    (*task_ids, TASK_WORKING),
                )
                stale_tasks = cursor.fetchall()
                cursor.execute(
                    f"""
                    UPDATE project_tasks
                    SET status = %s, updated_at = %s
                    WHERE id IN ({task_placeholders}) AND status = %s
                    """,
                    (TASK_CLAIMED, now, *task_ids, TASK_WORKING),
                )
                for project_id in {
                    int(row["project_id"]) for row in stale_tasks
                }:
                    self._update_project_status(cursor, project_id)
                return len(stale)

    def list_results(self, task_id: int) -> list[GenerationResult]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT gr.*
                    FROM generation_results gr
                    JOIN generation_attempts ga ON ga.id = gr.attempt_id
                    WHERE ga.task_id = %s
                    ORDER BY ga.attempt_no DESC, gr.result_index
                    """,
                    (task_id,),
                )
                return [_generation_result(row) for row in cursor.fetchall()]

    def expired_candidate_results(self, task_id: int) -> list[GenerationResult]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT gr.*
                    FROM generation_results gr
                    JOIN generation_attempts ga ON ga.id = gr.attempt_id
                    WHERE ga.task_id = %s AND gr.is_final = 0
                      AND gr.expire_at IS NOT NULL AND gr.expire_at <= %s
                    """,
                    (task_id, utc_now()),
                )
                return [_generation_result(row) for row in cursor.fetchall()]

    def expired_candidate_results_for_tasks(
        self,
        task_ids: Sequence[int],
    ) -> list[tuple[int, GenerationResult]]:
        clean_ids = tuple(dict.fromkeys(int(value) for value in task_ids))
        if not clean_ids:
            return []
        placeholders = ", ".join(["%s"] * len(clean_ids))
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT ga.task_id AS task_id, gr.*
                    FROM generation_results gr
                    JOIN generation_attempts ga ON ga.id = gr.attempt_id
                    WHERE ga.task_id IN ({placeholders})
                      AND gr.is_final = 0
                      AND gr.expire_at IS NOT NULL AND gr.expire_at <= %s
                    """,
                    (*clean_ids, utc_now()),
                )
                return [
                    (int(row["task_id"]), _generation_result(row))
                    for row in cursor.fetchall()
                ]

    def task_project_roots(
        self,
        task_ids: Sequence[int],
    ) -> dict[int, str]:
        clean_ids = tuple(dict.fromkeys(int(value) for value in task_ids))
        if not clean_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(clean_ids))
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT t.id AS id, p.project_relative_root AS root
                    FROM project_tasks t
                    JOIN projects p ON p.id = t.project_id
                    WHERE t.id IN ({placeholders})
                    """,
                    clean_ids,
                )
                return {
                    int(row["id"]): str(row["root"])
                    for row in cursor.fetchall()
                }

    def result_paths_in_use(
        self,
        relative_paths: Sequence[str],
    ) -> set[str]:
        clean_paths = tuple(
            dict.fromkeys(path for path in relative_paths if path)
        )
        if not clean_paths:
            return set()
        placeholders = ", ".join(["%s"] * len(clean_paths))
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT relative_path AS path FROM generation_results
                    WHERE relative_path IN ({placeholders})
                    UNION
                    SELECT thumbnail_relative_path AS path
                    FROM generation_results
                    WHERE thumbnail_relative_path IN ({placeholders})
                    """,
                    (*clean_paths, *clean_paths),
                )
                return {str(row["path"]) for row in cursor.fetchall()}

    def result_path_reference_count(
        self,
        relative_path: str,
        excluding_result_id: int,
    ) -> int:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM generation_results
                    WHERE id <> %s
                      AND (
                          relative_path = %s
                          OR thumbnail_relative_path = %s
                      )
                    """,
                    (
                        excluding_result_id,
                        relative_path,
                        relative_path,
                    ),
                )
                return int(cursor.fetchone()["count"])

    def delete_generation_results(
        self,
        result_ids: Sequence[int],
    ) -> list[int]:
        clean_ids = tuple(dict.fromkeys(int(value) for value in result_ids))
        if not clean_ids:
            return []
        placeholders = ", ".join(["%s"] * len(clean_ids))
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM generation_results WHERE is_final = 0 "
                    f"AND id IN ({placeholders})",
                    clean_ids,
                )
                cursor.execute(
                    f"SELECT id FROM generation_results "
                    f"WHERE id IN ({placeholders})",
                    clean_ids,
                )
                remaining = {int(row["id"]) for row in cursor.fetchall()}
                return [
                    value for value in clean_ids if value not in remaining
                ]

    def list_templates(self, actor: AuthUser) -> list[dict[str, Any]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM prompt_templates
                    WHERE scope = 'shared' OR (scope = 'personal' AND owner_id = %s)
                    ORDER BY scope DESC, name
                    """,
                    (actor.id,),
                )
                return [dict(row) for row in cursor.fetchall()]

    def save_template(
        self,
        actor: AuthUser,
        *,
        scope: str,
        name: str,
        prompt: str,
        template_id: int | None = None,
    ) -> int:
        name = name.strip()
        prompt = prompt.strip()
        if scope not in ("shared", "personal"):
            raise ValueError("不支持的模板范围")
        if scope == "shared" and actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以维护共享模板")
        if not name or not prompt:
            raise ValueError("模板名称和提示词不能为空")
        owner_id = None if scope == "shared" else actor.id
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, owner_id FROM prompt_templates
                    WHERE name = %s AND scope = %s
                      AND ((owner_id = %s) OR (owner_id IS NULL AND %s IS NULL))
                    LIMIT 1
                    """,
                    (name, scope, owner_id, owner_id),
                )
                duplicate = cursor.fetchone()
                if duplicate is not None and (
                    template_id is None or int(duplicate["id"]) != int(template_id)
                ):
                    raise StateConflictError("同名模板已经存在")
                if template_id is None:
                    cursor.execute(
                        """
                        INSERT INTO prompt_templates(
                            scope, owner_id, name, prompt, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (scope, owner_id, name, prompt, now, now),
                    )
                    return int(cursor.lastrowid)
                cursor.execute(
                    """
                    UPDATE prompt_templates
                    SET name = %s, prompt = %s, updated_at = %s
                    WHERE id = %s
                      AND scope = %s
                      AND ((owner_id = %s) OR (owner_id IS NULL AND %s IS NULL))
                    """,
                    (name, prompt, now, template_id, scope, owner_id, owner_id),
                )
                if cursor.rowcount != 1:
                    raise PermissionDeniedError("不能修改该模板")
                return int(template_id)

    def delete_template(self, template_id: int, actor: AuthUser) -> None:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT scope, owner_id FROM prompt_templates WHERE id = %s",
                    (template_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return
                if row["scope"] == "shared" and actor.role != ROLE_ADMIN:
                    raise PermissionDeniedError("只有管理员可以删除共享模板")
                if row["scope"] == "personal" and int(row["owner_id"] or 0) != actor.id:
                    raise PermissionDeniedError("不能删除其他人的个人模板")
                cursor.execute(
                    "DELETE FROM prompt_templates WHERE id = %s",
                    (template_id,),
                )

    def list_model_prices(self) -> dict[str, Decimal]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT mp.model, mp.unit_price
                    FROM model_prices mp
                    JOIN (
                        SELECT model, MAX(id) AS id
                        FROM model_prices
                        GROUP BY model
                    ) latest ON latest.id = mp.id
                    """
                )
                return {
                    row["model"]: Decimal(str(row["unit_price"]))
                    for row in cursor.fetchall()
                }

    def current_model_price(self, model: str) -> Decimal:
        return self.list_model_prices().get(model, Decimal("0"))

    def set_model_price(
        self,
        actor: AuthUser,
        model: str,
        unit_price: Decimal,
        *,
        currency: str = "CNY",
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以维护模型价格")
        if model not in MODEL_IDS:
            raise ValueError("不支持的模型")
        if unit_price < 0:
            raise ValueError("模型单价不能为负数")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO model_prices(
                        model, unit_price, currency, effective_from, created_by
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (model, unit_price, currency, utc_now(), actor.id),
                )

    def get_shared_setting(self, key: str) -> Any:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT value_json FROM shared_settings WHERE setting_key = %s",
                    (key,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return json.loads(row["value_json"])

    def set_shared_setting(
        self,
        actor: AuthUser,
        key: str,
        value: Any,
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以修改共享设置")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO shared_settings(
                        setting_key, value_json, updated_by, updated_at
                    ) VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        value_json = VALUES(value_json),
                        updated_by = VALUES(updated_by),
                        updated_at = VALUES(updated_at)
                    """,
                    (
                        key,
                        json.dumps(value, ensure_ascii=False),
                        actor.id,
                        utc_now(),
                    ),
                )

    def report(
        self,
        actor: AuthUser,
        report_filter: ReportFilter,
    ) -> ReportData:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以查看经营报告")
        if report_filter.date_basis not in (
            REPORT_DATE_COMPLETED,
            REPORT_DATE_CREATED,
        ):
            raise ValueError("不支持的统计口径")
        conditions = ["t.status <> %s"]
        params: list[Any] = [TASK_EXPANDED]
        date_column = (
            "t.approved_at"
            if report_filter.date_basis == REPORT_DATE_COMPLETED
            else "p.created_at"
        )
        if report_filter.start_date:
            conditions.append(f"{date_column} >= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(report_filter.start_date, time.min)
                )
            )
        if report_filter.end_date:
            conditions.append(f"{date_column} < %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(
                        report_filter.end_date + timedelta(days=1),
                        time.min,
                    )
                )
            )
        if report_filter.project_scope:
            conditions.append("p.project_scope = %s")
            params.append(report_filter.project_scope)
        if report_filter.project_name:
            conditions.append("p.project_name = %s")
            params.append(report_filter.project_name)
        if report_filter.project_id:
            conditions.append("p.id = %s")
            params.append(report_filter.project_id)
        if report_filter.assignee_id:
            conditions.append("t.assignee_id = %s")
            params.append(report_filter.assignee_id)
        if report_filter.model:
            conditions.append("t.model = %s")
            params.append(report_filter.model)
        base_where_sql = " AND ".join(conditions)
        report_conditions = [
            *conditions,
            "(t.status <> %s OR COALESCE(attempts.attempt_count, 0) > 0)",
        ]
        where_sql = " AND ".join(report_conditions)
        report_params = [*params, TASK_APPROVED]
        attempt_params = (
            ATTEMPT_SUCCEEDED,
            ATTEMPT_FAILED,
            ATTEMPT_SUCCEEDED,
            ATTEMPT_SUCCEEDED,
            ATTEMPT_SUCCEEDED,
            ATTEMPT_SUCCEEDED,
            ATTEMPT_FAILED,
        )
        query = f"""
            SELECT
                p.id AS project_id,
                p.project_code,
                p.project_scope,
                p.project_name,
                p.product_code,
                p.status AS project_status,
                p.priority,
                p.created_at AS project_created_at,
                t.id AS task_id,
                t.task_code,
                t.status AS task_status,
                t.assignee_id,
                owner.display_name AS owner_name,
                COALESCE(assignee.display_name, '') AS assignee_name,
                t.model,
                garment.original_name AS garment_name,
                model_asset.original_name AS model_name,
                t.final_quantity,
                t.claimed_at,
                t.work_started_at,
                t.submitted_at,
                t.approved_at,
                COALESCE(attempts.generated_image_count, 0)
                    AS generated_image_count,
                COALESCE(attempts.attempt_count, 0) AS attempt_count,
                COALESCE(attempts.successful_attempt_count, 0)
                    AS successful_attempt_count,
                COALESCE(attempts.failed_attempt_count, 0)
                    AS failed_attempt_count,
                COALESCE(attempts.provider_name, '') AS provider_name,
                COALESCE(attempts.generation_cost, 0) AS generation_cost
            FROM project_tasks t
            JOIN projects p ON p.id = t.project_id
            JOIN users owner ON owner.id = p.owner_id
            LEFT JOIN users assignee ON assignee.id = t.assignee_id
            JOIN project_assets garment ON garment.id = t.garment_asset_id
            JOIN project_assets model_asset ON model_asset.id = t.model_asset_id
            LEFT JOIN (
                SELECT
                    task_id,
                    COUNT(*) AS attempt_count,
                    SUM(status = %s) AS successful_attempt_count,
                    SUM(status = %s) AS failed_attempt_count,
                    SUM(
                        CASE WHEN status = %s
                            THEN generated_image_count ELSE 0 END
                    ) AS generated_image_count,
                    GROUP_CONCAT(
                        DISTINCT CASE
                            WHEN status = %s AND provider_name <> ''
                            THEN provider_name
                            ELSE NULL
                        END
                        SEPARATOR '、'
                    ) AS provider_name,
                    SUM(
                        CASE WHEN status = %s
                            THEN generated_image_count * unit_price
                            ELSE 0 END
                    ) AS generation_cost
                FROM generation_attempts
                WHERE status IN (%s, %s)
                  AND model <> 'manual_ps'
                GROUP BY task_id
            ) attempts ON attempts.task_id = t.id
            WHERE {where_sql}
            ORDER BY p.created_at, p.id, t.task_no
        """
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (*attempt_params, *report_params),
                )
                rows = [
                    ReportTaskRow(
                        project_id=int(row["project_id"]),
                        project_code=row["project_code"],
                        project_scope=row["project_scope"],
                        project_name=row["project_name"],
                        product_code=row["product_code"],
                        project_status=row["project_status"],
                        priority=row["priority"],
                        task_id=int(row["task_id"]),
                        task_code=row["task_code"],
                        task_status=row["task_status"],
                        assignee_id=(
                            int(row["assignee_id"])
                            if row["assignee_id"] is not None
                            else None
                        ),
                        owner_name=row["owner_name"],
                        assignee_name=row["assignee_name"],
                        model=row["model"],
                        garment_name=row["garment_name"],
                        model_name=row["model_name"],
                        final_quantity=int(row["final_quantity"] or 0),
                        generated_image_count=int(
                            row["generated_image_count"] or 0
                        ),
                        attempt_count=int(row["attempt_count"] or 0),
                        successful_attempt_count=int(
                            row["successful_attempt_count"] or 0
                        ),
                        failed_attempt_count=int(
                            row["failed_attempt_count"] or 0
                        ),
                        generation_cost=Decimal(
                            str(row["generation_cost"] or 0)
                        ),
                        project_created_at=row["project_created_at"],
                        claimed_at=row["claimed_at"],
                        work_started_at=row["work_started_at"],
                        submitted_at=row["submitted_at"],
                        approved_at=row["approved_at"],
                        provider_name=row["provider_name"] or "",
                    )
                    for row in cursor.fetchall()
                ]
                provider_query = f"""
                    SELECT
                        providers.provider_name,
                        providers.model,
                        COUNT(DISTINCT p.id) AS project_count,
                        COUNT(DISTINCT t.id) AS task_count,
                        COUNT(
                            DISTINCT CASE WHEN t.status = %s
                                THEN t.id END
                        ) AS approved_task_count,
                        SUM(
                            CASE WHEN t.status = %s
                                THEN t.final_quantity ELSE 0 END
                        ) AS final_image_count,
                        SUM(providers.successful_attempt_count)
                            AS successful_attempt_count,
                        SUM(providers.failed_attempt_count)
                            AS failed_attempt_count,
                        SUM(providers.generated_image_count)
                            AS generated_image_count,
                        SUM(providers.generation_cost) AS generation_cost
                    FROM (
                        SELECT
                            ga.task_id,
                            COALESCE(
                                NULLIF(ga.provider_name, ''),
                                '历史/未记录'
                            ) AS provider_name,
                            ga.model,
                            SUM(ga.status = %s) AS successful_attempt_count,
                            SUM(ga.status = %s) AS failed_attempt_count,
                            SUM(
                                CASE WHEN ga.status = %s
                                    THEN ga.generated_image_count
                                    ELSE 0 END
                            ) AS generated_image_count,
                            SUM(
                                CASE WHEN ga.status = %s
                                    THEN ga.generated_image_count
                                        * ga.unit_price
                                    ELSE 0 END
                            ) AS generation_cost
                        FROM generation_attempts ga
                        JOIN project_tasks t ON t.id = ga.task_id
                        JOIN projects p ON p.id = t.project_id
                        WHERE {base_where_sql}
                          AND ga.status IN (%s, %s)
                          AND ga.model <> 'manual_ps'
                        GROUP BY ga.task_id, provider_name, ga.model
                    ) providers
                    JOIN project_tasks t ON t.id = providers.task_id
                    JOIN projects p ON p.id = t.project_id
                    GROUP BY providers.provider_name, providers.model
                    ORDER BY generated_image_count DESC,
                             providers.provider_name
                """
                cursor.execute(
                    provider_query,
                    (
                        TASK_APPROVED,
                        TASK_APPROVED,
                        ATTEMPT_SUCCEEDED,
                        ATTEMPT_FAILED,
                        ATTEMPT_SUCCEEDED,
                        ATTEMPT_SUCCEEDED,
                        *params,
                        ATTEMPT_SUCCEEDED,
                        ATTEMPT_FAILED,
                    ),
                )
                provider_groups = []
                for row in cursor.fetchall():
                    attempts = int(row["successful_attempt_count"] or 0) + int(
                        row["failed_attempt_count"] or 0
                    )
                    provider_groups.append(
                        GroupMetrics(
                            key=f"{row['provider_name']}:{row['model']}",
                            label=f"{row['provider_name']} / {row['model']}",
                            project_count=int(row["project_count"] or 0),
                            task_count=int(row["task_count"] or 0),
                            approved_task_count=int(
                                row["approved_task_count"] or 0
                            ),
                            final_image_count=int(
                                row["final_image_count"] or 0
                            ),
                            generated_image_count=int(
                                row["generated_image_count"] or 0
                            ),
                            attempt_count=attempts,
                            failed_attempt_count=int(
                                row["failed_attempt_count"] or 0
                            ),
                            generation_success_rate=(
                                int(row["successful_attempt_count"] or 0)
                                / attempts
                                if attempts
                                else 0.0
                            ),
                            average_generation_attempts=(
                                attempts / int(row["task_count"])
                                if int(row["task_count"] or 0)
                                else 0.0
                            ),
                            total_cost=Decimal(
                                str(row["generation_cost"] or 0)
                            ),
                            average_production_seconds=0.0,
                        )
                    )
        return aggregate_report(
            rows,
            date_basis=report_filter.date_basis,
            by_model=tuple(provider_groups),
        )

    def list_report_project_options(
        self,
        actor: AuthUser,
    ) -> list[tuple[str, str]]:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以查看经营报告")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT project_scope, project_name
                    FROM projects
                    WHERE project_name <> ''
                    ORDER BY project_scope, project_name
                    """
                )
                return [
                    (row["project_scope"], row["project_name"])
                    for row in cursor.fetchall()
                ]

    @staticmethod
    def _validate_draft(draft: ProjectDraft) -> None:
        if draft.project_code and len(draft.project_code.strip()) > 128:
            raise ValueError("任务编码不能超过 128 个字符")
        product_code = (
            draft.product_code.strip()
            or draft.name.strip()
            or Path(draft.project_relative_root).name
        )
        if not product_code:
            raise ValueError("货号不能为空")
        if not draft.project_relative_root.strip():
            raise ValueError("任务共享目录不能为空")
        if not draft.assets:
            raise ValueError("项目至少需要服装图和模特图")
        if not draft.pairs:
            raise ValueError("项目至少需要一个制作任务")
        garment_count = sum(asset.role == "garment" for asset in draft.assets)
        model_count = sum(asset.role == "model" for asset in draft.assets)
        if not garment_count or not model_count:
            raise ValueError("项目必须同时包含服装图和模特图")
        seen: set[PairingInput] = set()
        garments_by_model: dict[int, set[int]] = {}
        for pair in draft.pairs:
            if not 0 <= pair.garment_index < garment_count:
                raise ValueError("配对引用了不存在的服装图")
            if not 0 <= pair.model_index < model_count:
                raise ValueError("配对引用了不存在的模特图")
            if pair in seen:
                raise ValueError("项目包含重复配对")
            seen.add(pair)
            garments_by_model.setdefault(pair.model_index, set()).add(
                pair.garment_index
            )
        for model_index, garment_indexes in garments_by_model.items():
            if len(garment_indexes) > 13:
                raise ValueError(
                    f"模特图第 {model_index + 1} 张最多关联 13 张衣服图"
                )

    @staticmethod
    def _next_task_sequence(
        cursor,
        owner_id: int,
        sequence_date: date,
    ) -> int:
        cursor.execute(
            """
            INSERT INTO task_code_sequences(owner_id, sequence_date, current_value)
            VALUES (%s, %s, 1)
            ON DUPLICATE KEY UPDATE current_value = current_value + 1
            """,
            (owner_id, sequence_date),
        )
        cursor.execute(
            """
            SELECT current_value
            FROM task_code_sequences
            WHERE owner_id = %s AND sequence_date = %s
            """,
            (owner_id, sequence_date),
        )
        return int(cursor.fetchone()["current_value"])

    def _insert_user(
        self,
        cursor,
        username: str,
        display_name: str,
        role: str,
        password: str,
        *,
        must_change_password: bool,
    ) -> int:
        now = utc_now()
        cursor.execute(
            """
            INSERT INTO users(
                username, display_name, role, password_hash,
                must_change_password, is_active, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, 1, %s, %s)
            """,
            (
                username,
                display_name,
                role,
                hash_password(password),
                int(must_change_password),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _seed_defaults(self, cursor) -> None:
        cursor.execute(
            "SELECT COUNT(*) AS count FROM prompt_templates WHERE scope = 'shared'"
        )
        if int(cursor.fetchone()["count"]) == 0:
            now = utc_now()
            for name, prompt in BUILTIN_PROMPT_TEMPLATES:
                cursor.execute(
                    """
                    INSERT INTO prompt_templates(
                        scope, owner_id, name, prompt, created_at, updated_at
                    ) VALUES ('shared', NULL, %s, %s, %s, %s)
                    """,
                    (name, prompt, now, now),
                )

    @staticmethod
    def _seed_user_menu_permissions(cursor, user_id: int, role: str) -> None:
        for menu_key in ROLE_DEFAULT_MENUS.get(role, ()):
            cursor.execute(
                """
                INSERT IGNORE INTO user_menu_permissions(user_id, menu_key)
                VALUES (%s, %s)
                """,
                (user_id, menu_key),
            )

    @staticmethod
    def _assert_project_access(
        cursor,
        user_id: int,
        project_scope: str,
        project_name: str,
    ) -> None:
        cursor.execute(
            """
            SELECT 1 FROM user_project_access
            WHERE user_id = %s AND project_scope = %s
              AND project_name = %s
            LIMIT 1
            """,
            (user_id, project_scope, project_name.strip()),
        )
        if cursor.fetchone() is None:
            raise PermissionDeniedError("当前账号没有该共享盘项目的权限")

    def _insert_assets_and_tasks(
        self,
        cursor,
        project_id: int,
        draft: ProjectDraft,
        owner_id: int,
        task_code: str,
        now: datetime,
    ) -> None:
        garments = [asset for asset in draft.assets if asset.role == "garment"]
        models = [asset for asset in draft.assets if asset.role == "model"]
        asset_ids: list[int] = []
        for asset in (*garments, *models):
            cursor.execute(
                """
                INSERT INTO project_assets(
                    project_id, role, relative_path, original_name,
                    content_hash, mime, width, height, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project_id,
                    asset.role,
                    asset.relative_path,
                    asset.original_name,
                    asset.content_hash,
                    asset.mime,
                    asset.width,
                    asset.height,
                    now,
                ),
            )
            asset_ids.append(int(cursor.lastrowid))
        garment_ids = asset_ids[: len(garments)]
        model_ids = asset_ids[len(garments) :]
        grouped: dict[int, list[int]] = {}
        for pair in draft.pairs:
            grouped.setdefault(pair.model_index, []).append(pair.garment_index)
        for task_no, model_index in enumerate(grouped, start=1):
            garment_indexes = list(dict.fromkeys(grouped[model_index]))
            cursor.execute(
                """
                INSERT INTO project_tasks(
                    task_code, project_id, task_no, garment_asset_id,
                    model_asset_id, status, prompt_text, model,
                    aspect_ratio, resolution, n, final_quantity,
                    source_revision,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, '', '', '3:4', '2K', 1, 0, 1, %s, %s
                )
                """,
                (
                    f"{task_code}-{task_no:02d}",
                    project_id,
                    task_no,
                    garment_ids[garment_indexes[0]],
                    model_ids[model_index],
                    TASK_AVAILABLE,
                    now,
                    now,
                ),
            )
            subtask_id = int(cursor.lastrowid)
            for position, garment_index in enumerate(garment_indexes, start=1):
                cursor.execute(
                    """
                    INSERT INTO task_garments(
                        task_id, garment_asset_id, position
                    ) VALUES (%s, %s, %s)
                    """,
                    (
                        subtask_id,
                        garment_ids[garment_index],
                        position,
                    ),
                )

    def _project_row(self, cursor, project_id: int):
        cursor.execute(
            """
            SELECT p.*, owner.display_name AS owner_name,
                   COUNT(t.id) AS task_count,
                   COALESCE(SUM(t.status = %s), 0) AS approved_count,
                   COALESCE(SUM(t.status = %s), 0) AS awaiting_count
            FROM projects p
            JOIN users owner ON owner.id = p.owner_id
            LEFT JOIN project_tasks t
              ON t.project_id = p.id AND t.status <> 'expanded'
            WHERE p.id = %s
            GROUP BY p.id, owner.display_name
            """,
            (TASK_APPROVED, TASK_AWAITING_ACCEPTANCE, project_id),
        )
        return cursor.fetchone()

    def _project_row_for_update(self, cursor, project_id: int):
        cursor.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (project_id,))
        row = cursor.fetchone()
        if row is None:
            raise StateConflictError("项目不存在")
        return row

    @staticmethod
    def _task_select_sql() -> str:
        return """
            SELECT
                t.*,
                p.project_code,
                p.product_code AS project_name,
                p.project_relative_root,
                p.owner_id,
                p.deadline AS project_deadline,
                p.priority AS project_priority,
                p.project_scope,
                p.project_name AS access_project_name,
                p.is_urgent AS project_is_urgent,
                garment.relative_path AS garment_path,
                garment.original_name AS garment_name,
                garment.content_hash AS garment_hash,
                model_asset.relative_path AS model_path,
                model_asset.original_name AS model_name,
                model_asset.content_hash AS model_hash,
                garment_sets.garments_json,
                COALESCE(assignee.display_name, '') AS assignee_name
            FROM project_tasks t
            JOIN projects p ON p.id = t.project_id
            JOIN project_assets garment ON garment.id = t.garment_asset_id
            JOIN project_assets model_asset ON model_asset.id = t.model_asset_id
            LEFT JOIN (
                SELECT
                    tg.task_id,
                    JSON_ARRAYAGG(
                        JSON_OBJECT(
                            'id', tg.garment_asset_id,
                            'relative_path', asset.relative_path,
                            'original_name', asset.original_name,
                            'content_hash', asset.content_hash,
                            'mime', asset.mime,
                            'width', asset.width,
                            'height', asset.height,
                            'position', tg.position
                        )
                    ) AS garments_json
                FROM task_garments tg
                JOIN project_assets asset ON asset.id = tg.garment_asset_id
                GROUP BY tg.task_id
            ) garment_sets ON garment_sets.task_id = t.id
            LEFT JOIN users assignee ON assignee.id = t.assignee_id
        """

    def _task_row_for_update(self, cursor, task_id: int):
        cursor.execute(
            """
            SELECT t.*, p.id AS project_id, p.owner_id,
                   p.project_code,
                   p.status AS project_status
            FROM project_tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE t.id = %s
            FOR UPDATE
            """,
            (task_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StateConflictError("任务不存在")
        return row

    @staticmethod
    def _attempt_row_for_update(cursor, attempt_id: int):
        cursor.execute(
            """
            SELECT ga.*, t.project_id, t.id AS task_id
            FROM generation_attempts ga
            JOIN project_tasks t ON t.id = ga.task_id
            WHERE ga.id = %s
            FOR UPDATE
            """,
            (attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StateConflictError("生成记录不存在")
        return row

    def _update_project_status(self, cursor, project_id: int) -> None:
        cursor.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM project_tasks
            WHERE project_id = %s AND status <> %s
            GROUP BY status
            """,
            (project_id, TASK_EXPANDED),
        )
        counts = {row["status"]: int(row["count"]) for row in cursor.fetchall()}
        total = sum(counts.values())
        if not total:
            status = PROJECT_SUBMITTED
        elif (
            counts.get(TASK_APPROVED, 0) + counts.get(TASK_SKIPPED, 0)
            == total
        ):
            status = PROJECT_COMPLETED
        else:
            status = PROJECT_IN_PROGRESS
        now = utc_now()
        cursor.execute(
            """
            UPDATE projects
            SET status = %s, updated_at = %s,
                completed_at = CASE WHEN %s = %s THEN COALESCE(completed_at, %s) ELSE NULL END
            WHERE id = %s
            """,
            (
                status,
                now,
                status,
                PROJECT_COMPLETED,
                now,
                project_id,
            ),
        )

    @staticmethod
    def _assert_generation_slot(cursor, user_id: int) -> None:
        cursor.execute(
            """
            SELECT max_generation_concurrency
            FROM users WHERE id = %s AND deleted_at IS NULL FOR UPDATE
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise PermissionDeniedError("账号不存在")
        limit = int(row["max_generation_concurrency"])
        cursor.execute(
            """
            SELECT COUNT(*) AS count
            FROM generation_attempts
            WHERE operator_id = %s AND status = %s
            """,
            (user_id, ATTEMPT_RUNNING),
        )
        if int(cursor.fetchone()["count"]) >= limit:
            raise GenerationSlotBusyError(
                f"当前账号并发任务已达到上限（{limit} 个）"
            )

    @staticmethod
    def _event(
        cursor,
        project_id: int,
        task_id: int | None,
        actor_id: int | None,
        event_type: str,
        from_status: str,
        to_status: str,
        detail: dict[str, Any],
    ) -> None:
        cursor.execute(
            """
            INSERT INTO workflow_events(
                project_id, task_id, actor_id, event_type,
                from_status, to_status, detail_json, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                project_id,
                task_id,
                actor_id,
                event_type,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )


def _load_schema() -> str:
    return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def _add_column_if_missing(
    cursor,
    table: str,
    column: str,
    definition: str,
) -> None:
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table, column),
    )
    if int(cursor.fetchone()["count"]) == 0:
        cursor.execute(
            f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"
        )


def _add_index_if_missing(
    cursor,
    table: str,
    index: str,
    columns: str,
    *,
    unique: bool = False,
) -> None:
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = %s
        """,
        (table, index),
    )
    if int(cursor.fetchone()["count"]) == 0:
        unique_sql = "UNIQUE " if unique else ""
        cursor.execute(
            f"CREATE {unique_sql}INDEX `{index}` ON `{table}` {columns}"
        )


def _drop_index_if_exists(cursor, table: str, index: str) -> None:
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = %s
        """,
        (table, index),
    )
    if int(cursor.fetchone()["count"]) > 0:
        cursor.execute(f"DROP INDEX `{index}` ON `{table}`")


def _split_sql(schema: str) -> list[str]:
    return [statement.strip() for statement in schema.split(";") if statement.strip()]


def _mysql_error_message(exc: Exception) -> str:
    if isinstance(exc, pymysql.MySQLError) and exc.args:
        return str(exc.args[-1])
    return str(exc)


def _validate_display_name(value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError("姓名或项目名不能为空")
    if re.search(r'[<>:"/\\|?*]', clean):
        raise ValueError("姓名或项目名不能包含 Windows 文件夹禁用字符")
    return clean


def _auth_user(row: dict[str, Any]) -> AuthUser:
    return AuthUser(
        id=int(row["id"]),
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        must_change_password=bool(row["must_change_password"]),
        is_active=bool(row["is_active"]),
        urgent_quota_per_day=int(
            row.get("urgent_quota_per_day", 3)
        ),
        max_generation_concurrency=int(
            row.get("max_generation_concurrency", 2)
        ),
    )


def _project_record(row: dict[str, Any]) -> ProjectRecord:
    return ProjectRecord(
        id=int(row["id"]),
        project_code=row["project_code"],
        name=row["name"],
        deadline=row["deadline"],
        priority=row["priority"],
        brand_platform=row["brand_platform"],
        category=row["category"],
        note=row["note"],
        status=row["status"],
        owner_id=int(row["owner_id"]),
        owner_name=row["owner_name"],
        project_relative_root=row["project_relative_root"],
        pairing_mode=row["pairing_mode"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        task_count=int(row.get("task_count") or 0),
        approved_count=int(row.get("approved_count") or 0),
        awaiting_count=int(row.get("awaiting_count") or 0),
        product_code=row.get("product_code") or row["name"],
        source_date=row.get("source_date"),
        urgent_count=int(row.get("urgent_count") or 0),
        project_scope=row.get("project_scope") or "internal",
        project_name=row.get("project_name") or "",
        is_urgent=bool(row.get("is_urgent")),
        urgent_date=row.get("urgent_date"),
        urgent_by=int(row["urgent_by"]) if row.get("urgent_by") else None,
    )


def _project_task(row: dict[str, Any]) -> ProjectTask:
    garment_rows = json.loads(row.get("garments_json") or "[]")
    garment_rows.sort(key=lambda item: int(item.get("position", 0)))
    garment_assets = tuple(
        ProjectAssetInput(
            role="garment",
            relative_path=item["relative_path"],
            original_name=item["original_name"],
            content_hash=item["content_hash"],
            mime=item["mime"],
            width=int(item["width"]),
            height=int(item["height"]),
        )
        for item in garment_rows
    )
    if not garment_assets:
        garment_assets = (
            ProjectAssetInput(
                role="garment",
                relative_path=row["garment_path"],
                original_name=row["garment_name"],
                content_hash=row["garment_hash"],
                mime="image/png",
                width=0,
                height=0,
            ),
        )
    return ProjectTask(
        id=int(row["id"]),
        task_code=row["task_code"],
        project_id=int(row["project_id"]),
        project_code=row["project_code"],
        project_name=row["project_name"],
        project_relative_root=row["project_relative_root"],
        garment_asset_id=int(row["garment_asset_id"]),
        garment_path=row["garment_path"],
        garment_name=row["garment_name"],
        garment_hash=row["garment_hash"],
        model_asset_id=int(row["model_asset_id"]),
        model_path=row["model_path"],
        model_name=row["model_name"],
        model_hash=row["model_hash"],
        status=row["status"],
        assignee_id=int(row["assignee_id"]) if row["assignee_id"] else None,
        assignee_name=row["assignee_name"],
        prompt_text=row["prompt_text"],
        model=row["model"],
        aspect_ratio=row["aspect_ratio"],
        resolution=row["resolution"],
        n=int(row["n"]),
        final_quantity=int(row["final_quantity"]),
        owner_id=int(row["owner_id"]),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
        claimed_at=row["claimed_at"],
        work_started_at=row["work_started_at"],
        submitted_at=row["submitted_at"],
        approved_at=row["approved_at"],
        returned_at=row["returned_at"],
        project_deadline=row["project_deadline"],
        project_priority=row["project_priority"],
        source_date=row.get("source_date"),
        product_code=row.get("product_code") or row["project_name"],
        source_revision=int(row.get("source_revision") or 1),
        garment_assets=garment_assets,
        is_urgent=bool(row.get("is_urgent")),
        urgent_date=row.get("urgent_date"),
        urgent_by=int(row["urgent_by"]) if row.get("urgent_by") else None,
        project_scope=row.get("project_scope") or "internal",
        access_project_name=row.get("access_project_name") or "",
        project_is_urgent=bool(row.get("project_is_urgent")),
        parent_task_id=(
            int(row["parent_task_id"])
            if row.get("parent_task_id")
            else None
        ),
        split_at=row.get("split_at"),
    )


def _generation_attempt(row: dict[str, Any]) -> GenerationAttempt:
    return GenerationAttempt(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        attempt_no=int(row["attempt_no"]),
        operator_id=int(row["operator_id"]),
        model=row["model"],
        prompt_text=row["prompt_text"],
        aspect_ratio=row["aspect_ratio"],
        resolution=row["resolution"],
        n=int(row["n"]),
        status=row["status"],
        source_revision=int(row.get("source_revision") or 1),
        unit_price=Decimal(str(row["unit_price"])),
        generated_image_count=int(row["generated_image_count"]),
        request_id=row["request_id"] or "",
        remote_task_id=row["remote_task_id"] or "",
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        error_message=row["error_message"] or "",
        operator_name=row.get("operator_name") or "",
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        provider_id=(
            int(row["provider_id"])
            if row.get("provider_id")
            else None
        ),
        provider_code=row.get("provider_code") or "",
        provider_name=row.get("provider_name") or "",
        provider_model=row.get("provider_model") or "",
        requested_provider_code=(
            row.get("requested_provider_code") or ""
        ),
        requested_provider_name=(
            row.get("requested_provider_name") or ""
        ),
    )


def _api_provider(row: dict[str, Any]) -> ApiProvider:
    return ApiProvider(
        id=int(row["id"]),
        provider_code=row["provider_code"],
        display_name=row["display_name"],
        base_url=row["base_url"],
        default_protocol=row["default_protocol"],
        enabled=bool(row["is_enabled"]),
        priority=int(row["priority"]),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _provider_model_route(row: dict[str, Any]) -> ProviderModelRoute:
    return ProviderModelRoute(
        provider_id=int(row["provider_id"]),
        provider_code=row["provider_code"],
        provider_name=row["provider_name"],
        base_url=row["base_url"],
        default_protocol=row["default_protocol"],
        logical_model=row["logical_model"],
        provider_model=row["provider_model"],
        protocol=row["protocol"] or row["default_protocol"],
        max_reference_images=int(row["max_reference_images"]),
        images_per_request=int(row["images_per_request"]),
        priority=int(row["priority"]),
    )


def _generation_result(row: dict[str, Any]) -> GenerationResult:
    return GenerationResult(
        id=int(row["id"]),
        attempt_id=int(row["attempt_id"]),
        result_index=int(row["result_index"]),
        relative_path=row["relative_path"],
        thumbnail_relative_path=row["thumbnail_relative_path"],
        content_hash=row["content_hash"],
        mime=row["mime"],
        width=int(row["width"]),
        height=int(row["height"]),
        is_final=bool(row["is_final"]),
        source_type=row.get("source_type") or "generated",
        source_revision=int(row.get("source_revision") or 1),
    )


# The public repository interface is backed by MySQL in production.
CentralRepository = MySQLCentralRepository
