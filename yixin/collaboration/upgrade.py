from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from ..paths import AppPaths


def prepare_legacy_data(paths: AppPaths) -> Path | None:
    if paths.collaboration_marker.exists():
        return None
    database = paths.database
    if not database.exists():
        paths.collaboration_marker.write_text("fresh-install\n", encoding="utf-8")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_base = paths.root.parent / f"{paths.root.name}_backup_{timestamp}"
    archive_path = Path(
        shutil.make_archive(
            str(archive_base),
            "zip",
            root_dir=paths.root.parent,
            base_dir=paths.root.name,
        )
    )
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{database}{suffix}")
        if source.exists():
            source.rename(database.with_name(f"{database.name}.legacy{suffix}.bak"))
    paths.collaboration_marker.write_text(
        f"backup={archive_path.name}\n",
        encoding="utf-8",
    )
    return archive_path
