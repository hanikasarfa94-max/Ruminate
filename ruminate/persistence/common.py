"""Shared transaction, input and idempotency boundaries for cognition services."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import (
    bundled_migrations,
    connect,
    validate_database,
    validate_storage_paths,
    verify_history,
)


def validate_text(value: str, name: str, max_length: int = 2000, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or (not empty and not value.strip())
        or any(ord(c) < 32 and c not in "\n\r\t" for c in value)
        or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise RuminateError("invalid_input", f"Invalid {name}; maximum {max_length} characters")
    return value


def validate_limit(value: int, maximum: int = 50) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise RuminateError("invalid_limit", f"Limit must be between 1 and {maximum}")
    return value


def utc_now(now: datetime | None = None) -> datetime:
    value = now if now is not None else datetime.now(UTC)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RuminateError("invalid_time", "Time must include a timezone")
    return value.astimezone(UTC)


def timestamp(now: datetime | None = None) -> str:
    return utc_now(now).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid4().hex


def json_dump(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def validate_topic_slug(slug: str) -> None:
    """The generated ID namespace cannot also name a different human-readable topic."""
    if re.fullmatch(r"top_[0-9a-f]{32}", slug):
        raise RuminateError("reserved_topic_slug", "Topic IDs cannot be used as topic slugs")


def find_topic(connection: sqlite3.Connection, topic_id: str) -> sqlite3.Row | None:
    validate_text(topic_id, "topic", 100)
    rows = connection.execute(
        "SELECT * FROM topics WHERE id=? OR slug=? LIMIT 2", (topic_id, topic_id)
    ).fetchall()
    if len(rows) > 1:
        raise RuminateError(
            "ambiguous_topic",
            "Topic reference matches multiple topics; choose an unambiguous ID or slug",
        )
    return cast(sqlite3.Row, rows[0]) if rows else None


def resolve_topic(connection: sqlite3.Connection, topic_id: str) -> sqlite3.Row:
    row = find_topic(connection, topic_id)
    if row is None:
        raise RuminateError("topic_not_found", "Topic does not exist")
    return row


def audit(connection: sqlite3.Connection, action: str, object_id: str, details: Any = None) -> None:
    connection.execute(
        "INSERT INTO audit_events(id,action,object_id,created_at,details_json) VALUES(?,?,?,?,?)",
        (new_id("evt"), action, object_id, timestamp(), json_dump(details or {})),
    )


def mutate(
    connection: sqlite3.Connection,
    operation: str,
    request_id: str,
    payload: Any,
    perform: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run inside Store.write(); repeated identical requests return their original result."""
    validate_text(request_id, "request_id", 100)
    digest = hashlib.sha256(json_dump(payload).encode("utf-8")).hexdigest()
    old = connection.execute(
        "SELECT payload_sha256,result_json FROM mutations WHERE operation=? AND request_id=?",
        (operation, request_id),
    ).fetchone()
    if old is not None:
        if old["payload_sha256"] != digest:
            raise RuminateError(
                "idempotency_conflict", "Request id was reused with different input"
            )
        return cast(dict[str, Any], json.loads(old["result_json"]))
    result = perform()
    connection.execute(
        "INSERT INTO mutations(operation,request_id,payload_sha256,result_json,created_at) "
        "VALUES(?,?,?,?,?)",
        (operation, request_id, digest, json_dump(result), timestamp()),
    )
    return result


class Store:
    def __init__(self, config: Config) -> None:
        self.config = config

    @contextmanager
    def _connection(self, *, readonly: bool) -> Iterator[sqlite3.Connection]:
        validate_storage_paths(self.config.data_dir)
        if not self.config.database_path.is_file():
            raise RuminateError("not_initialized", "Run ruminate init before using the store")
        with connect(self.config.database_path, readonly=readonly) as connection:
            validate_database(connection)
            if verify_history(connection, bundled_migrations()) != len(bundled_migrations()):
                raise RuminateError("migration_required", "Run ruminate init to upgrade storage")
            yield connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._connection(readonly=True) as connection:
            yield connection

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._connection(readonly=False) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
