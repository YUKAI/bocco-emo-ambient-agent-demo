from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bocco_bridge.bocco import (  # noqa: E402
    AtomicFileTokenStore,
    TokenState,
    TokenStoreError,
)


class AtomicFileTokenStoreTests(unittest.TestCase):
    def test_rotated_pair_and_expiry_are_durably_reloaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            store = AtomicFileTokenStore(path)
            expires_at = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
            expected = TokenState("access-new", "refresh-new", expires_at)

            store.save(expected)

            self.assertEqual(AtomicFileTokenStore(path).load(), expected)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_failed_replace_preserves_previous_token_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            store = AtomicFileTokenStore(path)
            old = TokenState("access-old", "refresh-old")
            store.save(old)

            with patch(
                "bocco_bridge.bocco.token_store.os.replace",
                side_effect=OSError("simulated failure"),
            ):
                with self.assertRaises(TokenStoreError):
                    store.save(TokenState("access-new", "refresh-new"))

            self.assertEqual(store.load(), old)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_invalid_file_does_not_expose_its_contents_in_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text('{"refresh_token":"secret-value"', encoding="utf-8")

            with self.assertRaisesRegex(TokenStoreError, "Unable to load OAuth state") as ctx:
                AtomicFileTokenStore(path).load()

            self.assertNotIn("secret-value", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
