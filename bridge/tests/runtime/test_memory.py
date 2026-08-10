import os
from pathlib import Path
import tempfile
import unittest

from bocco_bridge.memory import HouseholdMemory


class HouseholdMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "memory.db"
        self.memory = HouseholdMemory(self.path)
        await self.memory.initialize()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_store_list_forget_and_source_dedup(self) -> None:
        first = await self.memory.remember(
            "room-1", "猫の名前はミケ", "remember-1", created_at=100
        )
        duplicate = await self.memory.remember(
            "room-1", "別の内容", "remember-1", created_at=101
        )

        self.assertEqual(first.id, duplicate.id)
        self.assertEqual(
            [fact.text for fact in await self.memory.list_active("room-1")],
            ["猫の名前はミケ"],
        )
        self.assertEqual(await self.memory.forget("room-1", "猫の名前"), 1)
        self.assertEqual(await self.memory.list_active("room-1"), ())

    async def test_subject_match_supersedes_old_fact_and_excludes_it_from_search(
        self,
    ) -> None:
        old = await self.memory.remember(
            "room-1", "猫の名前はミケ", "old-name", created_at=100
        )
        new = await self.memory.remember(
            "room-1", "猫の名前はタマ", "new-name", created_at=101
        )

        active = await self.memory.list_active("room-1")
        matches = await self.memory.search("room-1", "猫の名前を教えて")
        self.assertEqual([fact.id for fact in active], [new.id])
        self.assertEqual([fact.id for fact in matches], [new.id])
        self.assertNotEqual(old.id, new.id)

    async def test_search_is_room_scoped_bounded_and_restart_persistent(self) -> None:
        await self.memory.remember(
            "room-1", "犬の好きな食べ物はさつまいも", "dog-1", created_at=100
        )
        await self.memory.remember(
            "room-2", "犬の好きな食べ物はりんご", "dog-2", created_at=101
        )
        await self.memory.remember(
            "room-1", "犬の散歩は朝七時", "dog-3", created_at=102
        )

        reopened = HouseholdMemory(self.path)
        matches = await reopened.search(
            "room-1", "犬の好きな食べ物と散歩", limit=5, max_chars=20
        )

        self.assertTrue(matches)
        self.assertTrue(all(fact.room_uuid == "room-1" for fact in matches))
        self.assertLessEqual(sum(len(fact.text) for fact in matches), 20)
        self.assertNotIn("りんご", "".join(fact.text for fact in matches))

    async def test_memory_database_file_is_private(self) -> None:
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
