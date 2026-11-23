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

    def test_path_segment_is_url_decoded(self) -> None:
        self.assertEqual(
            extract_listing_id(
                "https://shop.example.com/p/%E4%B8%AD%E6%96%87"
            ),
            "中文",
        )

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(extract_listing_id(""), "")
        self.assertEqual(extract_listing_id("   "), "")

    def test_malformed_url_returns_empty(self) -> None:
        for value in (
            "https://shop／com/item/123456",
            "https://shop：com/item/123456",
            "http://[::1",
        ):
            with self.subTest(value=value):
                self.assertEqual(extract_listing_id(value), "")


if __name__ == "__main__":
    unittest.main()
