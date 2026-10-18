"""Bounded streaming storage for uploaded videos."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from uuid import uuid4

from fastapi import UploadFile


class UploadError(ValueError):
    """Base class for controlled upload failures."""


class UnsupportedVideoExtensionError(UploadError):
    """Raised when the client filename is not a MOV file."""


class EmptyUploadError(UploadError):
    """Raised for an empty uploaded file."""


class UploadTooLargeError(UploadError):
    """Raised when the configured byte limit is exceeded."""


@dataclass(frozen=True, slots=True)
class SavedUpload:
    """Metadata for a safely stored upload."""

    path: Path
    size_bytes: int
    sha256: str


class VideoStorage:
    """Store uploads under generated names outside any public directory."""

    def __init__(self, root: Path, max_bytes: int, chunk_bytes: int) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.chunk_bytes = chunk_bytes

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def clean_startup_orphans(self) -> int:
        """Single-worker startup only, after unfinished jobs have been recovered.

        This directory is private application storage, never a user media folder.
        Only our generated names are eligible; links/directories are not followed.
        """
        removed = 0
        for path in self.root.iterdir():
            if re.fullmatch(r"[0-9a-f]{32}\.mov", path.name) and not path.is_symlink() and path.is_file():
                path.unlink()
                removed += 1
        return removed

    async def save(self, upload: UploadFile) -> SavedUpload:
        filename = upload.filename or ""
        if Path(filename).suffix.lower() != ".mov":
            await upload.close()
            raise UnsupportedVideoExtensionError("Only .mov files are supported")

        target = self.root / f"{uuid4().hex}.mov"
        size = 0
        digest = hashlib.sha256()
        try:
            with target.open("xb") as destination:
                while chunk := await upload.read(self.chunk_bytes):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UploadTooLargeError("Uploaded file exceeds the configured limit")
                    destination.write(chunk)
                    digest.update(chunk)

            if size == 0:
                raise EmptyUploadError("Uploaded file is empty")
            return SavedUpload(path=target, size_bytes=size, sha256=digest.hexdigest())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

    @staticmethod
    def delete(path: Path) -> None:
        path.unlink(missing_ok=True)
