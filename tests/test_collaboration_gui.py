import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None

if HAS_PYSIDE:
    from PySide6.QtWidgets import QApplication

    from yixin.collaboration.shared_paths import SharedPathResolver
    from yixin.collaboration.types import (
        GenerationResult,
        ProjectTask,
    )
    from yixin.ui.result_gallery import ResultGallery


@unittest.skipUnless(HAS_PYSIDE, "PySide6 未安装")
class CollaborationGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_result_gallery_marks_selected_final_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "项目"
            output = project / "AI成品" / "P001" / "P001-0001" / "attempt_001"
            output.mkdir(parents=True)
            image_path = output / "result.png"
            Image.new("RGB", (100, 120), "blue").save(image_path)
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
                status="awaiting_acceptance",
                assignee_id=3,
                assignee_name="制作A",
                prompt_text="test",
                model="model",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                final_quantity=1,
                owner_id=2,
                created_at=datetime.now(),
            )
            result = GenerationResult(
                id=10,
                attempt_id=1,
                result_index=0,
                relative_path=image_path.relative_to(project).as_posix(),
                thumbnail_relative_path=image_path.relative_to(project).as_posix(),
                content_hash="a" * 64,
                mime="image/png",
                width=100,
                height=120,
                is_final=True,
            )
            stale_result = GenerationResult(
                id=11,
                attempt_id=2,
                result_index=0,
                relative_path=image_path.relative_to(project).as_posix(),
                thumbnail_relative_path=image_path.relative_to(project).as_posix(),
                content_hash="b" * 64,
                mime="image/png",
                width=100,
                height=120,
                is_final=True,
                source_type="manual_ps",
                source_revision=2,
            )
            gallery = ResultGallery(
                SharedPathResolver(root),
                checkable=True,
            )
            gallery.set_task(task, [result, stale_result])
            self.assertEqual(gallery.list_widget.count(), 2)
            self.assertEqual(gallery.selected_result_ids(), [10])
            gallery.deleteLater()


if __name__ == "__main__":
    unittest.main()
