import unittest

from yixin.collaboration.auth import hash_password, verify_password


class CollaborationAuthTests(unittest.TestCase):
    def test_password_hash_round_trip(self) -> None:
        encoded = hash_password("StrongPassword1", iterations=1000)
        self.assertTrue(verify_password("StrongPassword1", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))
        self.assertNotIn("StrongPassword1", encoded)

    def test_password_validation(self) -> None:
        with self.assertRaises(ValueError):
            hash_password("short")
        with self.assertRaises(ValueError):
            hash_password("has whitespace")


if __name__ == "__main__":
    unittest.main()
