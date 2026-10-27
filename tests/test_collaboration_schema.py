import unittest
from pathlib import Path


class CollaborationSchemaTests(unittest.TestCase):
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
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", schema)
        for column in (
            "project_scope",
            "project_name",
            "is_urgent",
            "urgent_date",
            "urgent_by",
        ):
            self.assertIn(column, schema)
        self.assertIn("utf8mb4", schema)
        self.assertIn("FOREIGN KEY", schema)


if __name__ == "__main__":
    unittest.main()
