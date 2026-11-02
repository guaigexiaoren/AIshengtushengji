from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yixin.paths import AppPaths
from yixin.storage import (
    FileStore,
    ImageValidationError,
    data_url_for_file,
    sha256_file,
)
from PIL import Image
from tests.helpers import make_image, png_bytes


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp.name) / "data root")
        self.paths.ensure()
        self.store = FileStore(self.paths)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reference_copy_and_hash_are_stable(self) -> None:
        source = make_image(Path(self.temp.name) / "source.png")
        first = self.store.save_reference(source)
        second = self.store.save_reference(source)
        self.assertEqual(first.path, second.path)
        self.assertEqual(first.content_hash, sha256_file(source))
        self.assertEqual(first.mime, "image/png")
        self.assertTrue(first.path.is_file())
        self.assertTrue(data_url_for_file(first.path).startswith("data:image/png;base64,"))

    def test_invalid_image_is_rejected(self) -> None:
        source = Path(self.temp.name) / "broken.png"
        source.write_bytes(b"not an image")
        with self.assertRaises(ImageValidationError):
            self.store.save_reference(source)

    def test_generated_image_and_thumbnail(self) -> None:
        generated = self.store.store_generated(9, png_bytes(), 0)
        self.assertTrue(generated.path.is_file())
        self.assertEqual(generated.width, 64)
        thumbnail = self.store.create_thumbnail(generated.path)
        self.assertTrue(thumbnail.is_file())
        self.assertEqual(thumbnail.suffix.lower(), ".jpg")
        with self.assertRaises(ImageValidationError):
            self.store.store_generated(9, b"not image", 1)

    def test_cleanup_only_removes_old_unprotected_files(self) -> None:
        old = self.paths.temp / "inputs" / "old.png"
        recent = self.paths.temp / "inputs" / "recent.png"
        protected = self.paths.temp / "inputs" / "protected.png"
        old.parent.mkdir(parents=True, exist_ok=True)
        for path in (old, recent, protected):
            path.write_bytes(b"x")
        old_time = time.time() - 8 * 24 * 60 * 60
        os.utime(old, (old_time, old_time))
        os.utime(protected, (old_time, old_time))
        removed = self.store.cleanup_temp(
            [protected],
            days=7,
            now=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        self.assertIn(old.resolve(), {path.resolve() for path in removed})
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(protected.exists())

    def test_reference_crop_is_non_destructive_and_preserves_transparency(self) -> None:
        source = Path(self.temp.name) / "transparent.png"
        image = Image.new("RGBA", (120, 90), (20, 50, 90, 140))
        image.putpixel((15, 15), (255, 0, 0, 0))
        image.save(source)
        original_hash = sha256_file(source)
        reference = self.store.save_reference(source)
        cropped = self.store.save_cropped_reference(reference, (10, 15, 90, 75))
        self.assertEqual((cropped.width, cropped.height), (80, 60))
        self.assertEqual(cropped.mime, "image/png")
        self.assertEqual(cropped.source_path, source.resolve())
        self.assertEqual(sha256_file(source), original_hash)
        with Image.open(cropped.path) as result:
            self.assertEqual(result.size, (80, 60))
            self.assertIn("A", result.getbands())
        duplicate = self.store.save_cropped_reference(reference, (10, 15, 90, 75))
        self.assertEqual(duplicate.path, cropped.path)


if __name__ == "__main__":
    unittest.main()
