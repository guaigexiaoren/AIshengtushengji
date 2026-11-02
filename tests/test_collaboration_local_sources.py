import tempfile
import unittest
import base64
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PIL import Image

from yixin.collaboration.local_sources import LocalTaskSourceStore
from yixin.collaboration.shared_paths import SharedPathResolver
from yixin.collaboration.repository import StateConflictError
from yixin.collaboration.types import (
    ATTEMPT_CANCELLED,
    ATTEMPT_QUEUED,
    AuthUser,
    GenerationAttempt,
    ProjectAssetInput,
    ProjectTask,
)
from yixin.collaboration.worker import ProjectGenerationWorker
from yixin.models import ImageApiResult, ImageRequest, RemoteImage
from yixin.paths import AppPaths
from yixin.storage import FileStore
from tests.helpers import png_bytes


def make_task(root: Path) -> ProjectTask:
    project = root / "项目" / "123"
    (project / "衣服图").mkdir(parents=True)
    (project / "模特图").mkdir(parents=True)
    Image.new("RGB", (20, 20), "red").save(
        project / "衣服图" / "衣服1.png"
    )
    Image.new("RGB", (20, 20), "blue").save(
        project / "模特图" / "模特1.png"
    )
    return ProjectTask(
        id=11,
        task_code="P-001",
        project_id=1,
        project_code="P",
        project_name="123",
        project_relative_root="项目/123",
        garment_asset_id=1,
        garment_path="衣服图/衣服1.png",
        garment_name="衣服1.png",
        garment_hash="a" * 64,
        model_asset_id=2,
        model_path="模特图/模特1.png",
        model_name="模特1.png",
        model_hash="b" * 64,
        status="working",
        assignee_id=7,
        assignee_name="AI甲",
        prompt_text="",
        model="",
        aspect_ratio="3:4",
        resolution="2K",
        n=1,
        final_quantity=0,
        owner_id=3,
        created_at=datetime.now(),
        source_revision=1,
        garment_assets=(
            ProjectAssetInput(
                "garment",
                "衣服图/衣服1.png",
                "衣服1.png",
                "a" * 64,
                "image/png",
                20,
                20,
            ),
        ),
    )


class LocalTaskSourceTests(unittest.TestCase):
    def test_local_sources_persist_and_attempt_snapshot_survives(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = make_task(root / "share")
            extra = root / "新增衣服.png"
            Image.new("RGB", (30, 30), "green").save(extra)
            store = LocalTaskSourceStore(
                root / "local",
                7,
                task,
                SharedPathResolver(root / "share"),
            )
            self.assertEqual(len(store.state.garments), 1)
            store.add_paths("garment", [extra])
            store.save_attempt_snapshot(19)
            reloaded = LocalTaskSourceStore(
                root / "local",
                7,
                task,
                SharedPathResolver(root / "share"),
            )
            self.assertEqual(len(reloaded.state.garments), 2)
            self.assertIsNotNone(reloaded.state.model)
            snapshot = reloaded.load_attempt_snapshot(19)
            self.assertIsNotNone(snapshot)
            self.assertEqual(len(snapshot), 3)

    def test_source_revision_change_resets_local_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = make_task(root / "share")
            extra = root / "新增衣服.png"
            Image.new("RGB", (30, 30), "green").save(extra)
            store = LocalTaskSourceStore(
                root / "local",
                7,
                task,
                SharedPathResolver(root / "share"),
            )
            store.add_paths("garment", [extra])
            store.save_attempt_snapshot(22)
            changed = replace(task, source_revision=2)
            reloaded = LocalTaskSourceStore(
                root / "local",
                7,
                changed,
                SharedPathResolver(root / "share"),
            )
            self.assertEqual(reloaded.state.source_revision, 2)
            self.assertEqual(len(reloaded.state.garments), 1)
            snapshot = reloaded.load_attempt_snapshot(22)
            self.assertIsNotNone(snapshot)
            self.assertEqual(len(snapshot), 3)

    def test_crop_can_be_restored_from_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = make_task(root / "share")
            resolver = SharedPathResolver(root / "share")
            paths = AppPaths(root / "data")
            paths.ensure()
            file_store = FileStore(paths)
            store = LocalTaskSourceStore(root / "local", 7, task, resolver)
            original_hash = store.state.garments[0].content_hash
            cropped = file_store.save_cropped_reference(
                store.state.garments[0],
                (0, 0, 10, 10),
            )
            store.replace_image("garment", 0, cropped)
            self.assertNotEqual(
                store.state.garments[0].content_hash,
                original_hash,
            )
            store.restore_image("garment", 0)
            self.assertEqual(
                store.state.garments[0].content_hash,
                original_hash,
            )

    def test_generation_worker_uses_local_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = make_task(root / "share")
            resolver = SharedPathResolver(root / "share")
            store = LocalTaskSourceStore(
                root / "local",
                7,
                task,
                resolver,
            )
            references = store.state.references()
            attempt = GenerationAttempt(
                id=31,
                task_id=task.id,
                attempt_no=1,
                operator_id=7,
                model="gemini-3.1-flash-image",
                prompt_text="测试",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                status=ATTEMPT_QUEUED,
                source_revision=1,
                unit_price=0,
                generated_image_count=0,
            )

            class Repository:
                def get_task(self, _task_id):
                    return task

                def start_queued_generation_attempt(self, *_args):
                    return attempt

                def current_model_price(self, _model):
                    return 0

                def complete_generation_attempt(self, *_args, **_kwargs):
                    return None

                def fail_generation_attempt(self, *_args, **_kwargs):
                    return None

            class Client:
                received = None
                image_bytes = png_bytes((70, 80, 90), (20, 20))

                def generate(self, request: ImageRequest):
                    self.received = request
                    return ImageApiResult(
                        images=(
                            RemoteImage(
                                b64_json=base64.b64encode(
                                    self.image_bytes
                                ).decode("ascii")
                            ),
                        ),
                        request_id="local-test",
                        remote_task_id="",
                        usage_input_tokens=1,
                        usage_output_tokens=2,
                        raw_response={},
                        status_code=200,
                    )

                @staticmethod
                def resolve_image(_image):
                    return Client.image_bytes

            client = Client()
            paths = AppPaths(root / "data")
            paths.ensure()
            worker = ProjectGenerationWorker(
                repository=Repository(),
                client=client,
                resolver=resolver,
                task=task,
                actor=AuthUser(7, "ai", "AI甲", "ai"),
                prompt="测试",
                model="gemini-3.1-flash-image",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                local_references=references,
                reserved_attempt=attempt,
            )
            worker._run()
            self.assertEqual(
                tuple(image.path for image in client.received.reference_images),
                tuple(image.path for image in references),
            )
            output = root / "share" / "项目" / "123" / "AI成品"
            files = [path for path in output.iterdir() if path.is_file()]
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0].name, r"^_r001_01_[0-9a-f]{12}\.png$")

    def test_cancelled_queued_attempt_exits_without_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = make_task(root / "share")
            attempt = GenerationAttempt(
                id=32,
                task_id=task.id,
                attempt_no=1,
                operator_id=7,
                model="gemini-3.1-flash-image",
                prompt_text="取消竞态",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                status=ATTEMPT_QUEUED,
                source_revision=1,
                unit_price=0,
                generated_image_count=0,
            )

            class Repository:
                def get_task(self, _task_id):
                    return task

                def start_queued_generation_attempt(self, *_args):
                    raise StateConflictError("已取消")

                def current_model_price(self, _model):
                    return 0

                def generation_attempt_status(self, _attempt_id):
                    return ATTEMPT_CANCELLED

                def fail_generation_attempt(self, *_args, **_kwargs):
                    raise AssertionError("不应记录失败")

            finished = []
            failed = []
            worker = ProjectGenerationWorker(
                repository=Repository(),
                client=object(),
                resolver=SharedPathResolver(root / "share"),
                task=task,
                actor=AuthUser(7, "ai", "AI甲", "ai"),
                prompt="取消竞态",
                model="gemini-3.1-flash-image",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                reserved_attempt=attempt,
            )
            worker.signals.finished.connect(
                lambda task_id, attempt_id: finished.append(
                    (task_id, attempt_id)
                )
            )
            worker.signals.failed.connect(
                lambda task_id, message, attempt_id: failed.append(
                    (task_id, message, attempt_id)
                )
            )
            worker._run()
            self.assertEqual(finished, [(task.id, attempt.id)])
            self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
