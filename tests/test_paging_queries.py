import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from yixin.constants import TASK_FAILED, TASK_SUCCESS
from yixin.database import Database
from yixin.paging import PageRequest, TaskQuery


class PagingQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "paging.db")
        self.database.initialize()
        rows = []
        for index in range(250):
            rows.append(
                (
                    TASK_SUCCESS if index % 2 == 0 else TASK_FAILED,
                    "text",
                    "gemini-3.1-flash-image",
                    f"BENCH 货号 {index:03d}",
                    "3:4",
                    "2K",
                    1,
                    f"2026-10-02T{4 + index % 12:02d}:00:00+00:00",
                    f"2026-10-02T{4 + index % 12:02d}:00:00+00:00",
                )
            )
        with self.database.connection() as connection:
            connection.executemany(
                """
                INSERT INTO tasks(
                    status, mode, model, prompt, aspect_ratio,
                    resolution, n, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_task_page_filters_counts_and_sorts_before_paging(self) -> None:
        result = self.database.list_tasks_page(
            TaskQuery(
                keyword="BENCH",
                statuses=(TASK_SUCCESS,),
                start_date=date(2026, 10, 2),
                end_date=date(2026, 10, 2),
            ),
            PageRequest(
                page=1,
                page_size=100,
                sort_field="created_at",
                sort_direction="asc",
            ),
        )
        self.assertEqual(result.total, 125)
        self.assertEqual(len(result.items), 100)
        self.assertEqual(result.total_pages, 2)
        self.assertTrue(
            all(item["status"] == TASK_SUCCESS for item in result.items)
        )

        final_page = self.database.list_tasks_page(
            TaskQuery(
                keyword="BENCH",
                statuses=(TASK_SUCCESS,),
            ),
            PageRequest(
                page=2,
                page_size=100,
                sort_field="created_at",
                sort_direction="asc",
            ),
        )
        self.assertEqual(len(final_page.items), 25)
        self.assertFalse(
            {
                item["id"] for item in result.items
            }
            & {
                item["id"] for item in final_page.items
            }
        )

    def test_task_result_count_and_paging_indexes(self) -> None:
        result = self.database.list_tasks_page(
            TaskQuery(),
            PageRequest(
                page=1,
                page_size=10,
                sort_field="result_count",
                sort_direction="desc",
            ),
        )
        self.assertEqual(len(result.items), 10)
        self.assertTrue(
            all(item["result_count"] == 0 for item in result.items)
        )
        with self.database.connection() as connection:
            index_names = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA index_list(tasks)"
                ).fetchall()
            }
        self.assertIn("idx_tasks_created_id", index_names)
        self.assertIn("idx_tasks_status_created", index_names)

    def test_page_request_validation(self) -> None:
        with self.assertRaises(ValueError):
            PageRequest(page=0)
        with self.assertRaises(ValueError):
            PageRequest(page_size=0)
        with self.assertRaises(ValueError):
            PageRequest(sort_direction="sideways")

    def test_ten_thousand_tasks_still_return_only_one_page(self) -> None:
        rows = [
            (
                TASK_SUCCESS,
                "text",
                "gemini-3.1-flash-image",
                f"批量性能 {index:05d}",
                "3:4",
                "2K",
                1,
                "2026-10-02T08:00:00+00:00",
                "2026-10-02T08:00:00+00:00",
            )
            for index in range(10_000)
        ]
        with self.database.connection() as connection:
            connection.executemany(
                """
                INSERT INTO tasks(
                    status, mode, model, prompt, aspect_ratio,
                    resolution, n, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        started = time.perf_counter()
        result = self.database.list_tasks_page(
            TaskQuery(keyword="批量性能"),
            PageRequest(page=1, page_size=100),
        )
        elapsed = time.perf_counter() - started
        self.assertEqual(result.total, 10_000)
        self.assertEqual(len(result.items), 100)
        self.assertLess(elapsed, 1.5)


if __name__ == "__main__":
    unittest.main()
