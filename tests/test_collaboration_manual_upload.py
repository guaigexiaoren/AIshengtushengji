import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from PIL import Image

from yixin.collaboration.manual_upload import upload_manual_candidates
from yixin.collaboration.shared_paths import SharedPathResolver
from yixin.collaboration.types import (
    ATTEMPT_RUNNING,
    AuthUser,
    GenerationAttempt,
    ProjectAssetInput,
    ProjectTask,
    ROLE_AI,
)
from yixin.paths import AppPaths
from yixin.storage import FileStore


class FakeManualRepository:
    def __init__(self, task: ProjectTask) -> None:
        self.task = task
        self.completed = None

    def create_manual_attempt(self, _task_id, actor):
        return GenerationAttempt(
            id=7,
            task_id=self.task.id,
            attempt_no=2,
            operator_id=actor.id,
            model="manual_ps",
            prompt_text="",
            aspect_ratio="3:4",
            resolution="2K",
            n=0,
            status=ATTEMPT_RUNNING,
            source_revision=1,
            unit_price=Decimal("0"),
            generated_image_count=0,
            created_at=datetime.now(),
        )

    def complete_generation_attempt(
        self,
        attempt_id,
        actor,
        **kwargs,
    ):
        self.completed = (attempt_id, actor, kwargs)

    def fail_generation_attempt(self, *_args, **_kwargs):
        pass


class ManualUploadTests(unittest.TestCase):
    def test_manual_ps_images_are_recorded_as_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "share"
            project = root / "项目" / "12.26" / "123"
            project.mkdir(parents=True)
            task = ProjectTask(
                id=1,
                task_code="项目-123-001-01",
                project_id=1,
                project_code="项目-123-001",
                project_name="123",
                project_relative_root="项目/12.26/123",
                garment_asset_id=1,
                garment_path="衣服图/G001.png",
                garment_name="G001.png",
                garment_hash="a" * 64,
                model_asset_id=2,
                model_path="模特图/M001.png",
                model_name="M001.png",
                model_hash="b" * 64,
                status="claimed",
                assignee_id=2,
                assignee_name="制作A",
                prompt_text="",
                model="",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                final_quantity=0,
                owner_id=3,
                created_at=datetime.now(),
                garment_assets=(
                    ProjectAssetInput(
                        "garment",
                        "衣服图/G001.png",
                        "G001.png",
                        "a" * 64,
                        "image/png",
                        10,
                        10,
                    ),
                ),
            )
            source_one = Path(temp) / "ps1.png"
            source_two = Path(temp) / "ps2.png"
            Image.new("RGB", (30, 40), "red").save(source_one)
            Image.new("RGB", (30, 40), "blue").save(source_two)
            repository = FakeManualRepository(task)
            paths = AppPaths(Path(temp) / "data")
            paths.ensure()
            count = upload_manual_candidates(
                repository,
                SharedPathResolver(root),
                FileStore(paths),
                task,
                AuthUser(2, "ai", "制作A", ROLE_AI),
                [source_one, source_two],
            )
            self.assertEqual(count, 2)
            self.assertEqual(repository.completed[2]["source_type"], "manual_ps")
            results = repository.completed[2]["results"]
            self.assertEqual(len(results), 2)
            self.assertTrue(
                all("manual" in result.relative_path for result in results)
            )


if __name__ == "__main__":
    unittest.main()
