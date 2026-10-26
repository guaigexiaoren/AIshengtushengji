import unittest

from yixin.collaboration.api_keys import decrypt_api_key, encrypt_api_key
from yixin.collaboration.folders import parse_date_folder
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


if __name__ == "__main__":
    unittest.main()
