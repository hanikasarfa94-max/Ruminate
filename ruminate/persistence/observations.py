"""Host visual notes remain separate from source text and confirmed knowledge."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ruminate.errors import RuminateError
from ruminate.persistence.database import (
    bundled_migrations,
    connect,
    validate_database,
    validate_storage_paths,
    verify_history,
)

_KINDS = {"visual_transcription", "visual_interpretation"}


def _string(value: str, name: str, maximum: int, *, multiline: bool = False) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 and (not multiline or char not in "\t\n\r") for char in value)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise RuminateError(
            "invalid_observation", f"{name} must be non-empty text up to {maximum} characters"
        )


def _page(value: int) -> None:
    # Actual page existence is checked against the preserved PDF in Application.
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**31 - 1:
        raise RuminateError("invalid_page", "Page must be a positive integer")


def _result(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "observation_id": row["id"],
        "source_id": row["source_id"],
        "version_id": row["version_id"],
        "source_sha256": row["source_sha256"],
        "blob_path": row["blob_path"],
        "page": row["page"],
        "locator": json.loads(row["locator_json"]),
        "kind": row["kind"],
        "text": row["text"],
        "host": row["host"],
        "host_attribution": "caller_supplied",
        "request_id": row["request_id"],
        "origin": row["origin"],
        "status": row["status"],
        "content_trust": "untrusted_model_output",
        "created_at": row["created_at"],
    }


class ObservationRepository:
    """Persist append-only notes; never promote them to native evidence or beliefs.

    Callers must validate actual page existence using the stored PDF before add().
    This repository validates immutable source/version ownership and PDF metadata.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connection(self, *, readonly: bool = True) -> Iterator[sqlite3.Connection]:
        validate_storage_paths(self.database_path.parent)
        if not self.database_path.is_file():
            raise RuminateError("not_initialized", "Run ruminate init before using the store")
        with connect(self.database_path, readonly=readonly) as connection:
            validate_database(connection)
            if verify_history(connection, bundled_migrations()) < 3:
                raise RuminateError("migration_required", "Run ruminate init to update the schema")
            yield connection

    @staticmethod
    def _version(connection: sqlite3.Connection, source_id: str, version_id: str) -> sqlite3.Row:
        version = connection.execute(
            "SELECT * FROM source_versions WHERE id=? AND source_id=?", (version_id, source_id)
        ).fetchone()
        if version is None:
            raise RuminateError(
                "version_not_found", "Source version does not exist for this source"
            )
        if version["mime_type"] != "application/pdf":
            raise RuminateError("unsupported_visual_source", "Visual observations require a PDF")
        return cast(sqlite3.Row, version)

    def add(
        self,
        *,
        source_id: str,
        version_id: str,
        page: int,
        kind: str,
        text: str,
        host: str,
        request_id: str,
    ) -> dict[str, Any]:
        _string(source_id, "source_id", 100)
        _string(version_id, "version_id", 100)
        _page(page)
        if not isinstance(kind, str) or kind not in _KINDS:
            raise RuminateError(
                "invalid_observation", "Kind must be visual_transcription or visual_interpretation"
            )
        _string(text, "text", 20000, multiline=True)
        _string(host, "host", 100)
        _string(request_id, "request_id", 100)
        with self._connection(readonly=False) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                version = self._version(connection, source_id, version_id)
                existing = connection.execute(
                    "SELECT o.*,v.source_id FROM visual_observations o "
                    "JOIN source_versions v ON v.id=o.version_id WHERE o.request_id=?",
                    (request_id,),
                ).fetchone()
                if existing is not None:
                    fields = ("source_id", "version_id", "page", "kind", "text", "host")
                    expected = (source_id, version_id, page, kind, text, host)
                    if tuple(existing[field] for field in fields) != expected:
                        raise RuminateError(
                            "idempotency_conflict", "Request id was already used with other content"
                        )
                    connection.commit()
                    return _result(existing)
                observation_id = "obs_" + uuid.uuid4().hex
                created_at = datetime.now(UTC).isoformat()
                locator = json.dumps({"type": "pdf_page", "page": page, "bbox": None})
                connection.execute(
                    "INSERT INTO visual_observations "
                    "(id,version_id,source_sha256,blob_path,page,locator_json,kind,text,"
                    "host,request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        version_id,
                        version["sha256"],
                        version["blob_path"],
                        page,
                        locator,
                        kind,
                        text,
                        host,
                        request_id,
                        created_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO audit_events(id,action,object_id,created_at,details_json) "
                    "VALUES(?,?,?,?,?)",
                    (
                        "evt_" + uuid.uuid4().hex,
                        "visual_observation_recorded",
                        observation_id,
                        created_at,
                        json.dumps(
                            {
                                "source_id": source_id,
                                "version_id": version_id,
                                "page": page,
                                "kind": kind,
                                "origin": "host_model",
                                "status": "unverified",
                            },
                            separators=(",", ":"),
                        ),
                    ),
                )
                row = connection.execute(
                    "SELECT o.*,v.source_id FROM visual_observations o "
                    "JOIN source_versions v ON v.id=o.version_id WHERE o.id=?",
                    (observation_id,),
                ).fetchone()
                assert row is not None
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return _result(row)

    def list(
        self,
        *,
        source_id: str,
        version_id: str | None = None,
        page: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Read newest notes, up to 20 records/20,000 text characters in total.

        Omitted version includes history. Pagination is not yet implemented; the
        version/page filters can narrow a source with more than one responseful.
        """
        _string(source_id, "source_id", 100)
        if version_id is not None:
            _string(version_id, "version_id", 100)
        if page is not None:
            _page(page)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise RuminateError("invalid_limit", "Limit must be between 1 and 20")
        with self._connection() as connection:
            if version_id is not None:
                self._version(connection, source_id, version_id)
            elif (
                connection.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone()
                is None
            ):
                raise RuminateError("source_not_found", "Source does not exist")
            clauses = ["v.source_id=?"]
            parameters: list[str | int] = [source_id]
            if version_id is not None:
                clauses.append("o.version_id=?")
                parameters.append(version_id)
            if page is not None:
                clauses.append("o.page=?")
                parameters.append(page)
            parameters.append(limit)
            rows = connection.execute(
                "SELECT o.*,v.source_id FROM visual_observations o "
                "JOIN source_versions v ON v.id=o.version_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY o.created_at DESC,o.id LIMIT ?",
                parameters,
            ).fetchall()
        results = []
        total = 0
        for row in rows:
            total += len(row["text"])
            if total > 20000:
                break
            results.append(_result(row))
        return results
