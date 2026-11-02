from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .constants import (
    BUILTIN_PROMPT_TEMPLATES,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCESS,
)
from .models import ImageRequest, StoredImage
from .paging import PageRequest, PageResult, TaskQuery
from .storage import utc_now_iso
from .time_utils import local_to_utc_naive


SCHEMA_VERSION = 4


class StateTransitionError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    model TEXT NOT NULL,
                    auto_strategy TEXT,
                    auto_reason TEXT,
                    prompt TEXT NOT NULL,
                    aspect_ratio TEXT NOT NULL,
                    resolution TEXT,
                    n INTEGER NOT NULL,
                    request_id TEXT,
                    remote_task_id TEXT,
                    raw_response TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    error_code TEXT,
                    error_message TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS task_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    original_name TEXT NOT NULL,
                    source_path TEXT
                );

                CREATE TABLE IF NOT EXISTS task_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    thumbnail_path TEXT,
                    content_hash TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    remote_url TEXT,
                    saved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS works (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                    folder_name TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    model TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    raw_response TEXT,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS work_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    original_name TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    prompt TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                );

                CREATE TABLE IF NOT EXISTS work_tags (
                    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    PRIMARY KEY (work_id, tag_id)
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status_id ON tasks(status, id);
                CREATE INDEX IF NOT EXISTS idx_task_results_task ON task_results(task_id, position);
                CREATE INDEX IF NOT EXISTS idx_work_assets_work ON work_assets(work_id, role, position);
                CREATE INDEX IF NOT EXISTS idx_works_favorite ON works(favorite, created_at);
                """
            )
            task_asset_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(task_assets)").fetchall()
            }
            if "source_path" not in task_asset_columns:
                connection.execute("ALTER TABLE task_assets ADD COLUMN source_path TEXT")
            task_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "auto_strategy" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN auto_strategy TEXT")
            if "auto_reason" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN auto_reason TEXT")
            if "created_at" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"
                )
            if "updated_at" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_created_id "
                "ON tasks(created_at, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_created "
                "ON tasks(status, created_at)"
            )
            self._seed_builtin_templates(connection)
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _seed_builtin_templates(connection: sqlite3.Connection) -> None:
        seed_key = "builtin_templates_seeded_v1"
        seeded = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (seed_key,),
        ).fetchone()
        if seeded is not None:
            return
        now = utc_now_iso()
        for name, prompt in BUILTIN_PROMPT_TEMPLATES:
            connection.execute(
                """
                INSERT OR IGNORE INTO templates(name, prompt, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, prompt, now, now),
            )
        connection.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, '1')",
            (seed_key,),
        )

    def create_task(self, request: ImageRequest) -> int:
        return self.create_tasks((request,))[0]

    def create_tasks(self, requests: Sequence[ImageRequest]) -> list[int]:
        requests = tuple(requests)
        if not requests:
            raise ValueError("至少需要一个任务")
        for request in requests:
            request.validate()

        task_ids: list[int] = []
        now = utc_now_iso()
        with self.connection() as connection:
            for request in requests:
                task_ids.append(self._insert_task(connection, request, now))
        return task_ids

    @staticmethod
    def _insert_task(
        connection: sqlite3.Connection,
        request: ImageRequest,
        now: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO tasks(
                status, mode, model, auto_strategy, auto_reason,
                prompt, aspect_ratio, resolution, n,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                TASK_QUEUED,
                request.mode,
                request.model,
                request.auto_strategy,
                request.auto_reason,
                request.prompt.strip(),
                request.aspect_ratio,
                request.effective_resolution,
                request.n,
                now,
                now,
            ),
        )
        task_id = int(cursor.lastrowid)
        for position, image in enumerate(request.reference_images):
            connection.execute(
                """
                INSERT INTO task_assets(
                    task_id, role, position, path, content_hash, mime,
                    width, height, original_name, source_path
                ) VALUES (?, 'reference', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    position,
                    str(image.path),
                    image.content_hash,
                    image.mime,
                    image.width,
                    image.height,
                    image.original_name,
                    str(image.source_path) if image.source_path else None,
                ),
            )
        return task_id

    def claim_next_queued(self) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY id LIMIT 1",
                (TASK_QUEUED,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (TASK_RUNNING, now, now, row["id"], TASK_QUEUED),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (row["id"],)
            ).fetchone()
            return dict(claimed)

    def recover_after_restart(self) -> int:
        now = utc_now_iso()
        with self.connection() as connection:
            cancelled = connection.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, finished_at = ?,
                    error_message = COALESCE(error_message, '用户已停止等待')
                WHERE status = ? AND cancel_requested = 1
                """,
                (TASK_CANCELLED, now, now, TASK_RUNNING),
            ).rowcount
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, finished_at = ?,
                    error_code = 'interrupted',
                    error_message = '上次程序意外退出，任务可能已经产生费用'
                WHERE status = ? AND cancel_requested = 0
                """,
                (TASK_FAILED, now, now, TASK_RUNNING),
            )
        return cancelled

    def transition_task(
        self,
        task_id: int,
        target_status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        raw_response: str | None = None,
        request_id: str | None = None,
        remote_task_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        allowed = {
            TASK_QUEUED: {TASK_RUNNING, TASK_CANCELLED},
            TASK_RUNNING: {TASK_SUCCESS, TASK_FAILED, TASK_CANCELLED},
            TASK_SUCCESS: set(),
            TASK_FAILED: set(),
            TASK_CANCELLED: set(),
        }
        now = utc_now_iso()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise StateTransitionError("任务不存在")
            current = row["status"]
            if target_status not in allowed.get(current, set()):
                raise StateTransitionError(f"不能从 {current} 切换到 {target_status}")
            finished_at = now if target_status in (TASK_SUCCESS, TASK_FAILED, TASK_CANCELLED) else None
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, finished_at = COALESCE(?, finished_at),
                    error_code = COALESCE(?, error_code),
                    error_message = COALESCE(?, error_message),
                    raw_response = COALESCE(?, raw_response),
                    request_id = COALESCE(?, request_id),
                    remote_task_id = COALESCE(?, remote_task_id),
                    input_tokens = COALESCE(?, input_tokens),
                    output_tokens = COALESCE(?, output_tokens)
                WHERE id = ?
                """,
                (
                    target_status,
                    now,
                    finished_at,
                    error_code,
                    error_message,
                    raw_response,
                    request_id,
                    remote_task_id,
                    input_tokens,
                    output_tokens,
                    task_id,
                ),
            )

    def update_task_from_api(
        self,
        task_id: int,
        *,
        request_id: str | None,
        remote_task_id: str | None,
        raw_response: str,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET request_id = COALESCE(?, request_id),
                    remote_task_id = COALESCE(?, remote_task_id),
                    raw_response = ?,
                    input_tokens = COALESCE(?, input_tokens),
                    output_tokens = COALESCE(?, output_tokens),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    request_id,
                    remote_task_id,
                    raw_response,
                    input_tokens,
                    output_tokens,
                    utc_now_iso(),
                    task_id,
                ),
            )

    def record_failure_metadata(
        self,
        task_id: int,
        *,
        request_id: str | None,
        remote_task_id: str | None,
        raw_response: str | None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET request_id = COALESCE(?, request_id),
                    remote_task_id = COALESCE(?, remote_task_id),
                    raw_response = COALESCE(?, raw_response),
                    updated_at = ?
                WHERE id = ?
                """,
                (request_id, remote_task_id, raw_response, utc_now_iso(), task_id),
            )

    def request_cancel(self, task_id: int) -> str:
        now = utc_now_iso()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("任务不存在")
            if row["status"] == TASK_QUEUED:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, cancel_requested = 1, updated_at = ?, finished_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (TASK_CANCELLED, now, now, task_id, TASK_QUEUED),
                )
                return TASK_CANCELLED
            if row["status"] == TASK_RUNNING:
                connection.execute(
                    """
                    UPDATE tasks
                    SET cancel_requested = 1, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (now, task_id, TASK_RUNNING),
                )
                return TASK_RUNNING
            return row["status"]

    def is_cancel_requested(self, task_id: int) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return bool(row and row["cancel_requested"])

    def add_task_result(
        self,
        task_id: int,
        position: int,
        image: StoredImage,
        *,
        thumbnail_path: Path | None,
        remote_url: str | None,
    ) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO task_results(
                    task_id, position, path, thumbnail_path, content_hash, mime,
                    width, height, remote_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    position,
                    str(image.path),
                    str(thumbnail_path) if thumbnail_path else None,
                    image.content_hash,
                    image.mime,
                    image.width,
                    image.height,
                    remote_url,
                    utc_now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_tasks(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT tasks.*,
                       (
                           SELECT COUNT(*)
                           FROM task_results
                           WHERE task_results.task_id = tasks.id
                       ) AS result_count
                FROM tasks
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_tasks_page(
        self,
        query: TaskQuery,
        page: PageRequest,
    ) -> PageResult[dict[str, Any]]:
        sort_fields = {
            "id": "id",
            "status": "status",
            "mode": "mode",
            "model": "model",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "kind": "'ordinary'",
            "result_count": "result_count",
        }
        sort_field = sort_fields.get(page.sort_field, "created_at")
        direction = "ASC" if page.sort_direction == "asc" else "DESC"
        conditions = ["1 = 1"]
        params: list[Any] = []
        keyword = query.keyword.strip()
        if keyword:
            pattern = f"%{keyword}%"
            conditions.append(
                "("
                "CAST(id AS TEXT) LIKE ? OR prompt LIKE ? OR "
                "model LIKE ? OR mode LIKE ?"
                ")"
            )
            params.extend((pattern, pattern, pattern, pattern))
        if query.statuses:
            placeholders = ", ".join("?" for _ in query.statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(query.statuses)
        if query.start_date is not None:
            start = local_to_utc_naive(
                datetime.combine(query.start_date, time.min)
            ).isoformat(timespec="seconds")
            conditions.append("created_at >= ?")
            params.append(start)
        if query.end_date is not None:
            end_date = query.end_date
            end = local_to_utc_naive(
                datetime.combine(end_date, time.max)
            ).isoformat(timespec="seconds")
            conditions.append("created_at <= ?")
            params.append(end)
        where_sql = " AND ".join(conditions)
        with self.connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM tasks WHERE {where_sql}",
                    tuple(params),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT tasks.*,
                       (
                           SELECT COUNT(*)
                           FROM task_results
                           WHERE task_results.task_id = tasks.id
                       ) AS result_count
                FROM tasks
                WHERE {where_sql}
                ORDER BY {sort_field} {direction}, id {direction}
                LIMIT ? OFFSET ?
                """,
                (*params, page.page_size, page.offset),
            ).fetchall()
        return PageResult(
            items=[dict(row) for row in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    def get_task_references(self, task_id: int) -> list[StoredImage]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_assets
                WHERE task_id = ? AND role = 'reference'
                ORDER BY position
                """,
                (task_id,),
            ).fetchall()
            return [
                StoredImage(
                    path=Path(row["path"]),
                    content_hash=row["content_hash"],
                    mime=row["mime"],
                    width=row["width"],
                    height=row["height"],
                    original_name=row["original_name"],
                    source_path=(
                        Path(row["source_path"]) if row["source_path"] else None
                    ),
                )
                for row in rows
            ]

    def get_task_results(self, task_id: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM task_results WHERE task_id = ? ORDER BY position",
                (task_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def active_temp_paths(self) -> set[Path]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT a.path
                FROM task_assets a
                JOIN tasks t ON t.id = a.task_id
                WHERE t.status IN (?, ?)
                """,
                (TASK_QUEUED, TASK_RUNNING),
            ).fetchall()
            return {Path(row["path"]) for row in rows}

    def has_running_tasks(self) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM tasks WHERE status = ? LIMIT 1", (TASK_RUNNING,)
            ).fetchone()
            return row is not None

    def create_work_with_assets(
        self,
        *,
        task_id: int,
        folder_name: str,
        title: str,
        prompt: str,
        model: str,
        parameters: dict[str, Any],
        raw_response: str | None,
        assets: Sequence[tuple[str, int, StoredImage]],
        saved_result_id: int,
        saved_path: Path,
        saved_thumbnail_path: Path,
        saved_mime: str,
        saved_width: int,
        saved_height: int,
    ) -> int:
        with self.connection() as connection:
            work_id = self._insert_work_with_assets(
                connection,
                task_id=task_id,
                folder_name=folder_name,
                title=title,
                prompt=prompt,
                model=model,
                parameters=parameters,
                raw_response=raw_response,
                assets=assets,
            )
            updated = connection.execute(
                """
                UPDATE task_results
                SET saved = 1, path = ?, thumbnail_path = ?,
                    mime = ?, width = ?, height = ?
                WHERE id = ? AND task_id = ?
                """,
                (
                    str(saved_path),
                    str(saved_thumbnail_path),
                    saved_mime,
                    saved_width,
                    saved_height,
                    saved_result_id,
                    task_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("生成结果不存在，作品保存已回滚")
            return work_id

    def create_manual_work_with_assets(
        self,
        *,
        folder_name: str,
        title: str,
        parameters: dict[str, Any],
        assets: Sequence[tuple[str, int, StoredImage]],
    ) -> int:
        with self.connection() as connection:
            return self._insert_work_with_assets(
                connection,
                task_id=None,
                folder_name=folder_name,
                title=title,
                prompt="",
                model="手动上传",
                parameters=parameters,
                raw_response=None,
                assets=assets,
            )

    def create_external_work_with_assets(
        self,
        *,
        folder_name: str,
        title: str,
        prompt: str,
        model: str,
        parameters: dict[str, Any],
        assets: Sequence[tuple[str, int, StoredImage]],
    ) -> int:
        with self.connection() as connection:
            return self._insert_work_with_assets(
                connection,
                task_id=None,
                folder_name=folder_name,
                title=title,
                prompt=prompt,
                model=model,
                parameters=parameters,
                raw_response=None,
                assets=assets,
            )

    @staticmethod
    def _insert_work_with_assets(
        connection: sqlite3.Connection,
        *,
        task_id: int | None,
        folder_name: str,
        title: str,
        prompt: str,
        model: str,
        parameters: dict[str, Any],
        raw_response: str | None,
        assets: Sequence[tuple[str, int, StoredImage]],
    ) -> int:
        now = utc_now_iso()
        cursor = connection.execute(
            """
            INSERT INTO works(
                task_id, folder_name, title, prompt, model,
                parameters_json, raw_response, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                folder_name,
                title,
                prompt,
                model,
                json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                raw_response,
                now,
                now,
            ),
        )
        work_id = int(cursor.lastrowid)
        for role, position, image in assets:
            connection.execute(
                """
                INSERT INTO work_assets(
                    work_id, role, position, path, content_hash, mime,
                    width, height, original_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_id,
                    role,
                    position,
                    str(image.path),
                    image.content_hash,
                    image.mime,
                    image.width,
                    image.height,
                    image.original_name,
                ),
            )
        return work_id

    def get_work(self, work_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM works WHERE id = ?", (work_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_work_assets(
        self, work_id: int, *, role: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM work_assets WHERE work_id = ?"
        params: list[Any] = [work_id]
        if role:
            sql += " AND role = ?"
            params.append(role)
        sql += " ORDER BY role, position, id"
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def list_works(
        self,
        *,
        search: str = "",
        tags: Sequence[str] = (),
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        parameters: list[Any] = []
        search = search.strip()
        if search:
            like = f"%{search.lower()}%"
            clauses.append(
                """
                (
                    lower(w.title) LIKE ? OR lower(w.prompt) LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM work_tags wt
                        JOIN tags t ON t.id = wt.tag_id
                        WHERE wt.work_id = w.id AND lower(t.name) LIKE ?
                    )
                )
                """
            )
            parameters.extend([like, like, like])
        clean_tags = tuple(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
        if clean_tags:
            placeholders = ",".join("?" for _ in clean_tags)
            clauses.append(
                f"""
                w.id IN (
                    SELECT wt.work_id FROM work_tags wt
                    JOIN tags t ON t.id = wt.tag_id
                    WHERE t.name IN ({placeholders})
                    GROUP BY wt.work_id
                    HAVING COUNT(DISTINCT t.name) = ?
                )
                """
            )
            parameters.extend(clean_tags)
            parameters.append(len(clean_tags))
        sql = f"""
            SELECT
                w.*,
                COALESCE(
                    (
                        SELECT path FROM work_assets thumbnail
                        WHERE thumbnail.work_id = w.id AND thumbnail.role = 'thumbnail'
                        ORDER BY thumbnail.id LIMIT 1
                    ),
                    (
                        SELECT path FROM work_assets result
                        WHERE result.work_id = w.id AND result.role = 'result'
                        ORDER BY result.position, result.id LIMIT 1
                    )
                ) AS cover_path
            FROM works w
            WHERE {' AND '.join(clauses)}
            ORDER BY w.created_at DESC, w.id DESC
            LIMIT ?
        """
        parameters.append(limit)
        with self.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            return [dict(row) for row in rows]

    def list_work_result_previews(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    w.id AS work_id,
                    w.title,
                    result.path AS result_path,
                    COALESCE(
                        (
                            SELECT thumbnail.path FROM work_assets thumbnail
                            WHERE thumbnail.work_id = w.id
                              AND thumbnail.role = 'thumbnail'
                            ORDER BY thumbnail.id LIMIT 1
                        ),
                        result.path
                    ) AS preview_path
                FROM works w
                JOIN work_assets result
                  ON result.work_id = w.id AND result.role = 'result'
                ORDER BY w.created_at DESC, w.id DESC,
                         result.position, result.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_work_tags(self, work_id: int, names: Iterable[str]) -> list[str]:
        clean_names = list(
            dict.fromkeys(name.strip() for name in names if name.strip())
        )
        with self.connection() as connection:
            connection.execute("DELETE FROM work_tags WHERE work_id = ?", (work_id,))
            for name in clean_names:
                connection.execute(
                    "INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,)
                )
                row = connection.execute(
                    "SELECT id FROM tags WHERE name = ?", (name,)
                ).fetchone()
                connection.execute(
                    "INSERT OR IGNORE INTO work_tags(work_id, tag_id) VALUES (?, ?)",
                    (work_id, row["id"]),
                )
            connection.execute(
                "UPDATE works SET updated_at = ? WHERE id = ?",
                (utc_now_iso(), work_id),
            )
            connection.execute(
                """
                DELETE FROM tags
                WHERE id NOT IN (SELECT DISTINCT tag_id FROM work_tags)
                """
            )
        return clean_names

    def get_work_tags(self, work_id: int) -> list[str]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT t.name FROM tags t
                JOIN work_tags wt ON wt.tag_id = t.id
                WHERE wt.work_id = ?
                ORDER BY t.name
                """,
                (work_id,),
            ).fetchall()
            return [row["name"] for row in rows]

    def list_tags(self) -> list[str]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT t.name FROM tags t
                JOIN work_tags wt ON wt.tag_id = t.id
                GROUP BY t.name ORDER BY lower(t.name)
                """
            ).fetchall()
            return [row["name"] for row in rows]

    def delete_work(self, work_id: int) -> dict[str, Any] | None:
        work = self.get_work(work_id)
        if work is None:
            return None
        assets = self.get_work_assets(work_id)
        with self.connection() as connection:
            connection.execute("DELETE FROM works WHERE id = ?", (work_id,))
            connection.execute(
                "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM work_tags)"
            )
        return {"work": work, "assets": assets}

    def list_templates(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM templates ORDER BY lower(name)"
            ).fetchall()
            return [dict(row) for row in rows]

    def save_template(self, name: str, prompt: str) -> int:
        clean_name = name.strip()
        clean_prompt = prompt.strip()
        if not clean_name:
            raise ValueError("模板名称不能为空")
        if not clean_prompt:
            raise ValueError("模板提示词不能为空")
        now = utc_now_iso()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO templates(name, prompt, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    prompt = excluded.prompt,
                    updated_at = excluded.updated_at
                """,
                (clean_name, clean_prompt, now, now),
            )
            row = connection.execute(
                "SELECT id FROM templates WHERE name = ?", (clean_name,)
            ).fetchone()
            return int(row["id"])

    def delete_template(self, template_id: int) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM templates WHERE id = ?", (template_id,))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
