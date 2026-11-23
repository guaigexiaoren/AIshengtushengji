from __future__ import annotations

import unittest

from yixin.collaboration.product_code import parse_product_code


class ProductCodeTests(unittest.TestCase):
    def test_truncates_full_code_to_second_separator(self) -> None:
        code = parse_product_code("DV263q25445p80723")
        self.assertIsNotNone(code)
        self.assertEqual(code.base_code, "DV263q25445p")

    def test_code_without_tail_is_also_valid(self) -> None:
        code = parse_product_code("DV263q25445p")
        self.assertIsNotNone(code)
        self.assertEqual(code.base_code, "DV263q25445p")

    def test_all_tail_variants_share_one_base_code(self) -> None:
        bases = {
            parse_product_code(value).base_code
            for value in (
                "DV263q25445p80722",
                "DV263q25445p80723",
                "DV263q25445p80725",
                "DV263q25445p8072",
                "DV263q25445p",
            )
        }
        self.assertEqual(bases, {"DV263q25445p"})

    def test_accepts_any_separator_letters(self) -> None:
        code = parse_product_code("dv241a10001b12345")
        self.assertIsNotNone(code)
        self.assertEqual(code.year, "24")
        self.assertEqual(code.season, "1")
        self.assertEqual(code.base_code, "DV241a10001b")

    def test_ignores_characters_appended_after_code(self) -> None:
        for value in (
            "DV263q25445p80723-备注",
            "DV263q25445p8072_1",
            "DV263q25445p80723副本",
            "DV263q25445p80723 (改)",
        ):
            code = parse_product_code(value)
            self.assertIsNotNone(code, value)
            self.assertEqual(code.base_code, "DV263q25445p", value)

    def test_rejects_malformed_codes(self) -> None:
        for value in (
            "",
            "123",
            "DV263q25445",
            "ABC263q1p80723",
            "DV5q25445p8072",
        ):
            self.assertIsNone(parse_product_code(value), value)


if __name__ == "__main__":
    unittest.main()
