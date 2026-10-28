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
    for task_id in task_ids:
        task = repository.get_task(task_id)
        if task is None:
            continue
        expired = repository.expired_candidate_results(task_id)
        if not expired:
            continue
        delete_ids: list[int] = []
        for result in expired:
            for relative_path in (
                result.relative_path,
                result.thumbnail_relative_path,
            ):
                if not relative_path:
                    continue
                try:
                    path = resolver.resolve_project_path(
                        task.project_relative_root,
                        relative_path,
                    )
                    if (
                        path.is_file()
                        and repository.result_path_reference_count(
                            relative_path,
                            result.id,
                        )
                        == 0
                    ):
                        path.unlink()
                        removed.append(str(path))
                except Exception as exc:
                    logger.warning(
                        "候选图清理失败：%s",
                        sanitize_text(str(exc)),
                    )
            delete_ids.append(result.id)
        repository.delete_generation_results(delete_ids)
    return removed
