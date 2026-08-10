"""In-memory native-stamp catalog loaded once during bridge startup."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .bocco import Stamp


class StampCatalogClient(Protocol):
    async def list_stamps(self) -> tuple[Stamp, ...]: ...


class StampCatalog:
    """Cache stamp names for exact, case-insensitive lookup."""

    def __init__(self, stamps: Iterable[Stamp] = ()) -> None:
        self._by_name: dict[str, Stamp] = {}
        self.replace(stamps)

    @property
    def size(self) -> int:
        return len(self._by_name)

    async def load(self, client: StampCatalogClient) -> None:
        self.replace(await client.list_stamps())

    def replace(self, stamps: Iterable[Stamp]) -> None:
        self._by_name = {stamp.name: stamp for stamp in stamps}

    def get(self, name: str) -> Stamp | None:
        wanted = name.casefold()
        return next(
            (
                stamp
                for stamp_name, stamp in self._by_name.items()
                if stamp_name.casefold() == wanted
            ),
            None,
        )
