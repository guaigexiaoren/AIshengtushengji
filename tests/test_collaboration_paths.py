import tempfile
import unittest
from pathlib import Path

from yixin.collaboration.shared_paths import (
    SharedPathResolver,
    safe_path_component,
)
from yixin.collaboration.types import ProjectTask
from datetime import datetime


def make_task(project_root: str) -> ProjectTask:
    return ProjectTask(
        id=1,
        task_code="P001-0001",
        project_id=1,
        project_code="P001",
        project_name="测试项目",
        project_relative_root=project_root,
        garment_asset_id=1,
        garment_path="衣服图/G001.png",
        garment_name="G001.png",
        garment_hash="a" * 64,
        model_asset_id=2,
        model_path="模特图/M001.png",
        model_name="M001.png",
        model_hash="b" * 64,
        status="available",
        assignee_id=None,
        assignee_name="",
        prompt_text="",
        model="",
        aspect_ratio="3:4",
        resolution="2K",
        n=1,
        final_quantity=0,
        owner_id=2,
        created_at=datetime.now(),
    )


class SharedPathTests(unittest.TestCase):
    def test_project_relative_and_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "share"
            project = root / "项目A"
            (project / "衣服图").mkdir(parents=True)
            resolver = SharedPathResolver(root)
            self.assertEqual(resolver.relative_to_root(project), "项目A")
            task = make_task("项目A")
            output = resolver.output_directory(task, 2)
            self.assertEqual(
                resolver.relative_output_path(task, output / "result.png"),
                "AI成品/P001/P001-0001/attempt_002/result.png",
            )

    def test_path_cannot_escape_share_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            resolver = SharedPathResolver(Path(temp) / "share")
            with self.assertRaises(ValueError):
                resolver.resolve("../outside.png")

    def test_windows_invalid_characters_are_sanitized(self) -> None:
        self.assertEqual(safe_path_component('P:001/测试?'), "P_001_测试_")


if __name__ == "__main__":
    unittest.main()
