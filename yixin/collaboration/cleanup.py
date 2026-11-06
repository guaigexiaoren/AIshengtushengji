from __future__ import annotations

import logging
from collections.abc import Iterable

from ..logging_utils import sanitize_text
from .repository import MySQLCentralRepository
from .shared_paths import SharedPathResolver


def cleanup_expired_candidates(
    repository: MySQLCentralRepository,
    resolver: SharedPathResolver,
    task_ids: Iterable[int],
) -> list[str]:
    logger = logging.getLogger("yixin.collaboration.cleanup")
    removed: list[str] = []
    ids = tuple(dict.fromkeys(int(value) for value in task_ids))
    if not ids:
        return removed
    # 一次性取出所有任务的过期候选图，避免逐任务、逐文件查询。
    expired = repository.expired_candidate_results_for_tasks(ids)
    if not expired:
        return removed
    roots = repository.task_project_roots(ids)
    # 先删数据库行，只有确实删除的行才去清文件，避免误删刚被设为最终图的文件。
    deleted_ids = set(
        repository.delete_generation_results(
            [result.id for _task_id, result in expired]
        )
    )
    pending = [
        (task_id, result)
        for task_id, result in expired
        if result.id in deleted_ids
    ]
    paths = [
        path
        for _task_id, result in pending
        for path in (result.relative_path, result.thumbnail_relative_path)
        if path
    ]
    in_use = repository.result_paths_in_use(paths)
    for task_id, result in pending:
        root = roots.get(task_id)
        if root is None:
            continue
        for relative_path in (
            result.relative_path,
            result.thumbnail_relative_path,
        ):
            if not relative_path or relative_path in in_use:
                continue
            try:
                path = resolver.resolve_project_path(root, relative_path)
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
            except Exception as exc:
                logger.warning(
                    "候选图清理失败：%s",
                    sanitize_text(str(exc)),
                )
    return removed
