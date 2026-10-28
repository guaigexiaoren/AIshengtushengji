import os
import threading
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

from yixin.collaboration.repository import MySQLCentralRepository, MySQLConfig
from yixin.collaboration.reports import ReportFilter
from yixin.collaboration.types import (
    GeneratedResultInput,
    PairingInput,
    ProjectAssetInput,
    ProjectDraft,
    ATTEMPT_QUEUED,
    TASK_APPROVED,
    TASK_CLAIMED,
)


MYSQL_ENV = {
    key: os.environ.get(key)
    for key in (
        "YIXIN_TEST_MYSQL_HOST",
        "YIXIN_TEST_MYSQL_PORT",
        "YIXIN_TEST_MYSQL_DATABASE",
        "YIXIN_TEST_MYSQL_USER",
        "YIXIN_TEST_MYSQL_PASSWORD",
    )
}
RUN_MYSQL = all(MYSQL_ENV.values()) and str(
    MYSQL_ENV["YIXIN_TEST_MYSQL_DATABASE"]
).endswith("_test")


@unittest.skipUnless(
    RUN_MYSQL,
    "需要 YIXIN_TEST_MYSQL_* 环境变量，且测试库名必须以 _test 结尾",
)
class MySQLIntegrationTests(unittest.TestCase):
    def test_concurrent_claim_and_complete_review_loop(self) -> None:
        repository = MySQLCentralRepository(
            MySQLConfig(
                host=str(MYSQL_ENV["YIXIN_TEST_MYSQL_HOST"]),
                port=int(MYSQL_ENV["YIXIN_TEST_MYSQL_PORT"]),
                database=str(MYSQL_ENV["YIXIN_TEST_MYSQL_DATABASE"]),
                user=str(MYSQL_ENV["YIXIN_TEST_MYSQL_USER"]),
                password=str(MYSQL_ENV["YIXIN_TEST_MYSQL_PASSWORD"]),
            )
        )
        repository.ensure_schema()
        users = []
        project_id = None
        try:
            admin = repository.create_initial_admin(
                "_integration_admin",
                "集成管理员",
                "TempAdmin123",
            )
            users.append(admin.id)
            operator = repository.create_user(
                "_integration_ops",
                "集成运营",
                "operations",
                "TempOps123",
                urgent_quota_per_day=2,
                project_access=(("internal", "集成项目"),),
            )
            ai_one = repository.create_user(
                "_integration_ai1",
                "集成制作1",
                "ai",
                "TempAi1123",
            )
            ai_two = repository.create_user(
                "_integration_ai2",
                "集成制作2",
                "ai",
                "TempAi2123",
            )
            users.extend((operator.id, ai_one.id, ai_two.id))
            repository.set_user_policies(
                admin,
                operator.id,
                urgent_quota_per_day=2,
                max_generation_concurrency=2,
            )
            self.assertNotIn(
                "task_center",
                repository.user_menu_permissions(operator.id),
            )
            self.assertEqual(
                repository.list_accessible_projects(operator),
                (("internal", "集成项目"),),
            )
            repository.save_api_key(
                admin,
                ai_one.id,
                "sk-integration-key-123",
            )
            self.assertEqual(
                repository.load_actor_api_key(ai_one),
                "sk-integration-key-123",
            )
            repository.set_model_price(
                admin,
                "gemini-3.1-flash-image",
                Decimal("1.25"),
            )
            project_id = repository.create_project(
                operator,
                ProjectDraft(
                    project_code="",
                    name="集成测试",
                    deadline=datetime.now() + timedelta(days=1),
                    priority="normal",
                    project_relative_root="内部项目/集成项目/_integration_project",
                    pairing_mode="filename",
                    assets=(
                        ProjectAssetInput(
                            "garment",
                            "衣服图/G001.png",
                            "G001.png",
                            "a" * 64,
                            "image/png",
                            10,
                            10,
                        ),
                        ProjectAssetInput(
                            "garment",
                            "衣服图/G002.png",
                            "G002.png",
                            "d" * 64,
                            "image/png",
                            10,
                            10,
                        ),
                        ProjectAssetInput(
                            "garment",
                            "衣服图/G003.png",
                            "G003.png",
                            "e" * 64,
                            "image/png",
                            10,
                            10,
                        ),
                        ProjectAssetInput(
                            "model",
                            "模特图/M001.png",
                            "M001.png",
                            "b" * 64,
                            "image/png",
                            10,
                            10,
                        ),
                        ProjectAssetInput(
                            "model",
                            "模特图/M002.png",
                            "M002.png",
                            "f" * 64,
                            "image/png",
                            10,
                            10,
                        ),
                    ),
                    pairs=(
                        PairingInput(0, 0),
                        PairingInput(1, 0),
                        PairingInput(2, 0),
                        PairingInput(0, 1),
                        PairingInput(1, 1),
                        PairingInput(2, 1),
                    ),
                    source_date=date.today(),
                    product_code="集成货号",
                    project_scope="internal",
                    access_project_name="集成项目",
                ),
            )
            repository.set_project_urgent(project_id, operator, True)
            self.assertTrue(repository.get_project(project_id).is_urgent)
            repository.set_project_urgent(project_id, operator, False)
            self.assertFalse(repository.get_project(project_id).is_urgent)
            repository.set_project_urgent(project_id, operator, True)
            project = repository.get_project(project_id)
            self.assertRegex(
                project.project_code,
                r"^集成运营-集成货号-\d{3,}$",
            )
            project_tasks = repository.list_project_tasks(project_id)
            self.assertEqual(len(project_tasks), 2)
            self.assertTrue(
                all(len(task.garment_assets) == 3 for task in project_tasks)
            )
            task = project_tasks[0]
            repository.update_subtask_sources(
                task.id,
                operator,
                garment_paths=(
                    *(asset.relative_path for asset in task.garment_assets),
                    "衣服图/G004.png",
                ),
                model_path=task.model_path,
                new_assets=(
                    ProjectAssetInput(
                        "garment",
                        "衣服图/G004.png",
                        "G004.png",
                        "0" * 64,
                        "image/png",
                        10,
                        10,
                    ),
                ),
            )
            task = repository.get_task(task.id)
            self.assertEqual(len(task.garment_assets), 4)
            self.assertGreater(task.source_revision, 1)
            queued_task = project_tasks[1]
            outcomes = []

            def claim(actor):
                try:
                    repository.claim_task(task.id, actor)
                    outcomes.append(actor.username)
                except Exception:
                    outcomes.append("conflict")

            threads = [
                threading.Thread(target=claim, args=(ai_one,)),
                threading.Thread(target=claim, args=(ai_two,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(outcomes.count("conflict"), 1)
            winner = ai_one if ai_one.username in outcomes else ai_two

            attempt = repository.create_generation_attempt(
                task.id,
                winner,
                prompt="集成测试提示词",
                model="gemini-3.1-flash-image",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                unit_price=Decimal("1.25"),
            )
            repository.complete_generation_attempt(
                attempt.id,
                winner,
                request_id="integration",
                remote_task_id="integration",
                raw_response={"ok": True},
                input_tokens=1,
                output_tokens=2,
                results=(
                    GeneratedResultInput(
                        0,
                        "AI成品/integration/result.png",
                        "",
                        "c" * 64,
                        "image/png",
                        10,
                        10,
                    ),
                ),
            )
            result_id = repository.list_results(task.id)[0].id
            history = repository.list_project_attempt_history(admin)
            history_row = next(
                row for row in history if int(row["id"]) == attempt.id
            )
            self.assertEqual(history_row["task_code"], task.task_code)
            self.assertEqual(
                history_row["access_project_name"],
                "集成项目",
            )
            self.assertEqual(int(history_row["result_count"]), 1)
            self.assertEqual(
                len(repository.list_attempt_results(attempt.id)),
                1,
            )
            repository.submit_for_acceptance(
                task.id,
                winner,
                final_result_ids=(result_id,),
                final_quantity=1,
            )
            repository.review_task(task.id, operator, approved=True)
            self.assertEqual(repository.get_task(task.id).status, TASK_APPROVED)
            self.assertEqual(
                repository.report(ReportFilter()).summary.total_cost,
                Decimal("1.2500"),
            )
            queue_outcomes = []

            def claim_and_queue(actor):
                try:
                    attempt = repository.claim_and_queue_generation(
                        queued_task.id,
                        actor,
                        prompt="自动领取并排队",
                        model="gemini-3.1-flash-image",
                        aspect_ratio="3:4",
                        resolution="2K",
                        n=1,
                        unit_price=Decimal("1.25"),
                    )
                    queue_outcomes.append((actor, attempt))
                except Exception:
                    queue_outcomes.append((actor, None))

            queue_threads = [
                threading.Thread(target=claim_and_queue, args=(ai_one,)),
                threading.Thread(target=claim_and_queue, args=(ai_two,)),
            ]
            for thread in queue_threads:
                thread.start()
            for thread in queue_threads:
                thread.join()
            successful = [
                item for item in queue_outcomes if item[1] is not None
            ]
            self.assertEqual(len(successful), 1)
            queue_winner, queued_attempt = successful[0]
            claimed_task = repository.get_task(queued_task.id)
            self.assertEqual(claimed_task.status, TASK_CLAIMED)
            self.assertEqual(claimed_task.assignee_id, queue_winner.id)
            self.assertEqual(queued_attempt.status, ATTEMPT_QUEUED)
            self.assertEqual(queued_attempt.operator_id, queue_winner.id)
            skipped_count = repository.skip_remaining_subtasks(
                project_id,
                admin,
                reason="实际只需完成一个子任务",
            )
            self.assertEqual(skipped_count, 1)
            self.assertEqual(
                repository.get_task(queued_task.id).status,
                "skipped",
            )
            self.assertEqual(
                repository.get_project(project_id).status,
                "completed",
            )
        finally:
            with repository.connection() as connection:
                with connection.cursor() as cursor:
                    if project_id:
                        cursor.execute(
                            "DELETE FROM project_tasks WHERE project_id = %s",
                            (project_id,),
                        )
                        cursor.execute(
                            "DELETE FROM project_assets WHERE project_id = %s",
                            (project_id,),
                        )
                        cursor.execute(
                            "DELETE FROM projects WHERE id = %s",
                            (project_id,),
                        )
                    if users:
                        placeholders = ", ".join(["%s"] * len(users))
                        cursor.execute(
                            f"DELETE FROM user_api_keys "
                            f"WHERE user_id IN ({placeholders}) "
                            f"OR updated_by IN ({placeholders})",
                            tuple(users) + tuple(users),
                        )
                        cursor.execute(
                            f"DELETE FROM model_prices WHERE created_by IN ({placeholders})",
                            tuple(users),
                        )
                        cursor.execute(
                            f"DELETE FROM users WHERE id IN ({placeholders})",
                            tuple(users),
                        )


if __name__ == "__main__":
    unittest.main()
