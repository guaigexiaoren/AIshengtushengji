import unittest
from datetime import date
from pathlib import Path

from yixin.collaboration.api_keys import decrypt_api_key, encrypt_api_key
from yixin.collaboration.folders import (
    parse_date_folder,
    resolve_account_directory,
)
class ApiKeyAndFolderTests(unittest.TestCase):
    def test_api_key_encryption_round_trip(self) -> None:
        encrypted = encrypt_api_key("sk-private-value-123456")
        self.assertNotIn("sk-private-value", encrypted)
        self.assertEqual(
            decrypt_api_key(encrypted),
            "sk-private-value-123456",
        )

    def test_date_folder_formats(self) -> None:
        self.assertIsNotNone(parse_date_folder("12.26"))
        self.assertIsNotNone(parse_date_folder("2026.12.26"))
        self.assertIsNone(parse_date_folder("项目A"))

    def test_custom_account_directory_uses_last_date_or_today(self) -> None:
        root = Path("/tmp/share")
        recognized = resolve_account_directory(
            root / "项目A" / "2026.12.26",
            root,
            today=date(2026, 1, 1),
        )
        self.assertEqual(recognized.source_date, date(2026, 12, 26))
        self.assertTrue(recognized.date_recognized)
        fallback = resolve_account_directory(
            root / "项目A" / "临时目录",
            root,
            today=date(2026, 1, 1),
        )
        self.assertEqual(fallback.source_date, date(2026, 1, 1))
        self.assertFalse(fallback.date_recognized)


if __name__ == "__main__":
    unittest.main()
