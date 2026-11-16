"""Atomic, checksum-verified migrations. Read methods never initialize storage."""

import hashlib
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from ruminate.errors import RuminateError

REQUIRED_COLUMNS = {
    "schema_migrations": {"version", "name", "checksum", "applied_at"},
    "app_metadata": {"key", "value"},
    "jobs": {
        "id",
        "kind",
        "input_ref",
        "status",
        "attempts",
        "error_code",
        "started_at",
        "finished_at",
    },
    "audit_events": {"id", "action", "object_id", "created_at", "details_json"},
}

V2_COLUMNS = {
    "sources": {"id", "source_type", "canonical_uri", "title", "created_at", "current_version_id"},
    "source_versions": {
        "id",
        "source_id",
        "sha256",
        "blob_path",
        "retrieved_at",
        "parser_version",
        "mime_type",
        "title",
        "authors_json",
        "published_at",
        "warnings_json",
        "status",
        "canonical_url",
    },
    "blocks": {
        "id",
        "version_id",
        "ordinal",
        "text",
        "page",
        "section",
        "locator_json",
        "parse_quality",
    },
    "source_aliases": {"uri", "source_id"},
    "topics": {"id", "slug", "name", "description", "status"},
    "source_topics": {"source_id", "topic_id"},
    "job_results": {"job_id", "source_id", "version_id"},
    "job_inputs": {"job_id", "topic", "snapshot_json", "lease_token", "lease_expires_at"},
    "block_fts": {"block_id", "indexed_text"},
}

V3_COLUMNS = {
    "visual_observations": {
        "id",
        "version_id",
        "source_sha256",
        "blob_path",
        "page",
        "kind",
        "text",
        "host",
        "request_id",
        "origin",
        "status",
        "created_at",
        "locator_json",
    },
}

V4_COLUMNS = {
    "mutations": {"operation", "request_id", "payload_sha256", "result_json", "created_at"},
    "claims": {"id", "topic_id", "statement", "claim_kind", "status", "origin", "created_at"},
    "claim_evidence": {
        "id",
        "claim_id",
        "block_id",
        "version_id",
        "relation",
        "start_char",
        "end_char",
        "quote",
        "verifier_status",
        "created_at",
    },
    "user_beliefs": {
        "id",
        "topic_id",
        "text",
        "provenance",
        "origin",
        "status",
        "user_confirmed_at",
        "created_at",
    },
    "belief_relations": {"id", "belief_id", "claim_id", "relation", "justification", "created_at"},
    "review_candidates": {
        "id",
        "fingerprint",
        "topic_id",
        "claim_id",
        "belief_id",
        "question",
        "reason",
        "priority",
        "score_breakdown_json",
        "evidence_json",
        "due_at",
        "status",
        "created_at",
        "updated_at",
    },
    "review_events": {"id", "candidate_id", "action", "feedback", "note", "created_at"},
    "output_projects": {
        "id",
        "topic_id",
        "title",
        "kind",
        "target_audience",
        "goal",
        "status",
        "created_at",
    },
    "output_revisions": {
        "id",
        "project_id",
        "version",
        "markdown",
        "snapshot_json",
        "created_at",
        "provider",
        "prompt_version",
        "review_status",
    },
}

V5_COLUMNS = {
    "enrichment_inputs": {
        "job_id",
        "request_id",
        "source_id",
        "version_id",
        "provider",
        "input_json",
        "result_json",
        "lease_token",
        "lease_expires_at",
    },
}


def validate_storage_paths(directory: Path) -> None:
    """Reject links and unexpected file types in application-owned derived paths."""
    if directory.exists() and not directory.is_dir():
        raise RuminateError("storage_error", "Data directory is not a directory")
    for name in (
        "ruminate.sqlite3",
        "ruminate.sqlite3-wal",
        "ruminate.sqlite3-shm",
        "blobs",
        "outputs",
    ):
        path = directory / name
        if path.is_symlink():
            raise RuminateError("unsafe_storage_path", "Storage entries must not be symbolic links")
        if not path.exists():
            continue
        info = path.stat()
        expected_directory = name in {"blobs", "outputs"}
        correct_type = (
            stat.S_ISDIR(info.st_mode) if expected_directory else stat.S_ISREG(info.st_mode)
        )
        if not correct_type or (not expected_directory and info.st_nlink != 1):
            raise RuminateError("unsafe_storage_path", "Unexpected storage entry type or hard link")


def validate_database(connection: sqlite3.Connection, *, allow_empty: bool = False) -> None:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables and allow_empty:
        # Do not adopt a foreign database containing views/triggers but no user tables.
        objects = connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if objects == 0:
            return
    if "app_metadata" not in tables:
        raise RuminateError("foreign_database", "Existing database is not a Ruminate store")
    metadata_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(app_metadata)")
    }
    if not {"key", "value"} <= metadata_columns:
        raise RuminateError("foreign_database", "Existing database has no valid Ruminate identity")
    identity = connection.execute("SELECT value FROM app_metadata WHERE key='product'").fetchone()
    if identity is None or identity[0] != "ruminate":
        raise RuminateError("foreign_database", "Existing database has no valid Ruminate identity")
    required_columns = dict(REQUIRED_COLUMNS)
    if "schema_migrations" in tables:
        version = (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        )
        if version >= 2:
            required_columns.update(V2_COLUMNS)
        if version >= 3:
            required_columns.update(V3_COLUMNS)
        if version >= 4:
            required_columns.update(V4_COLUMNS)
        if version >= 5:
            required_columns.update(V5_COLUMNS)
    for table, required in required_columns.items():
        # Table names are constants from this module, never caller-controlled input.
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if table not in tables or not required <= columns:
            raise RuminateError(
                "schema_incomplete", "Required Ruminate schema is missing or altered"
            )


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def bundled_migrations() -> tuple[Migration, ...]:
    directory = files("ruminate.persistence").joinpath("migrations")
    migrations = tuple(
        Migration(int(p.name.split("_", 1)[0]), p.name, p.read_text(encoding="utf-8"))
        for p in sorted(directory.iterdir(), key=lambda item: item.name)
        if p.name.endswith(".sql")
    )
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise RuminateError("invalid_migrations", "Migration versions must be consecutive from 1")
    return migrations


def statements(sql: str) -> Iterator[str]:
    buffer = ""
    for character in sql:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip():
        raise RuminateError("invalid_migration_sql", "Migration must end with a complete statement")


def verify_history(connection: sqlite3.Connection, migrations: Sequence[Migration]) -> int:
    rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    if [row["version"] for row in rows] != list(range(1, len(rows) + 1)):
        raise RuminateError("invalid_schema_history", "Stored migration sequence is invalid")
    if len(rows) > len(migrations):
        raise RuminateError("schema_too_new", "Database requires a newer Ruminate version")
    for row, migration in zip(rows, migrations, strict=False):
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise RuminateError(
                "migration_mismatch", "Stored migration differs from bundled schema"
            )
    return len(rows)


def apply_migrations(connection: sqlite3.Connection, migrations: Sequence[Migration]) -> int:
    """Commit the full pending batch or roll back all its DDL and history entries."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
        )
        current = verify_history(connection, migrations)
        for migration in migrations[current:]:
            for statement in statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum) VALUES (?, ?, ?)",
                (migration.version, migration.name, migration.checksum),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return len(migrations)


@contextmanager
def connect(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    target = f"{path.as_uri()}?mode=ro" if readonly else str(path)
    connection = sqlite3.connect(target, uri=readonly, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=5000")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def initialize(path: Path) -> int:
    with connect(path) as connection:
        # Verify ownership before changing the existing database's journal mode or schema.
        validate_database(connection, allow_empty=True)
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise RuminateError("wal_unavailable", "SQLite WAL is required")
        connection.execute("PRAGMA synchronous=FULL")
        return apply_migrations(connection, bundled_migrations())


def fts5_available() -> bool:
    with sqlite3.connect(":memory:") as connection:
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        except sqlite3.OperationalError:
            return False
    return True
