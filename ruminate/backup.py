"""Portable, integrity-checked snapshots; restore only into a new data directory."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.ingestion.files import BlobStore, directory_fd
from ruminate.outputs import OutputService
from ruminate.persistence.common import Store, json_dump, timestamp
from ruminate.persistence.database import (
    bundled_migrations,
    connect,
    initialize,
    validate_database,
    verify_history,
)

MAX_ENTRIES = 10_000
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_BLOB_BYTES = 20 * 1024 * 1024
MAX_RECORD_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
TABLES = (
    "schema_migrations",
    "app_metadata",
    "sources",
    "source_versions",
    "blocks",
    "source_aliases",
    "topics",
    "source_topics",
    "jobs",
    "job_inputs",
    "job_results",
    "audit_events",
    "visual_observations",
    "mutations",
    "claims",
    "claim_evidence",
    "user_beliefs",
    "belief_relations",
    "review_candidates",
    "review_events",
    "output_projects",
    "output_revisions",
    "enrichment_inputs",
)
BLOB_PATTERN = re.compile(r"blobs/([0-9a-f]{2})/([0-9a-f]{64})\Z")
OUTPUT_PATTERN = re.compile(
    r"outputs/out_[0-9a-f]{32}/rev_[0-9a-f]{32}/(?:draft\.md|evidence\.json)\Z"
)


def _fail(message: str) -> RuminateError:
    return RuminateError("invalid_backup", message)


def _limit_for(name: str) -> int:
    if name == "ruminate.sqlite3":
        return MAX_DATABASE_BYTES
    if name == "records.json":
        return MAX_RECORD_BYTES
    match = BLOB_PATTERN.fullmatch(name)
    if match and match[1] == match[2][:2]:
        return MAX_BLOB_BYTES
    if OUTPUT_PATTERN.fullmatch(name):
        return 2 * 1024 * 1024
    raise _fail("Archive contains an unexpected member path")


def _resolve_parent(path: Path) -> tuple[Path, str]:
    selected = path.expanduser().absolute()
    try:
        parent = selected.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuminateError("backup_path", "Selected parent directory is unavailable") from exc
    if selected.name in {"", ".", ".."}:
        raise RuminateError("backup_path", "Select a named file or new directory")
    return parent, selected.name


@contextmanager
def _regular_file(path: Path, max_bytes: int) -> Iterator[BinaryIO]:
    parent, name = _resolve_parent(path)
    with directory_fd(parent) as descriptor:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise _fail("Only regular files without links are accepted")
        if before.st_size > max_bytes:
            raise _fail("Backup member exceeds its size budget")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise _fail("File changed while reading the backup")
    finally:
        os.close(fd)


def _hash_file(path: Path, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _regular_file(path, max_bytes) as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > max_bytes:
                raise _fail("Backup member exceeds its size budget")
            digest.update(chunk)
    return digest.hexdigest(), size


def _validate_snapshot(path: Path, *, allow_older: bool = False) -> dict[str, Any]:
    if path.stat().st_size > MAX_DATABASE_BYTES:
        raise _fail("Database exceeds the backup size budget")
    try:
        with connect(path, readonly=True) as connection:
            deadline = time.monotonic() + 15
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            validate_database(connection)
            schema = verify_history(connection, bundled_migrations())
            if schema != len(bundled_migrations()) and not (allow_older and schema >= 5):
                raise _fail("Backup requires the same current schema version")
            if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise _fail("Database integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise _fail("Database contains broken references")
            references: list[dict[str, Any]] = [
                dict(row)
                for row in connection.execute(
                    "SELECT sha256,blob_path FROM source_versions LIMIT ?", (MAX_ENTRIES + 1,)
                )
            ]
            if len(references) > MAX_ENTRIES:
                raise _fail("Too many referenced source versions")
            jobs = connection.execute(
                "SELECT snapshot_json FROM job_inputs WHERE snapshot_json IS NOT NULL LIMIT ?",
                (MAX_ENTRIES + 1,),
            ).fetchall()
            if len(jobs) > MAX_ENTRIES:
                raise _fail("Too many captured job snapshots")
            for row in jobs:
                try:
                    captured = json.loads(row["snapshot_json"])
                    if not isinstance(captured, dict):
                        raise ValueError("Invalid job snapshot")
                    references.append(
                        {"sha256": captured["sha256"], "blob_path": captured["blob_path"]}
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    raise _fail("Captured job has an invalid blob reference") from exc
            blobs = {}
            for row in references:
                name = row["blob_path"]
                if not isinstance(name, str) or not isinstance(row["sha256"], str):
                    raise _fail("Database contains an invalid blob reference")
                _limit_for(name)
                if name != f"blobs/{row['sha256'][:2]}/{row['sha256']}":
                    raise _fail("Database contains an invalid blob reference")
                blobs[name] = row["sha256"]
            revisions = [
                dict(row)
                for row in connection.execute(
                    "SELECT project_id,id,version FROM output_revisions "
                    "ORDER BY project_id,version LIMIT ?",
                    (MAX_ENTRIES + 1,),
                )
            ]
            for row in revisions:
                _limit_for(f"outputs/{row['project_id']}/{row['id']}/draft.md")
            return {"schema_version": schema, "blobs": blobs, "revisions": revisions}
    except sqlite3.Error as exc:
        raise _fail("Database snapshot is unreadable or exceeded its validation budget") from exc


def _write_records(
    database: Path,
    output: Path | None,
    max_bytes: int = MAX_RECORD_BYTES,
) -> tuple[str, int]:
    """Stream fixed known tables; never interpret SQL supplied by an archive."""
    size = 0
    digest = hashlib.sha256()
    manager = output.open("xb") if output is not None else nullcontext(None)
    with connect(database, readonly=True) as connection, manager as stream:

        def write(value: str) -> None:
            nonlocal size
            encoded = value.encode("utf-8")
            size += len(encoded)
            if size > min(MAX_RECORD_BYTES, max_bytes):
                raise _fail("Structured records exceed the backup size budget")
            digest.update(encoded)
            if stream is not None:
                stream.write(encoded)

        write('{"format_version":1,"tables":{')
        schema = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        tables = TABLES + (("reflection_guides", "reflection_notes", "topic_suggestions",
                           "claim_topics") if schema >= 7 else ())
        for index, table in enumerate(tables):
            if index:
                write(",")
            write(json_dump(table) + ":[")
            # These table names are constants. Row order is stable for exact comparison.
            for row_index, row in enumerate(
                connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ):
                if row_index:
                    write(",")
                write(json_dump(dict(row)))
            write("]")
        write("}}\n")
    if output is not None:
        output.chmod(0o600)
    return digest.hexdigest(), size


def _expected_names(snapshot: dict[str, Any]) -> set[str]:
    names = {"ruminate.sqlite3", "records.json", *snapshot["blobs"]}
    for row in snapshot["revisions"]:
        base = f"outputs/{row['project_id']}/{row['id']}"
        names.update({base + "/draft.md", base + "/evidence.json"})
    if len(names) + 1 > MAX_ENTRIES:
        raise _fail("Backup contains too many members")
    return names


def _publish_directory(parent_fd: int, temporary: str, final: str) -> None:
    """Atomic directory rename that fails even when the destination is an empty directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    name, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    function = getattr(libc, name, None)
    if function is None:
        raise RuminateError("backup_platform", "Atomic exclusive restore is unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(parent_fd, os.fsencode(temporary), parent_fd, os.fsencode(final), flag)
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    os.fsync(parent_fd)


class BackupService:
    def __init__(self, config: Config) -> None:
        self.config = config

    def export_backup(self, destination: Path) -> dict[str, Any]:
        parent, name = _resolve_parent(destination)
        with directory_fd(parent) as parent_fd:
            if os.path.lexists(parent / name):
                raise RuminateError("backup_exists", "Backup destination already exists")
            with tempfile.TemporaryDirectory(prefix=".ruminate-backup-", dir=parent) as temporary:
                stage = Path(temporary)
                database = stage / "ruminate.sqlite3"
                with Store(self.config).read() as source, sqlite3.connect(database) as target:
                    deadline = time.monotonic() + 30

                    def progress(status: int, remaining: int, total: int) -> None:
                        if time.monotonic() > deadline:
                            raise _fail("Online backup exceeded its time budget")

                    source.backup(target, pages=128, progress=progress, sleep=0.01)
                database.chmod(0o600)
                snapshot = _validate_snapshot(database)
                names = _expected_names(snapshot)
                (stage / "blobs").mkdir(mode=0o700)
                (stage / "outputs").mkdir(mode=0o700)
                used = database.stat().st_size
                blob_store = BlobStore(
                    replace(
                        self.config,
                        max_source_bytes=min(self.config.max_source_bytes, MAX_BLOB_BYTES),
                    )
                )
                for relative, digest in snapshot["blobs"].items():
                    body = blob_store.read(digest, relative)
                    used += len(body)
                    if used > MAX_TOTAL_BYTES:
                        raise _fail("Backup exceeds its total size budget")
                    target_path = stage / relative
                    target_path.parent.mkdir(mode=0o700, exist_ok=True)
                    target_path.write_bytes(body)
                    target_path.chmod(0o600)
                _, size = _write_records(database, stage / "records.json", MAX_TOTAL_BYTES - used)
                used += size
                outputs = OutputService(Config(stage, network_mode="off"))
                for revision in snapshot["revisions"]:
                    result = outputs.get_revision(
                        revision["project_id"], revision=revision["version"]
                    )
                    sidecar = {key: value for key, value in result.items() if key != "markdown"}
                    files = {
                        "draft.md": result["markdown"].encode("utf-8"),
                        "evidence.json": (json_dump(sidecar) + "\n").encode("utf-8"),
                    }
                    base = stage / "outputs" / revision["project_id"] / revision["id"]
                    base.mkdir(mode=0o700, parents=True)
                    for filename, body in files.items():
                        used += len(body)
                        if used > MAX_TOTAL_BYTES or len(body) > _limit_for(
                            str((base / filename).relative_to(stage))
                        ):
                            raise _fail("Backup output exceeds its size budget")
                        (base / filename).write_bytes(body)
                        (base / filename).chmod(0o600)
                entries: list[dict[str, Any]] = []
                total = 0
                for relative in sorted(names):
                    digest, size = _hash_file(stage / relative, _limit_for(relative))
                    total += size
                    if total > MAX_TOTAL_BYTES:
                        raise _fail("Backup exceeds its total size budget")
                    entries.append({"path": relative, "sha256": digest, "size": size})
                manifest = {
                    "format": "ruminate-backup",
                    "format_version": 1,
                    "created_at": timestamp(),
                    "schema_version": snapshot["schema_version"],
                    "entries": entries,
                }
                manifest_bytes = (json_dump(manifest) + "\n").encode("utf-8")
                if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                    raise _fail("Backup manifest exceeds its size budget")
                archive_path = stage / "archive.zip"
                # Stored entries avoid ratio-limit surprises on highly repetitive user text.
                with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_STORED) as archive:
                    archive.writestr("manifest.json", manifest_bytes)
                    for entry in entries:
                        archive.write(stage / entry["path"], entry["path"])
                archive_path.chmod(0o600)
                with archive_path.open("rb") as archive_stream:
                    os.fsync(archive_stream.fileno())
                # Hard-link publication is atomic and refuses any existing destination.
                os.link(archive_path, name, dst_dir_fd=parent_fd, follow_symlinks=False)
                os.fsync(parent_fd)
        return {
            "status": "ok",
            "archive": str(parent / name),
            "schema_version": snapshot["schema_version"],
            "entries": len(entries),
            "source_blobs": len(snapshot["blobs"]),
            "output_revisions": len(snapshot["revisions"]),
            "uncompressed_bytes": total,
            "exports": "regenerated from the SQLite snapshot; arbitrary files excluded",
        }

    def import_backup(self, archive: Path) -> dict[str, Any]:
        parent, name = _resolve_parent(self.config.data_dir)
        with directory_fd(parent) as parent_fd:
            if os.path.lexists(parent / name):
                raise RuminateError(
                    "restore_exists", "Restore requires a new, nonexistent directory"
                )
            with tempfile.TemporaryDirectory(prefix=".ruminate-restore-", dir=parent) as temporary:
                stage = Path(temporary)
                try:
                    with _regular_file(archive, MAX_TOTAL_BYTES + 16 * 1024 * 1024) as stream:
                        with zipfile.ZipFile(stream, "r") as zipped:
                            entries, archived_schema = self._extract(zipped, stage)
                    snapshot = _validate_snapshot(stage / "ruminate.sqlite3", allow_older=True)
                    if snapshot["schema_version"] != archived_schema:
                        raise _fail("Manifest schema differs from the database history")
                    expected = _expected_names(snapshot)
                    if expected != set(entries):
                        raise _fail("Manifest does not match the database's referenced content")
                    for relative, digest in snapshot["blobs"].items():
                        if entries[relative]["sha256"] != digest:
                            raise _fail("Blob hash does not match its stored source version")
                    if _write_records(stage / "ruminate.sqlite3", None) != _hash_file(
                        stage / "records.json", MAX_RECORD_BYTES
                    ):
                        raise _fail("Structured records do not match the database snapshot")
                    # Authenticate the old snapshot and records before applying bundled SQL.
                    # The archive stays intact; only this new restore directory is upgraded.
                    if archived_schema < len(bundled_migrations()):
                        snapshot["schema_version"] = initialize(stage / "ruminate.sqlite3")
                        (stage / "records.json").unlink()
                        _write_records(stage / "ruminate.sqlite3", stage / "records.json")
                    (stage / "blobs").mkdir(mode=0o700, exist_ok=True)
                    (stage / "outputs").mkdir(mode=0o700, exist_ok=True)
                    outputs = OutputService(Config(stage, network_mode="off"))
                    for revision in snapshot["revisions"]:
                        outputs.export(revision["project_id"], revision=revision["version"])
                    _publish_directory(parent_fd, stage.name, name)
                except (zipfile.BadZipFile, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    raise _fail("Archive has an invalid structure or data") from exc
        return {
            "status": "ok",
            "data_dir": str(parent / name),
            "schema_version": snapshot["schema_version"],
            "restored_from_schema": archived_schema,
            "entries": len(entries),
            "source_blobs": len(snapshot["blobs"]),
            "output_revisions": len(snapshot["revisions"]),
        }

    @staticmethod
    def _extract(
        zipped: zipfile.ZipFile, stage: Path,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        infos = zipped.infolist()
        names = [info.filename for info in infos]
        if (
            len(names) > MAX_ENTRIES
            or len(names) != len(set(names))
            or "manifest.json" not in names
        ):
            raise _fail("Archive member inventory is invalid")
        total = 0
        for info in infos:
            name = info.filename
            if (
                info.orig_filename != name
                or name != str(PurePosixPath(name))
                or name.startswith("/")
                or "\\" in name
                or any(part in {".", ".."} for part in PurePosixPath(name).parts)
                or info.is_dir()
                or info.flag_bits & 1
            ):
                raise _fail("Archive contains unsafe paths or encrypted members")
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                raise _fail("Archive contains links or non-regular files")
            limit = MAX_MANIFEST_BYTES if name == "manifest.json" else _limit_for(name)
            if name != "manifest.json":
                total += info.file_size
            if (
                info.file_size > limit
                or total > MAX_TOTAL_BYTES
                or info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO
            ):
                raise _fail("Archive exceeds resource limits")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise _fail("Unsupported archive compression")
        manifest = json.loads(zipped.read("manifest.json"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != "ruminate-backup"
            or type(manifest.get("format_version")) is not int
            or manifest.get("format_version") != 1
            or type(manifest.get("schema_version")) is not int
            or not 5 <= manifest["schema_version"] <= len(bundled_migrations())
            or not isinstance(manifest.get("entries"), list)
        ):
            raise _fail("Unsupported backup manifest")
        entries = {}
        for entry in manifest["entries"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"path", "sha256", "size"}
                or not isinstance(entry["path"], str)
                or entry["path"] in entries
                or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or type(entry["size"]) is not int
                or entry["size"] < 0
            ):
                raise _fail("Invalid backup member metadata")
            _limit_for(entry["path"])
            entries[entry["path"]] = entry
        if set(names) != {"manifest.json", *entries}:
            raise _fail("Manifest and archive members differ")
        for relative, entry in entries.items():
            info = zipped.getinfo(relative)
            if info.file_size != entry["size"]:
                raise _fail("Member size differs from manifest")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256()
            size = 0
            with zipped.open(info) as source, destination.open("xb") as target:
                while chunk := source.read(65536):
                    size += len(chunk)
                    if size > entry["size"]:
                        raise _fail("Expanded member exceeds its declared size")
                    digest.update(chunk)
                    target.write(chunk)
            destination.chmod(0o600)
            if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                raise _fail("Backup member failed integrity verification")
        return entries, manifest["schema_version"]
