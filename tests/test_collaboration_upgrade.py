import tempfile
import unittest
from pathlib import Path

from yixin.collaboration.upgrade import prepare_legacy_data
from yixin.paths import AppPaths


class CollaborationUpgradeTests(unittest.TestCase):
    def test_legacy_database_is_backed_up_and_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = AppPaths(Path(temp) / "data")
            paths.ensure()
            paths.database.write_bytes(b"legacy-sqlite")
            archive = prepare_legacy_data(paths)
            self.assertIsNotNone(archive)
            self.assertTrue(archive.is_file())
            self.assertTrue(
                paths.database.with_name("app.db.legacy.bak").is_file()
            )
            self.assertFalse(paths.database.exists())
            self.assertTrue(paths.collaboration_marker.is_file())
            self.assertIsNone(prepare_legacy_data(paths))


if __name__ == "__main__":
    unittest.main()
