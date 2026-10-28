import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

from yixin.collaboration.cleanup import cleanup_expired_candidates
from yixin.collaboration.shared_paths import SharedPathResolver
from yixin.collaboration.types import GenerationResult, ProjectTask


class FakeCleanupRepository:
    def __init__(self, task: ProjectTask, result: GenerationResult) -> None:
        self.task = task
        self.result = result
        self.deleted: list[int] = []
        self.referenced_paths: set[str] = set()

    def get_task(self, _task_id):
        return self.task

    def expired_candidate_results(self, _task_id):
        return [self.result]

    def delete_generation_results(self, result_ids):
        self.deleted.extend(result_ids)

    def result_path_reference_count(self, relative_path, _excluding_result_id):
        return int(relative_path in self.referenced_paths)


class CollaborationCleanupTests(unittest.TestCase):
    def test_only_candidate_files_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "项目"
            output = project / "AI成品" / "P001" / "P001-0001" / "attempt_001"
            output.mkdir(parents=True)
            candidate = output / "candidate.png"
            thumbnail = output / "thumb.jpg"
            final = output / "final.png"
            for path in (candidate, thumbnail, final):
                Image.new("RGB", (10, 10), "white").save(path)
            task = ProjectTask(
                id=1,
                task_code="P001-0001",
                project_id=1,
                project_code="P001",
                project_name="测试",
                project_relative_root="项目",
                garment_asset_id=1,
                garment_path="衣服图/G001.png",
                garment_name="G001.png",
                garment_hash="a" * 64,
                model_asset_id=2,
                model_path="模特图/M001.png",
                model_name="M001.png",
                model_hash="b" * 64,
                status="approved",
                assignee_id=2,
                assignee_name="制作",
                prompt_text="",
                model="",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                final_quantity=1,
                owner_id=3,
                created_at=datetime.now(),
            )
            result = GenerationResult(
                id=10,
                attempt_id=1,
                result_index=0,
                relative_path=candidate.relative_to(project).as_posix(),
                thumbnail_relative_path=thumbnail.relative_to(project).as_posix(),
                content_hash="a" * 64,
                mime="image/png",
                width=10,
                height=10,
                is_final=False,
            )
            repository = FakeCleanupRepository(task, result)
            removed = cleanup_expired_candidates(
                repository,
                SharedPathResolver(root),
                [task.id],
            )
            self.assertEqual(len(removed), 2)
            self.assertFalse(candidate.exists())
            self.assertFalse(thumbnail.exists())
            self.assertTrue(final.exists())
            self.assertEqual(repository.deleted, [10])

    def test_shared_result_path_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "项目"
            output = project / "AI成品"
            output.mkdir(parents=True)
            candidate = output / "_r001_01_abcdef123456.png"
            Image.new("RGB", (10, 10), "white").save(candidate)
            task = ProjectTask(
                id=1,
                task_code="P001-0001",
                project_id=1,
                project_code="P001",
                project_name="测试",
                project_relative_root="项目",
                garment_asset_id=1,
                garment_path="衣服图/G001.png",
                garment_name="G001.png",
                garment_hash="a" * 64,
                model_asset_id=2,
                model_path="模特图/M001.png",
                model_name="M001.png",
                model_hash="b" * 64,
                status="approved",
                assignee_id=2,
                assignee_name="制作",
                prompt_text="",
                model="",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                final_quantity=1,
                owner_id=3,
                created_at=datetime.now(),
            )
            relative_path = candidate.relative_to(project).as_posix()
            result = GenerationResult(
                id=10,
                attempt_id=1,
                result_index=0,
                relative_path=relative_path,
                thumbnail_relative_path="",
                content_hash="a" * 64,
                mime="image/png",
                width=10,
                height=10,
                is_final=False,
            )
            repository = FakeCleanupRepository(task, result)
            repository.referenced_paths.add(relative_path)
            removed = cleanup_expired_candidates(
                repository,
                SharedPathResolver(root),
                [task.id],
            )
            self.assertEqual(removed, [])
            self.assertTrue(candidate.exists())


if __name__ == "__main__":
    unittest.main()
