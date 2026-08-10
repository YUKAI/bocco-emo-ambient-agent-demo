import unittest

from bocco_bridge.bocco import Stamp
from bocco_bridge.stamps import StampCatalog


class StampCatalogTests(unittest.TestCase):
    def test_name_resolution_is_case_insensitive_and_missing_is_safe(self) -> None:
        question = Stamp(
            "w10question",
            "571d9a6a-aab9-4f56-a866-9c8ab1a0124f",
            "疑問",
            "question.png",
        )
        catalog = StampCatalog((question,))

        self.assertEqual(catalog.size, 1)
        self.assertEqual(catalog.get("W10QUESTION"), question)
        self.assertIsNone(catalog.get("missing"))


if __name__ == "__main__":
    unittest.main()
