from __future__ import annotations

import unittest
from datetime import datetime, timezone

from yixin.time_utils import (
    format_elapsed,
    format_task_elapsed,
    local_to_utc_naive,
    utc_naive_to_local,
)


class TimeUtilsTests(unittest.TestCase):
    def test_local_utc_round_trip(self) -> None:
        local_value = datetime(2026, 1, 2, 12, 30, 0)
        utc_value = local_to_utc_naive(local_value)
        self.assertEqual(utc_naive_to_local(utc_value), local_value)

    def test_duration_formats(self) -> None:
        self.assertEqual(format_elapsed(35), "35秒")
        self.assertEqual(format_elapsed(312), "5分12秒")
        self.assertEqual(format_elapsed(3900), "1小时05分")

    def test_completed_and_running_task_elapsed(self) -> None:
        completed = {
            "created_at": "2026-01-01T10:00:00+00:00",
            "finished_at": "2026-01-01T10:05:12+00:00",
        }
        self.assertEqual(format_task_elapsed(completed), "5分12秒")
        running = {
            "created_at": "2026-01-01T10:00:00+00:00",
            "finished_at": None,
        }
        now = datetime(2026, 1, 1, 10, 0, 35, tzinfo=timezone.utc)
        self.assertEqual(format_task_elapsed(running, now=now), "35秒")

    def test_invalid_timestamp_is_unknown(self) -> None:
        self.assertEqual(format_task_elapsed({"created_at": None}), "—")
        self.assertEqual(format_task_elapsed({"created_at": "invalid"}), "—")


if __name__ == "__main__":
    unittest.main()
