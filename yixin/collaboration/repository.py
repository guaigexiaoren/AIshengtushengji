from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Sequence

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from ..constants import BUILTIN_PROMPT_TEMPLATES, MODEL_IDS
from ..private_config import SHARED_ROOT
from ..security import validate_api_key
from ..time_utils import local_to_utc_naive
from .api_keys import ApiKeyError, decrypt_api_key, encrypt_api_key
from .auth import hash_password, validate_new_password, verify_password
from .reports import (
    ReportData,
    ReportFilter,
    ReportTaskRow,
    aggregate_report,
)
from .types import (
    ATTEMPT_FAILED,
    ATTEMPT_QUEUED,
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    PROJECT_AWAITING_ACCEPTANCE,
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
    TASK_RETURNED,
    TASK_SKIPPED,
    TASK_WORKING,
    AuthUser,
    GeneratedResultInput,
    GenerationAttempt,
    GenerationResult,
    PairingInput,
    ProjectAssetInput,
    ProjectDraft,
    ProjectRecord,
    ProjectTask,
)

SCHEMA_VERSION = 2


class CentralRepositoryError(RuntimeError):
    pass


class SchemaNotInitializedError(CentralRepositoryError):
    pass


class PermissionDeniedError(CentralRepositoryError):
    pass


class StateConflictError(CentralRepositoryError):
    pass


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    charset: str = "utf8mb4"

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "charset": self.charset,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MySQLCentralRepository:
    def __init__(self, config: MySQLConfig) -> None:
        self.config = config

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        try:
            connection = pymysql.connect(
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
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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

    def create_initial_admin(
        self,
        username: str,
        display_name: str,
        password: str,
    ) -> AuthUser:
        display_name = _validate_display_name(display_name)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM users LIMIT 1")
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
                        "SELECT * FROM users WHERE username = %s",
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
                    "SELECT password_hash FROM users WHERE id = %s",
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
        query = "SELECT * FROM users"
        params: tuple[Any, ...] = ()
        if role:
            query += " WHERE role = %s"
            params = (role,)
        query += " ORDER BY is_active DESC, role, display_name"
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return [_auth_user(row) for row in cursor.fetchall()]

    def create_user(
        self,
        username: str,
        display_name: str,
        role: str,
        temporary_password: str,
    ) -> AuthUser:
        username = username.strip()
        display_name = _validate_display_name(display_name)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            raise ValueError("账号只能包含字母、数字、点、下划线或短横线，长度 3–64")
        if not display_name:
            raise ValueError("姓名不能为空")
        if role not in ROLES:
            raise ValueError("不支持的账号角色")
        validate_new_password(temporary_password)
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM users WHERE display_name = %s LIMIT 1",
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
        except pymysql.err.IntegrityError as exc:
            raise StateConflictError("账号已存在") from exc
        return AuthUser(user_id, username, display_name, role, True, True)

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET is_active = %s, updated_at = %s WHERE id = %s",
                    (int(active), utc_now(), user_id),
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
                    WHERE id = %s
                    """,
                    (
                        urgent_quota_per_day,
                        max_generation_concurrency,
                        utc_now(),
                        user_id,
                    ),
                )

    def set_user_display_name(
        self,
        actor: AuthUser,
        user_id: int,
        display_name: str,
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以修改账号名称")
        clean = _validate_display_name(display_name)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id FROM users
                    WHERE display_name = %s AND id <> %s
                    LIMIT 1
                    """,
                    (clean, user_id),
                )
                if cursor.fetchone() is not None:
                    raise StateConflictError("显示名称或项目名已被其他账号使用")
                cursor.execute(
                    """
                    UPDATE users
                    SET display_name = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (clean, utc_now(), user_id),
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
                        "SELECT id FROM users WHERE id = %s FOR UPDATE",
                        (user_id,),
                    )
                    if cursor.fetchone() is None:
                        raise ValueError("账号不存在")
                    cursor.execute(
                        """
                        SELECT id FROM users
                        WHERE username = %s AND id <> %s
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
                            WHERE role = %s AND is_active = 1 AND id <> %s
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
                        WHERE id = %s
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
        except pymysql.err.IntegrityError as exc:
            raise StateConflictError("账号或显示名称已存在") from exc

    def save_api_key(
        self,
        actor: AuthUser,
        user_id: int,
        api_key: str,
    ) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以配置 API Key")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT role, is_active FROM users WHERE id = %s",
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
                        user_id, encrypted_key, key_suffix, updated_by,
                        updated_at, last_test_status, last_test_at
                    ) VALUES (%s, %s, %s, %s, %s, 'format_ok', %s)
                    ON DUPLICATE KEY UPDATE
                        encrypted_key = VALUES(encrypted_key),
                        key_suffix = VALUES(key_suffix),
                        updated_by = VALUES(updated_by),
                        updated_at = VALUES(updated_at),
                        last_test_status = VALUES(last_test_status),
                        last_test_at = VALUES(last_test_at)
                    """,
                    (user_id, encrypted, suffix, actor.id, now, now),
                )

    def clear_api_key(self, actor: AuthUser, user_id: int) -> None:
        if actor.role != ROLE_ADMIN:
            raise PermissionDeniedError("只有管理员可以清除 API Key")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM user_api_keys WHERE user_id = %s",
                    (user_id,),
                )

    def api_key_statuses(self) -> dict[int, dict[str, Any]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, key_suffix, updated_at,
                           last_test_status, last_test_at
                    FROM user_api_keys
                    """
                )
                return {
                    int(row["user_id"]): dict(row)
                    for row in cursor.fetchall()
                }

    def load_actor_api_key(self, actor: AuthUser) -> str:
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise ApiKeyError("当前账号不需要配置生图 API Key")
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT u.is_active, k.encrypted_key
                    FROM users u
                    LEFT JOIN user_api_keys k ON k.user_id = u.id
                    WHERE u.id = %s
                    """,
                    (actor.id,),
                )
                row = cursor.fetchone()
        if row is None or not bool(row["is_active"]):
            raise ApiKeyError("账号已停用")
        if not row["encrypted_key"]:
            raise ApiKeyError("尚未配置 API Key，请联系管理员")
        return decrypt_api_key(row["encrypted_key"])

    def reset_password(self, user_id: int, temporary_password: str) -> None:
        validate_new_password(temporary_password)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE users
                    SET password_hash = %s, must_change_password = 1, updated_at = %s
                    WHERE id = %s
                    """,
                    (hash_password(temporary_password), utc_now(), user_id),
                )

    def create_project(self, actor: AuthUser, draft: ProjectDraft) -> int:
        if actor.role not in (ROLE_OPERATIONS, ROLE_ADMIN):
            raise PermissionDeniedError("只有运营或管理员可以提交项目")
        self._validate_draft(draft)
        now = utc_now()
        source_date = draft.source_date or date.today()
        product_code = (
            draft.product_code.strip()
            or draft.name.strip()
            or Path(draft.project_relative_root).name
        )
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
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
                    cursor.execute(
                        """
                        INSERT INTO projects(
                            project_code, name, deadline, priority, brand_platform,
                            category, note, status, owner_id, project_relative_root,
                            pairing_mode, source_date, product_code,
                            source_revision, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, 'normal', '', '', %s, %s, %s, %s, %s,
                            %s, %s, 1, %s, %s
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
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                project = self._project_row_for_update(cursor, project_id)
                if actor.role != ROLE_ADMIN and int(project["owner_id"]) != actor.id:
                    raise PermissionDeniedError("只能修改自己提交的项目")
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

    def list_projects(self, limit: int = 5000) -> list[ProjectRecord]:
        query = """
            SELECT p.*, owner.display_name AS owner_name,
                   COUNT(t.id) AS task_count,
                   COALESCE(SUM(t.status = %s), 0) AS approved_count,
                   COALESCE(SUM(t.status = %s), 0) AS awaiting_count,
                   COALESCE(SUM(t.is_urgent = 1), 0) AS urgent_count
            FROM projects p
            JOIN users owner ON owner.id = p.owner_id
            LEFT JOIN project_tasks t ON t.project_id = p.id
            GROUP BY p.id, owner.display_name
            ORDER BY
                COALESCE(SUM(t.is_urgent), 0) DESC,
                p.source_date DESC, p.id DESC
            LIMIT %s
        """
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (TASK_APPROVED, TASK_AWAITING_ACCEPTANCE, limit),
                )
                return [_project_record(row) for row in cursor.fetchall()]

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
                           COALESCE(SUM(t.is_urgent = 1), 0) AS urgent_count
                    FROM projects p
                    JOIN users owner ON owner.id = p.owner_id
                    LEFT JOIN project_tasks t ON t.project_id = p.id
                    WHERE p.owner_id = %s AND p.source_date = %s
                      AND p.product_code = %s AND p.status <> %s
                    GROUP BY p.id, owner.display_name
                    ORDER BY p.id DESC
                    """,
                    (
                        TASK_APPROVED,
                        TASK_AWAITING_ACCEPTANCE,
                        owner_id,
                        source_date,
                        product_code,
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
                    + "ORDER BY t.is_urgent DESC, t.task_no",
                    (project_id,),
                )
                return [_project_task(row) for row in cursor.fetchall()]

    def get_project_draft(self, project_id: int) -> ProjectDraft | None:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                project = self._project_row(cursor, project_id)
                if project is None:
                    return None
                cursor.execute(
                    """
                    SELECT * FROM project_assets
                    WHERE project_id = %s
                    ORDER BY role = 'model', id
                    """,
                    (project_id,),
                )
                asset_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT garment_asset_id, model_asset_id
                    FROM project_tasks
                    WHERE project_id = %s
                    ORDER BY task_no
                    """,
                    (project_id,),
                )
                task_rows = cursor.fetchall()
        assets = tuple(
            ProjectAssetInput(
                role=row["role"],
                relative_path=row["relative_path"],
                original_name=row["original_name"],
                content_hash=row["content_hash"],
                mime=row["mime"],
                width=int(row["width"]),
                height=int(row["height"]),
            )
            for row in asset_rows
        )
        garment_indexes = {
            int(row["id"]): index
            for index, row in enumerate(
                asset for asset in asset_rows if asset["role"] == "garment"
            )
        }
        model_indexes = {
            int(row["id"]): index
            for index, row in enumerate(
                asset for asset in asset_rows if asset["role"] == "model"
            )
        }
        pairs = tuple(
            PairingInput(
                garment_indexes[int(row["garment_asset_id"])],
                model_indexes[int(row["model_asset_id"])],
            )
            for row in task_rows
        )
        return ProjectDraft(
            project_code=project["project_code"],
            name=project["name"],
            deadline=project["deadline"],
            priority=project["priority"],
            project_relative_root=project["project_relative_root"],
            pairing_mode=project["pairing_mode"],
            assets=assets,
            pairs=pairs,
            brand_platform=project["brand_platform"],
            category=project["category"],
            note=project["note"],
        )

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
        query = (
            self._task_select_sql()
            + " WHERE "
            + " AND ".join(conditions)
            + """
              ORDER BY
                t.is_urgent DESC, p.source_date DESC, p.id, t.task_no
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
                    FROM users WHERE id = %s
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

    def set_subtask_urgent(
        self,
        task_id: int,
        actor: AuthUser,
        urgent: bool,
    ) -> None:
        if actor.role not in (ROLE_OPERATIONS, ROLE_ADMIN):
            raise PermissionDeniedError("只有运营或管理员可以设置加急")
        today = date.today()
        now = utc_now()
        with self.connection() as connection:
            with connection.cursor() as cursor:
                task = self._task_row_for_update(cursor, task_id)
                if actor.role != ROLE_ADMIN and int(task["owner_id"]) != actor.id:
                    raise PermissionDeniedError("只能加急自己提交的子任务")
                if task["status"] in (
                    TASK_AWAITING_ACCEPTANCE,
                    TASK_APPROVED,
                    TASK_CANCELLED,
                ):
                    raise StateConflictError("当前子任务状态不能修改加急")
                if bool(task["is_urgent"]) == urgent:
                    return
                if urgent:
                    cursor.execute(
                        """
                        SELECT urgent_quota_per_day
                        FROM users WHERE id = %s
                        """,
                        (task["owner_id"],),
                    )
                    quota = int(cursor.fetchone()["urgent_quota_per_day"])
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM project_tasks t
                        JOIN projects p ON p.id = t.project_id
                        WHERE p.owner_id = %s
                          AND t.is_urgent = 1
                          AND t.urgent_date = %s
                          AND t.status <> %s
                        """,
                        (task["owner_id"], today, TASK_CANCELLED),
                    )
                    if int(cursor.fetchone()["count"]) >= quota:
                        raise StateConflictError(
                            f"今日加急额度已用完（{quota} 个）"
                        )
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET is_urgent = %s,
                        urgent_date = %s,
                        urgent_by = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        int(urgent),
                        today if urgent else None,
                        actor.id if urgent else None,
                        now,
                        task_id,
                    ),
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
                    "subtask_urgent" if urgent else "subtask_urgent_cleared",
                    task["status"],
                    task["status"],
                    {
                        "urgent": urgent,
                        "urgent_date": today.isoformat() if urgent else "",
                    },
                )

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
                if int(task["assignee_id"] or 0) != actor.id:
                    raise PermissionDeniedError("请先领取该任务")
                if task["status"] not in (TASK_CLAIMED, TASK_WORKING, TASK_RETURNED):
                    raise StateConflictError("当前任务状态不能生成图片")
                self._assert_generation_slot(cursor, actor.id)
                cursor.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no "
                    "FROM generation_attempts WHERE task_id = %s",
                    (task_id,),
                )
                attempt_no = int(cursor.fetchone()["next_no"])
                cursor.execute(
                    """
                    INSERT INTO generation_attempts(
                        task_id, attempt_no, operator_id, model, prompt_text,
                        aspect_ratio, resolution, n, status, source_revision,
                        unit_price,
                        created_at, started_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        task_id,
                        attempt_no,
                        actor.id,
                        model,
                        prompt,
                        aspect_ratio,
                        resolution,
                        n,
                        ATTEMPT_RUNNING,
                        int(task.get("source_revision") or 1),
                        unit_price,
                        now,
                        now,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
                previous_status = task["status"]
                work_started_at = task["work_started_at"] or now
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
        return GenerationAttempt(
            id=attempt_id,
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
            generated_image_count=0,
            created_at=now,
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
                if int(task["assignee_id"] or 0) != actor.id:
                    raise PermissionDeniedError("请先领取该任务")
                if task["status"] in (
                    TASK_AWAITING_ACCEPTANCE,
                    TASK_APPROVED,
                    TASK_CANCELLED,
                ):
                    raise StateConflictError("当前任务状态不能加入生成队列")
                cursor.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no "
                    "FROM generation_attempts WHERE task_id = %s",
                    (task_id,),
                )
                attempt_no = int(cursor.fetchone()["next_no"])
                cursor.execute(
                    """
                    INSERT INTO generation_attempts(
                        task_id, attempt_no, operator_id, model, prompt_text,
                        aspect_ratio, resolution, n, status, source_revision,
                        unit_price, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        task_id,
                        attempt_no,
                        actor.id,
                        model,
                        prompt,
                        aspect_ratio,
                        resolution,
                        n,
                        ATTEMPT_QUEUED,
                        int(task["source_revision"] or 1),
                        unit_price,
                        now,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET prompt_text = %s, model = %s, aspect_ratio = %s,
                        resolution = %s, n = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        prompt,
                        model,
                        aspect_ratio,
                        resolution,
                        n,
                        now,
                        task_id,
                    ),
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
        return GenerationAttempt(
            id=attempt_id,
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
            generated_image_count=0,
            operator_name=actor.display_name,
            created_at=now,
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
                    cursor.execute(
                        """
                        UPDATE project_tasks
                        SET assignee_id = %s, status = %s,
                            claimed_at = %s, updated_at = %s
                        WHERE id = %s
                        """,
                        (actor.id, TASK_CLAIMED, now, now, task_id),
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
                cursor.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no "
                    "FROM generation_attempts WHERE task_id = %s",
                    (task_id,),
                )
                attempt_no = int(cursor.fetchone()["next_no"])
                cursor.execute(
                    """
                    INSERT INTO generation_attempts(
                        task_id, attempt_no, operator_id, model, prompt_text,
                        aspect_ratio, resolution, n, status, source_revision,
                        unit_price, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        task_id,
                        attempt_no,
                        actor.id,
                        model,
                        prompt,
                        aspect_ratio,
                        resolution,
                        n,
                        ATTEMPT_QUEUED,
                        int(task["source_revision"] or 1),
                        unit_price,
                        now,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    UPDATE project_tasks
                    SET prompt_text = %s, model = %s, aspect_ratio = %s,
                        resolution = %s, n = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        prompt,
                        model,
                        aspect_ratio,
                        resolution,
                        n,
                        now,
                        task_id,
                    ),
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
        return GenerationAttempt(
            id=attempt_id,
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
            generated_image_count=0,
            operator_name=actor.display_name,
            created_at=now,
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
                if int(task["assignee_id"] or 0) != actor.id:
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
                if int(task["assignee_id"] or 0) != actor.id:
                    raise PermissionDeniedError("请先领取该任务")
                if task["status"] in (
                    TASK_AWAITING_ACCEPTANCE,
                    TASK_APPROVED,
                    TASK_CANCELLED,
                ):
                    raise StateConflictError("当前子任务状态不能上传成品")
                self._assert_generation_slot(cursor, actor.id)
                cursor.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no "
                    "FROM generation_attempts WHERE task_id = %s",
                    (task_id,),
                )
                attempt_no = int(cursor.fetchone()["next_no"])
                cursor.execute(
                    """
                    INSERT INTO generation_attempts(
                        task_id, attempt_no, operator_id, model, prompt_text,
                        aspect_ratio, resolution, n, status, source_revision,
                        unit_price, created_at, started_at
                    ) VALUES (
                        %s, %s, %s, 'manual_ps', %s,
                        %s, %s, 0, %s, %s, 0, %s, %s
                    )
                    """,
                    (
                        task_id,
                        attempt_no,
                        actor.id,
                        task["prompt_text"],
                        task["aspect_ratio"],
                        task["resolution"],
                        ATTEMPT_RUNNING,
                        int(task["source_revision"] or 1),
                        now,
                        now,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
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
        return GenerationAttempt(
            id=attempt_id,
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
            generated_image_count=0,
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
    ) -> None:
        if not results:
            raise ValueError("生成结果不能为空")
        now = utc_now()
        expire_at = now + timedelta(days=30)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                attempt = self._attempt_row_for_update(cursor, attempt_id)
                if int(attempt["operator_id"]) != actor.id and actor.role != ROLE_ADMIN:
                    raise PermissionDeniedError("不能修改其他人的生成记录")
                if attempt["status"] != ATTEMPT_RUNNING:
                    raise StateConflictError("生成记录状态已经变化")
                cursor.execute(
                    """
                    UPDATE generation_attempts
                    SET status = %s, generated_image_count = %s,
                        request_id = %s, remote_task_id = %s,
                        raw_response_json = %s, input_tokens = %s,
                        output_tokens = %s, finished_at = %s
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
                        now,
                        attempt_id,
                    ),
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
                            %s, %s, %s, %s, %s, %s, %s, %s, 0, %s,
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
                            expire_at,
                        ),
                    )
                self._event(
                    cursor,
                    int(attempt["project_id"]),
                    int(attempt["task_id"]),
                    actor.id,
                    "generation_succeeded",
                    TASK_WORKING,
                    TASK_WORKING,
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
                    "SELECT role, is_active FROM users WHERE id = %s",
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
                           COUNT(gr.id) AS result_count
                    FROM generation_attempts ga
                    JOIN project_tasks t ON t.id = ga.task_id
                    JOIN projects p ON p.id = t.project_id
                    JOIN users u ON u.id = ga.operator_id
                    LEFT JOIN generation_results gr ON gr.attempt_id = ga.id
                    GROUP BY ga.id, u.display_name, t.task_code,
                             t.project_id, p.project_code, p.product_code,
                             p.name
                    ORDER BY ga.created_at DESC, ga.id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [dict(row) for row in cursor.fetchall()]

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
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE generation_attempts
                    SET status = %s,
                        error_code = 'stale_running',
                        error_message = '客户端中断，生成任务已自动结束',
                        finished_at = %s
                    WHERE status = %s AND started_at < %s
                    """,
                    (ATTEMPT_FAILED, utc_now(), ATTEMPT_RUNNING, cutoff),
                )
                return cursor.rowcount

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

    def delete_generation_results(self, result_ids: Sequence[int]) -> None:
        clean_ids = tuple(dict.fromkeys(int(value) for value in result_ids))
        if not clean_ids:
            return
        placeholders = ", ".join(["%s"] * len(clean_ids))
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM generation_results WHERE is_final = 0 "
                    f"AND id IN ({placeholders})",
                    clean_ids,
                )

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

    def report(self, report_filter: ReportFilter) -> ReportData:
        conditions = ["1 = 1"]
        params: list[Any] = []
        if report_filter.start_date:
            conditions.append("p.created_at >= %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(report_filter.start_date, time.min)
                )
            )
        if report_filter.end_date:
            conditions.append("p.created_at < %s")
            params.append(
                local_to_utc_naive(
                    datetime.combine(
                        report_filter.end_date + timedelta(days=1),
                        time.min,
                    )
                )
            )
        if report_filter.project_id:
            conditions.append("p.id = %s")
            params.append(report_filter.project_id)
        if report_filter.assignee_id:
            conditions.append("t.assignee_id = %s")
            params.append(report_filter.assignee_id)
        if report_filter.model:
            conditions.append("t.model = %s")
            params.append(report_filter.model)
        query = f"""
            SELECT
                p.id AS project_id,
                p.project_code,
                p.product_code AS project_name,
                p.status AS project_status,
                p.priority,
                p.created_at AS project_created_at,
                t.id AS task_id,
                t.task_code,
                t.status AS task_status,
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
                t.returned_at,
                COALESCE(reviews.rework_count, 0) AS rework_count,
                COALESCE(attempts.generated_image_count, 0) AS generated_image_count,
                COALESCE(attempts.manual_image_count, 0) AS manual_image_count,
                COALESCE(attempts.generation_cost, 0) AS generation_cost
            FROM project_tasks t
            JOIN projects p ON p.id = t.project_id
            JOIN users owner ON owner.id = p.owner_id
            LEFT JOIN users assignee ON assignee.id = t.assignee_id
            JOIN project_assets garment ON garment.id = t.garment_asset_id
            JOIN project_assets model_asset ON model_asset.id = t.model_asset_id
            LEFT JOIN (
                SELECT task_id, COUNT(*) AS rework_count
                FROM task_reviews
                WHERE decision = 'return'
                GROUP BY task_id
            ) reviews ON reviews.task_id = t.id
            LEFT JOIN (
                SELECT
                    task_id,
                    SUM(generated_image_count) AS generated_image_count,
                    SUM(
                        CASE WHEN model = 'manual_ps'
                            THEN generated_image_count ELSE 0 END
                    ) AS manual_image_count,
                    SUM(generated_image_count * unit_price) AS generation_cost
                FROM generation_attempts
                WHERE status = %s
                GROUP BY task_id
            ) attempts ON attempts.task_id = t.id
            WHERE {" AND ".join(conditions)}
            ORDER BY p.created_at, p.id, t.task_no
        """
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (ATTEMPT_SUCCEEDED, *params))
                rows = [
                    ReportTaskRow(
                        project_id=int(row["project_id"]),
                        project_code=row["project_code"],
                        project_name=row["project_name"],
                        project_status=row["project_status"],
                        priority=row["priority"],
                        task_id=int(row["task_id"]),
                        task_code=row["task_code"],
                        task_status=row["task_status"],
                        owner_name=row["owner_name"],
                        assignee_name=row["assignee_name"],
                        model=row["model"],
                        garment_name=row["garment_name"],
                        model_name=row["model_name"],
                        final_quantity=int(row["final_quantity"] or 0),
                        rework_count=int(row["rework_count"] or 0),
                        generated_image_count=int(row["generated_image_count"] or 0),
                        manual_image_count=int(row["manual_image_count"] or 0),
                        generation_cost=Decimal(str(row["generation_cost"] or 0)),
                        project_created_at=row["project_created_at"],
                        claimed_at=row["claimed_at"],
                        work_started_at=row["work_started_at"],
                        submitted_at=row["submitted_at"],
                        approved_at=row["approved_at"],
                        returned_at=row["returned_at"],
                    )
                    for row in cursor.fetchall()
                ]
        return aggregate_report(rows)

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
        for pair in draft.pairs:
            if not 0 <= pair.garment_index < garment_count:
                raise ValueError("配对引用了不存在的服装图")
            if not 0 <= pair.model_index < model_count:
                raise ValueError("配对引用了不存在的模特图")
            if pair in seen:
                raise ValueError("项目包含重复配对")
            seen.add(pair)

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
            LEFT JOIN project_tasks t ON t.project_id = p.id
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
            WHERE project_id = %s
            GROUP BY status
            """,
            (project_id,),
        )
        counts = {row["status"]: int(row["count"]) for row in cursor.fetchall()}
        total = sum(counts.values())
        if not total:
            status = PROJECT_SUBMITTED
        elif (
            counts.get(TASK_APPROVED, 0) > 0
            and counts.get(TASK_APPROVED, 0) + counts.get(TASK_SKIPPED, 0)
            == total
        ):
            status = PROJECT_COMPLETED
        elif (
            counts.get(TASK_APPROVED, 0)
            + counts.get(TASK_AWAITING_ACCEPTANCE, 0)
            + counts.get(TASK_SKIPPED, 0)
            == total
        ):
            status = PROJECT_AWAITING_ACCEPTANCE
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
            FROM users WHERE id = %s FOR UPDATE
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
            raise StateConflictError(
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
