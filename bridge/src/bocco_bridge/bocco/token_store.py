from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .models import TokenState


class TokenStoreError(RuntimeError):
    """Raised when OAuth state cannot be loaded or durably persisted."""


class TokenStore(Protocol):
    """Synchronous persistence contract used inside the client's refresh lock."""

    def load(self) -> TokenState:
        ...

    def save(self, tokens: TokenState) -> None:
        ...


class InMemoryTokenStore:
    """Thread-safe store intended for tests and short-lived processes."""

    def __init__(self, tokens: TokenState) -> None:
        self._tokens = tokens
        self._lock = threading.Lock()

    def load(self) -> TokenState:
        with self._lock:
            return self._tokens

    def save(self, tokens: TokenState) -> None:
        with self._lock:
            self._tokens = tokens


class AtomicFileTokenStore:
    """Persist both rotated tokens with fsync plus an atomic same-dir replace."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> TokenState:
        try:
            raw = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TokenStoreError("Unable to load OAuth state") from exc

        if not isinstance(payload, dict):
            raise TokenStoreError("OAuth state has an invalid structure")
        expires_value = payload.get("access_expires_at")
        try:
            expires_at = (
                datetime.fromisoformat(expires_value)
                if expires_value is not None
                else None
            )
            return TokenState(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                access_expires_at=expires_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenStoreError("OAuth state has an invalid structure") from exc

    def save(self, tokens: TokenState) -> None:
        parent = self.path.parent
        temporary_path: Path | None = None
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            payload = {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "access_expires_at": (
                    tokens.access_expires_at.isoformat()
                    if tokens.access_expires_at is not None
                    else None
                ),
            }
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=parent
            )
            temporary_path = Path(temp_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise

            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise TokenStoreError("Unable to persist OAuth state") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
