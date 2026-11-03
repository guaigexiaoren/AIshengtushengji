import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from yixin.collaboration.reports import (
    ReportTaskRow,
    aggregate_report,
    export_report_excel,
)
from yixin.collaboration.types import TASK_APPROVED, TASK_AWAITING_ACCEPTANCE


class CollaborationReportTests(unittest.TestCase):
    def test_production_time_falls_back_to_approval_time(self) -> None:
        start = datetime(2026, 1, 1, 9, 0, 0)
        row = ReportTaskRow(
            project_id=1,
            project_code="P001",
            project_name="春季",
            project_status="completed",
            priority="normal",
            task_id=1,
            task_code="P001-0001",
            task_status=TASK_APPROVED,
            owner_name="运营A",
            assignee_name="制作A",
            model="model-a",
            garment_name="G001",
            model_name="M001",
            final_quantity=1,
            rework_count=0,
            generated_image_count=1,
            generation_cost=Decimal("1.00"),
            project_created_at=start,
            claimed_at=start + timedelta(hours=1),
            work_started_at=start + timedelta(hours=1),
            submitted_at=None,
            approved_at=start + timedelta(hours=3),
            returned_at=None,
        )
        report = aggregate_report([row])
        self.assertEqual(
            report.summary.average_production_seconds,
            2 * 60 * 60,
        )

    def test_summary_and_excel_export(self) -> None:
        start = datetime(2026, 1, 1, 9, 0, 0)
        rows = [
            ReportTaskRow(
                project_id=1,
                project_code="P001",
                project_name="春季",
                project_status="completed",
                priority="high",
                task_id=1,
                task_code="P001-0001",
                task_status=TASK_APPROVED,
                owner_name="运营A",
                assignee_name="制作A",
                model="model-a",
                garment_name="G001",
                model_name="M001",
                final_quantity=2,
                rework_count=1,
                generated_image_count=6,
                generation_cost=Decimal("6.00"),
                project_created_at=start,
                claimed_at=start + timedelta(hours=1),
                work_started_at=start + timedelta(hours=1),
                submitted_at=start + timedelta(hours=3),
                approved_at=start + timedelta(hours=4),
                returned_at=start + timedelta(hours=2),
                manual_image_count=2,
            ),
            ReportTaskRow(
                project_id=1,
                project_code="P001",
                project_name="春季",
                project_status="awaiting_acceptance",
                priority="high",
                task_id=2,
                task_code="P001-0002",
                task_status=TASK_AWAITING_ACCEPTANCE,
                owner_name="运营A",
                assignee_name="制作B",
                model="model-a",
                garment_name="G002",
                model_name="M002",
                final_quantity=0,
                rework_count=0,
                generated_image_count=3,
                generation_cost=Decimal("3.00"),
                project_created_at=start,
                claimed_at=start + timedelta(hours=2),
                work_started_at=start + timedelta(hours=2),
                submitted_at=start + timedelta(hours=5),
                approved_at=None,
                returned_at=None,
            ),
        ]
        report = aggregate_report(rows)
        self.assertEqual(report.summary.project_count, 1)
        self.assertEqual(report.summary.task_count, 2)
        self.assertEqual(report.summary.approved_task_count, 1)
        self.assertEqual(report.summary.final_image_count, 2)
        self.assertEqual(report.summary.rework_count, 1)
        self.assertEqual(report.summary.manual_image_count, 2)
        self.assertEqual(report.summary.total_cost, Decimal("9.00"))
        self.assertEqual(len(report.by_user), 2)

        with tempfile.TemporaryDirectory() as temp:
            path = export_report_excel(report, Path(temp) / "report.xlsx")
            workbook = load_workbook(path, read_only=True)
            self.assertEqual(
                workbook.sheetnames,
                ["汇总", "任务明细", "人员绩效", "成本明细", "趋势"],
            )
            self.assertEqual(workbook["汇总"]["B2"].value, 1)
            workbook.close()


if __name__ == "__main__":
    unittest.main()
