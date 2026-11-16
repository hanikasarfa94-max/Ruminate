"""Authorized, bounded file reads and atomic content-addressed blob storage."""

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import validate_storage_paths


@contextmanager
def directory_fd(path: Path) -> Iterator[int]:
    """Walk an already resolved absolute path without following replacement links."""
    if os.name != "posix":
        raise RuminateError("secure_io_unavailable", "Secure file import currently requires POSIX")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def read_at(parent: int, name: str, max_bytes: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuminateError("unsafe_file", "Only regular files without hard links are accepted")
        if before.st_size > max_bytes:
            raise RuminateError("source_too_large", "Source exceeds the configured byte limit")
        chunks = []
        count = 0
        while chunk := os.read(descriptor, min(65536, max_bytes + 1 - count)):
            chunks.append(chunk)
            count += len(chunk)
            if count > max_bytes:
                raise RuminateError("source_too_large", "Source exceeds the configured byte limit")
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuminateError(
                "source_changed", "Source changed during import; retry the operation"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_local_file(path: Path, config: Config) -> tuple[bytes, Path]:
    if not config.allowed_roots:
        raise RuminateError("file_not_authorized", "No file or directory has been authorized")
    try:
        target = path.expanduser().resolve(strict=True)
        authorized = any(
            target == root or (directory_scope and target.is_relative_to(root))
            for root, directory_scope in config.root_scopes
        )
        if not authorized:
            raise RuminateError("file_not_authorized", "File is outside the authorized scope")
        with directory_fd(target.parent) as parent:
            return read_at(parent, target.name, config.max_source_bytes), target
    except (OSError, RuntimeError) as exc:
        raise RuminateError("file_unreadable", "Cannot securely read the selected file") from exc


class BlobStore:
    def __init__(self, config: Config) -> None:
        self.config = config

    def put(self, body: bytes) -> tuple[str, str]:
        if len(body) > self.config.max_source_bytes:
            raise RuminateError("source_too_large", "Source exceeds the configured byte limit")
        digest = hashlib.sha256(body).hexdigest()
        validate_storage_paths(self.config.data_dir)
        with directory_fd(self.config.data_dir / "blobs") as base:
            try:
                os.mkdir(digest[:2], mode=0o700, dir_fd=base)
            except FileExistsError:
                pass
        with directory_fd(self.config.data_dir / "blobs" / digest[:2]) as parent:
            try:
                existing = read_at(parent, digest, self.config.max_source_bytes)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if hashlib.sha256(existing).hexdigest() != digest:
                    raise RuminateError(
                        "blob_corrupt", "Stored source blob failed its integrity check"
                    )
            else:
                temporary = f".{uuid4().hex}.tmp"
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, digest, src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError:
                        pass
        return digest, f"blobs/{digest[:2]}/{digest}"

    def read(self, digest: str, relative_path: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuminateError("invalid_blob", "Invalid source blob identifier")
        if relative_path != f"blobs/{digest[:2]}/{digest}":
            raise RuminateError("invalid_blob", "Invalid source blob location")
        validate_storage_paths(self.config.data_dir)
        with directory_fd(self.config.data_dir / "blobs" / digest[:2]) as parent:
            body = read_at(parent, digest, self.config.max_source_bytes)
        if hashlib.sha256(body).hexdigest() != digest:
            raise RuminateError("blob_corrupt", "Stored source blob failed its integrity check")
        return body
