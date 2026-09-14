"""Filesystem-backed blob storage."""

from __future__ import annotations

from pathlib import Path


class LocalStorage:
    """Stores blobs under a root directory. URIs are ``file://`` absolute paths."""

    scheme = "file://"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key_or_uri: str) -> Path:
        if key_or_uri.startswith(self.scheme):
            return Path(key_or_uri[len(self.scheme) :])
        p = (self.root / key_or_uri).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError(f"key escapes storage root: {key_or_uri!r}")
        return p

    def put(self, key: str, data: bytes) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"{self.scheme}{p}"

    def get(self, uri: str) -> bytes:
        return self._path(uri).read_bytes()

    def exists(self, uri: str) -> bool:
        return self._path(uri).exists()

    def delete(self, uri: str) -> None:
        self._path(uri).unlink(missing_ok=True)
