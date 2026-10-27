from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..logging_utils import sanitize_text
from ..storage import (
    MIME_TO_SUFFIX,
    FileStore,
    atomic_write_bytes,
    image_metadata,
    sha256_bytes,
)
from .repository import MySQLCentralRepository
from .shared_paths import SharedPathResolver, safe_path_component
from .types import AuthUser, GeneratedResultInput, ProjectTask


def upload_manual_candidates(
    repository: MySQLCentralRepository,
    resolver: SharedPathResolver,
    file_store: FileStore,
    task: ProjectTask,
    actor: AuthUser,
    paths: Sequence[Path],
) -> int:
    if not paths:
        return 0
    attempt = repository.create_manual_attempt(task.id, actor)
    project_root = resolver.project_root(task.project_relative_root)
    output = resolver.output_directory(task, attempt.attempt_no)
    output.mkdir(parents=True, exist_ok=True)
    generated: list[GeneratedResultInput] = []
    seen: set[str] = set()
    task_prefix = safe_path_component(task.task_code)
    try:
        for source in paths:
            data = Path(source).read_bytes()
            mime, width, height = image_metadata(data)
            content_hash = sha256_bytes(data)
            if content_hash in seen:
                continue
            seen.add(content_hash)
            index = len(generated)
            suffix = MIME_TO_SUFFIX.get(mime, ".png")
            destination = output / (
                f"manual_{task_prefix}_r{attempt.attempt_no:03d}_"
                f"{index + 1:02d}_{content_hash[:12]}{suffix}"
            )
            atomic_write_bytes(destination, data)
            thumbnail = file_store.create_thumbnail(
                destination,
                output
                / (
                    f"thumb_manual_{task_prefix}_"
                    f"r{attempt.attempt_no:03d}_{index + 1:02d}.jpg"
                ),
            )
            generated.append(
                GeneratedResultInput(
                    result_index=index,
                    relative_path=resolver.relative_to_project(
                        project_root,
                        destination,
                    ),
                    thumbnail_relative_path=resolver.relative_to_project(
                        project_root,
                        thumbnail,
                    ),
                    content_hash=content_hash,
                    mime=mime,
                    width=width,
                    height=height,
                )
            )
        if not generated:
            raise ValueError("所选图片内容重复，没有可上传的新成品")
        repository.complete_generation_attempt(
            attempt.id,
            actor,
            request_id="",
            remote_task_id="",
            raw_response={"source": "manual_ps"},
            input_tokens=None,
            output_tokens=None,
            results=generated,
            source_type="manual_ps",
        )
        return len(generated)
    except Exception as exc:
        try:
            repository.fail_generation_attempt(
                attempt.id,
                actor,
                error_code="manual_upload_failed",
                error_message=sanitize_text(str(exc)),
            )
        except Exception:
            pass
        raise
