import unittest
from pathlib import Path
from unittest.mock import patch

from yixin.collaboration.repository import (
    MySQLCentralRepository,
    SCHEMA_VERSION,
)
from yixin.collaboration.types import (
    MENU_KEYS,
    ROLE_AI,
    ROLE_DEFAULT_MENUS,
    TASK_STATUS_LABELS,
)


class CollaborationSchemaTests(unittest.TestCase):
    def test_schema_version_is_eleven(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 11)

    def test_schema_v10_migrates_active_identity_uniqueness(self) -> None:
        with (
            patch(
                "yixin.collaboration.repository._add_column_if_missing"
            ) as add_column,
            patch(
                "yixin.collaboration.repository._add_index_if_missing"
            ) as add_index,
            patch(
                "yixin.collaboration.repository._drop_index_if_exists"
            ) as drop_index,
        ):
            MySQLCentralRepository._apply_schema_v10(object())

        self.assertEqual(
            [call.args[2] for call in add_column.call_args_list],
            ["deleted_at", "active_username", "active_display_name"],
        )
        self.assertEqual(
            [call.args[2] for call in drop_index.call_args_list],
            ["username", "uk_users_display_name"],
        )
        self.assertEqual(
            [call.args[2] for call in add_index.call_args_list],
            [
                "uk_users_active_username",
                "uk_users_active_display_name",
            ],
        )

    def test_schema_v11_adds_reports_for_existing_admins(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.executed = []
                self.version = 10

            def execute(self, sql, params=()):
                self.executed.append((sql, params))
                return self

            def fetchone(self):
                return {"version": self.version}

        cursor = Cursor()
        MySQLCentralRepository._apply_schema_v11(cursor)
        self.assertIn(
            "INSERT IGNORE INTO user_menu_permissions",
            cursor.executed[-1][0],
        )
        self.assertEqual(cursor.executed[-1][1], ("admin",))

    def test_required_tables_are_declared(self) -> None:
        schema = (
            Path(__file__).resolve().parent.parent
            / "yixin"
            / "collaboration"
            / "schema.sql"
        ).read_text(encoding="utf-8")
        for table in (
            "users",
            "projects",
            "project_assets",
            "project_tasks",
            "generation_attempts",
            "generation_results",
            "task_reviews",
            "workflow_events",
            "prompt_templates",
            "model_prices",
            "shared_settings",
            "schema_migrations",
            "task_garments",
            "task_code_sequences",
            "user_api_keys",
            "user_menu_permissions",
            "user_project_access",
            "api_providers",
            "api_provider_models",
            "generation_provider_calls",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", schema)
        for column in (
            "project_scope",
            "project_name",
            "is_urgent",
            "urgent_date",
            "urgent_by",
            "provider_id",
            "provider_model",
            "default_protocol",
            "parent_task_id",
            "split_at",
            "requested_provider_code",
            "requested_provider_name",
            "deleted_at",
            "active_username",
            "active_display_name",
        ):
            self.assertIn(column, schema)
        self.assertIn("utf8mb4", schema)
        self.assertIn("FOREIGN KEY", schema)
        for index in (
            "uk_users_active_username",
            "uk_users_active_display_name",
            "idx_projects_scope_name_date",
            "idx_project_tasks_project_status_created",
            "idx_project_tasks_assignee_status_created",
            "idx_attempts_created_id",
        ):
            self.assertIn(index, schema)

    def test_new_workflow_menu_and_status_labels(self) -> None:
        self.assertEqual(MENU_KEYS["new_task"], "任务提交")
        self.assertEqual(MENU_KEYS["submission_history"], "提交记录")
        self.assertEqual(MENU_KEYS["assign_tasks"], "待分配")
        self.assertEqual(MENU_KEYS["ai_generate"], "AI生图")
        self.assertEqual(MENU_KEYS["reports"], "经营报告")
        self.assertNotIn("submitted", MENU_KEYS)
        self.assertNotIn("pending", MENU_KEYS)
        self.assertNotIn("completed", MENU_KEYS)
        self.assertEqual(TASK_STATUS_LABELS["expanded"], "已拆分")
        self.assertIn(
            "submission_history",
            ROLE_DEFAULT_MENUS["operations"],
        )
        self.assertNotIn(
            "reports",
            ROLE_DEFAULT_MENUS[ROLE_AI],
        )
        self.assertIn(
            "reports",
            ROLE_DEFAULT_MENUS["admin"],
        )
        self.assertNotIn(
            "submission_history",
            ROLE_DEFAULT_MENUS[ROLE_AI],
        )


if __name__ == "__main__":
    unittest.main()
