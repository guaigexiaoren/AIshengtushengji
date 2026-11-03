from contextlib import contextmanager
from datetime import date, datetime
import unittest

from yixin.collaboration.repository import (
    MySQLCentralRepository,
    MySQLConfig,
)
from yixin.collaboration.types import (
    ROLE_ADMIN,
    AuthUser,
    PairingInput,
    ProjectAssetInput,
    ProjectDraft,
)


class RecordingCursor:
    def __init__(self) -> None:
        self.closed = False
        self.executed = []
        self.lastrowid = 9

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def execute(self, sql, params=()):
        if self.closed:
            raise AssertionError("cursor used after close")
        self.executed.append((sql, params))
        return self

    def fetchone(self):
        return None


class RecordingConnection:
    def __init__(self) -> None:
        self.recording_cursor = RecordingCursor()

    def cursor(self):
        return self.recording_cursor


class RepositoryGuardTests(unittest.TestCase):
    def test_create_user_uses_one_open_transaction_cursor(self) -> None:
        repository = MySQLCentralRepository(
            MySQLConfig("localhost", 3306, "test", "user", "password")
        )
        connection = RecordingConnection()

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        actor = AuthUser(1, "admin", "管理员", ROLE_ADMIN)
        user = repository.create_user(
            actor,
            "operator",
            "运营A",
            "operations",
            "TempPass123",
            menu_permissions=("new_task",),
            project_access=(("internal", "春季项目"),),
        )
        self.assertEqual(user.id, 9)
        self.assertEqual(user.menu_permissions, ("new_task",))
        sql_text = " ".join(
            sql for sql, _params in connection.recording_cursor.executed
        )
        self.assertIn("UPDATE users", sql_text)
        self.assertIn("INSERT INTO user_menu_permissions", sql_text)
        self.assertIn("INSERT INTO user_project_access", sql_text)

    def test_draft_rejects_more_than_thirteen_garments_per_model(
        self,
    ) -> None:
        assets = tuple(
            ProjectAssetInput(
                "garment",
                f"衣服图/g{index}.png",
                f"g{index}.png",
                f"{index:064x}",
                "image/png",
                10,
                10,
            )
            for index in range(14)
        ) + (
            ProjectAssetInput(
                "model",
                "模特图/m.png",
                "m.png",
                "f" * 64,
                "image/png",
                10,
                10,
            ),
        )
        draft = ProjectDraft(
            project_code="",
            name="测试",
            deadline=datetime.now(),
            priority="normal",
            project_relative_root="内部项目/测试/货号",
            pairing_mode="all",
            assets=assets,
            pairs=tuple(PairingInput(index, 0) for index in range(14)),
            source_date=date.today(),
            product_code="货号",
            project_scope="internal",
            access_project_name="测试",
        )
        with self.assertRaisesRegex(ValueError, "13"):
            MySQLCentralRepository._validate_draft(draft)

    def test_ai_user_can_create_project(self) -> None:
        repository = MySQLCentralRepository(
            MySQLConfig("localhost", 3306, "test", "user", "password")
        )
        connection = RecordingConnection()

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        actor = AuthUser(4, "ai_user", "AI制作", "ai")
        draft = ProjectDraft(
            project_code="AI制作-货号-001",
            name="货号",
            deadline=datetime.now(),
            priority="normal",
            project_relative_root="内部项目/测试/货号",
            pairing_mode="all",
            assets=(
                ProjectAssetInput(
                    "garment",
                    "衣服图/g.png",
                    "g.png",
                    "a" * 64,
                    "image/png",
                    10,
                    10,
                ),
                ProjectAssetInput(
                    "model",
                    "模特图/m.png",
                    "m.png",
                    "b" * 64,
                    "image/png",
                    10,
                    10,
                ),
            ),
            pairs=(PairingInput(0, 0),),
            source_date=date.today(),
            product_code="货号",
            project_scope="internal",
            access_project_name="测试",
        )
        self.assertEqual(repository.create_project(actor, draft), 9)


if __name__ == "__main__":
    unittest.main()
