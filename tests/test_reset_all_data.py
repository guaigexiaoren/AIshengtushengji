import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import reset_all_data
from yixin.paths import AppPaths


class ResetAllDataTests(unittest.TestCase):
    def test_clear_local_data_removes_contents_and_recreates_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = AppPaths(Path(temp) / "data")
            paths.ensure()
            (paths.database).write_bytes(b"sqlite")
            (paths.library / "work.png").write_bytes(b"image")
            (paths.config / "secret.bin").write_bytes(b"secret")
            reset_all_data.clear_local_data(paths)
            self.assertEqual(
                set(paths.root.iterdir()),
                {paths.config, paths.temp, paths.library, paths.logs},
            )
            self.assertFalse(paths.database.exists())
            self.assertEqual(list(paths.config.iterdir()), [])
            self.assertEqual(list(paths.library.iterdir()), [])

    def test_clear_mysql_keeps_schema_migrations(self) -> None:
        executed = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, sql, params=()):
                executed.append(sql)
                return self

        class Connection:
            def cursor(self):
                return Cursor()

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        with patch(
            "tools.reset_all_data._connection",
            return_value=Connection(),
        ):
            reset_all_data.clear_mysql()
        self.assertNotIn("schema_migrations", reset_all_data.DATA_TABLES)
        for table in reset_all_data.DATA_TABLES:
            self.assertIn(f"TRUNCATE TABLE `{table}`", executed)


if __name__ == "__main__":
    unittest.main()
