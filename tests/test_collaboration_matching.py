import unittest

from yixin.collaboration.matching import MatchAsset, build_pairs
from yixin.collaboration.types import (
    PAIRING_ALL,
    PAIRING_FILENAME,
    PAIRING_MANUAL,
    PairingInput,
)


class CollaborationMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.garments = [
            MatchAsset("g1.png", "G001_红衬衫.png"),
            MatchAsset("g2.png", "G002_外套.png"),
        ]
        self.models = [
            MatchAsset("m1.png", "M001_模特A.png"),
            MatchAsset("m2.png", "M002_模特B.png"),
        ]

    def test_all_combinations(self) -> None:
        result = build_pairs(self.garments, self.models, PAIRING_ALL)
        self.assertEqual(len(result.pairs), 4)
        self.assertFalse(result.errors)

    def test_manual_pairs_are_deduplicated(self) -> None:
        result = build_pairs(
            self.garments,
            self.models,
            PAIRING_MANUAL,
            (PairingInput(0, 1), PairingInput(0, 1)),
        )
        self.assertEqual(result.pairs, (PairingInput(0, 1),))
        self.assertFalse(result.errors)

    def test_filename_pairs_same_number(self) -> None:
        result = build_pairs(
            self.garments,
            self.models,
            PAIRING_FILENAME,
        )
        self.assertEqual(
            result.pairs,
            (PairingInput(0, 0), PairingInput(1, 1)),
        )
        self.assertFalse(result.errors)

    def test_unmatched_and_duplicate_numbers_block_submission(self) -> None:
        garments = [
            *self.garments,
            MatchAsset("g3.png", "G001_重复.png"),
            MatchAsset("g4.png", "未编号.png"),
        ]
        result = build_pairs(
            garments,
            [self.models[0]],
            PAIRING_FILENAME,
        )
        self.assertTrue(result.errors)
        self.assertTrue(any("多张" in error for error in result.errors))
        self.assertTrue(any("缺少 G" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
