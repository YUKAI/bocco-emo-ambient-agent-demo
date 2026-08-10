import unittest

from bocco_bridge.bocco import MotionPreset
from bocco_bridge.motions import MotionCatalog


class MotionCatalogTests(unittest.TestCase):
    def test_family_selection_is_stable_and_missing_families_are_safe(self) -> None:
        catalog = MotionCatalog(
            (
                MotionPreset("GOOD_02", "good-2"),
                MotionPreset("GOOD_01", "good-1"),
                MotionPreset("YES_01", "yes-1"),
            )
        )

        selected = catalog.select(("GOOD_", "YES_"), seed="request-1")

        self.assertIsNotNone(selected)
        self.assertEqual(
            selected, catalog.select(("GOOD_", "YES_"), seed="request-1")
        )
        self.assertIsNone(catalog.select(("WHAT_",), seed="request-1"))
        self.assertEqual(catalog.get("yes_01"), MotionPreset("YES_01", "yes-1"))
        self.assertIsNone(catalog.get("YES"))


if __name__ == "__main__":
    unittest.main()
