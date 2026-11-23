from __future__ import annotations

import unittest

from yixin.collaboration.listing import extract_listing_id


class ListingIdTests(unittest.TestCase):
    def test_reads_query_parameter(self) -> None:
        self.assertEqual(
            extract_listing_id(
                "https://shop.example.com/detail?goods_id=12345678&from=x"
            ),
            "12345678",
        )

    def test_reads_camel_case_query_parameter(self) -> None:
        self.assertEqual(
            extract_listing_id(
                "https://shop.example.com/detail?itemId=ABC999"
            ),
            "ABC999",
        )

    def test_reads_numeric_path_segment(self) -> None:
        self.assertEqual(
            extract_listing_id(
                "https://shop.example.com/item/1234567890"
            ),
            "1234567890",
        )

    def test_reads_last_path_segment(self) -> None:
        self.assertEqual(
            extract_listing_id(
                "https://shop.example.com/p/abc-xyz"
            ),
            "abc-xyz",
        )

    def test_drops_file_extension_from_last_segment(self) -> None:
        self.assertEqual(
            extract_listing_id(
                "https://shop.example.com/item/abc-xyz.html"
            ),
            "abc-xyz",
        )

    def test_plain_id_is_returned(self) -> None:
        self.assertEqual(extract_listing_id("abc123"), "abc123")

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(extract_listing_id(""), "")
        self.assertEqual(extract_listing_id("   "), "")


if __name__ == "__main__":
    unittest.main()
