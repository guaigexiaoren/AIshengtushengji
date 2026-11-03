import unittest

from yixin.collaboration.auth import (
    hash_password,
    validate_new_password,
    verify_password,
)


class CollaborationAuthTests(unittest.TestCase):
    def test_password_hash_round_trip(self) -> None:
        encoded = hash_password("StrongPassword1", iterations=1000)
        self.assertTrue(verify_password("StrongPassword1", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))
        self.assertNotIn("StrongPassword1", encoded)

    def test_password_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "密码至少需要 6 个字符"):
            hash_password("short")
        with self.assertRaises(ValueError):
            hash_password("has whitespace")

    def test_password_minimum_length_boundary(self) -> None:
        with self.assertRaises(ValueError):
            validate_new_password("12345")

        for password in ("123456", "1234567"):
            self.assertEqual(validate_new_password(password), password)
            encoded = hash_password(password, iterations=1000)
            self.assertTrue(verify_password(password, encoded))


if __name__ == "__main__":
    unittest.main()
