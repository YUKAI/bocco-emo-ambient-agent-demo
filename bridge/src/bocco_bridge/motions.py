"""In-memory preset-motion catalog loaded once during bridge startup."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
from typing import Protocol

from .bocco import MotionPreset


class MotionCatalogClient(Protocol):
    async def list_motions(self) -> tuple[MotionPreset, ...]: ...


class MotionCatalog:
    """Cache motion names and choose stable mild variants for an event."""

    def __init__(self, motions: Iterable[MotionPreset] = ()) -> None:
        self._by_name: dict[str, MotionPreset] = {}
        self._by_uuid: dict[str, MotionPreset] = {}
        self.replace(motions)

    @property
    def size(self) -> int:
        return len(self._by_name)

    async def load(self, client: MotionCatalogClient) -> None:
        self.replace(await client.list_motions())

    def replace(self, motions: Iterable[MotionPreset]) -> None:
        self._by_name = {motion.name: motion for motion in motions}
        self._by_uuid = {motion.uuid: motion for motion in self._by_name.values()}

    def get(self, name: str) -> MotionPreset | None:
        """Return one exact catalog name, allowing only case variation."""

        wanted = name.casefold()
        return next(
            (
                motion
                for motion_name, motion in self._by_name.items()
                if motion_name.casefold() == wanted
            ),
            None,
        )

    def kind_for_uuid(self, motion_uuid: str) -> str | None:
        """Return the webhook ``motion.kind`` paired with a preset UUID."""

        motion = self._by_uuid.get(motion_uuid)
        return motion.name if motion is not None else None

    def select(
        self, name_families: tuple[str, ...], *, seed: str
    ) -> MotionPreset | None:
        normalized_families = tuple(family.casefold() for family in name_families)
        candidates = sorted(
            (
                motion
                for name, motion in self._by_name.items()
                if name.casefold().startswith(normalized_families)
            ),
            key=lambda motion: (motion.name.casefold(), motion.uuid),
        )
        if not candidates:
            return None
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        return candidates[int.from_bytes(digest[:8], "big") % len(candidates)]
