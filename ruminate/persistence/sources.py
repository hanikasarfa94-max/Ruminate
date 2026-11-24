"""Immutable evidence versions, conservative deduplication, and bounded FTS retrieval."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ruminate import __version__
from ruminate.errors import RuminateError
from ruminate.persistence.common import find_topic, validate_topic_slug
from ruminate.persistence.database import (
    bundled_migrations,
    connect,
    validate_database,
    validate_storage_paths,
    verify_history,
)

if TYPE_CHECKING:
    from ruminate.ingestion.parsers import ParsedDocument

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]+")
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def _id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def _terms(text: str, *, indexing: bool) -> list[str]:
    """FTS-only tokens; original block text is never normalized or overwritten."""
    separated = _CJK.sub(lambda match: " " + match.group() + " ", text.casefold())
    terms: list[str] = []
    for word in _WORDS.findall(separated):
        if _CJK.fullmatch(word):
            if indexing or len(word) == 1:
                terms.extend(word)
            if len(word) > 1:
                terms.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            terms.append(word)
    return terms


def _limit(value: int, maximum: int = 50) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise RuminateError("invalid_limit", f"Limit must be between 1 and {maximum}")


def _offset(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuminateError("invalid_range", "Offset must be a non-negative integer")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _topic_filter(connection: sqlite3.Connection, topic: str | None) -> tuple[str, list[str]]:
    if topic is None:
        return "", []
    resolved = find_topic(connection, topic)
    if resolved is None:
        return " AND 0", []
    return (
        " AND EXISTS (SELECT 1 FROM source_topics st WHERE st.source_id=s.id AND st.topic_id=?)",
        [resolved["id"]],
    )


def _version(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "version_id": row["id"],
        "source_id": row["source_id"],
        "sha256": row["sha256"],
        "blob_path": row["blob_path"],
        "retrieved_at": row["retrieved_at"],
        "parser_version": row["parser_version"],
        "mime_type": row["mime_type"],
        "title": row["title"],
        "authors": json.loads(row["authors_json"]),
        "published_at": row["published_at"],
        "warnings": json.loads(row["warnings_json"]),
        "status": row["status"],
        "canonical_url": row["canonical_url"],
    }


class SourceRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connection(self, *, readonly: bool = True) -> Iterator[sqlite3.Connection]:
        validate_storage_paths(self.database_path.parent)
        if not self.database_path.is_file():
            raise RuminateError("not_initialized", "Run ruminate init before using the store")
        with connect(self.database_path, readonly=readonly) as connection:
            validate_database(connection)
            current = verify_history(connection, bundled_migrations())
            if current != len(bundled_migrations()):
                raise RuminateError("migration_required", "Run ruminate init to update the schema")
            yield connection

    def save_document(
        self,
        *,
        document: ParsedDocument,
        sha256: str,
        blob_path: str,
        uri: str,
        source_type: str,
        retrieved_at: str,
        topic: str | None = None,
        aliases: tuple[str, ...] = (),
        job_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        if source_type not in {"file", "url"}:
            raise RuminateError("invalid_source_type", "Source type must be file or url")
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise RuminateError("invalid_checksum", "Expected a lowercase SHA256 checksum")
        if blob_path != f"blobs/{sha256[:2]}/{sha256}":
            raise RuminateError("unsafe_storage_path", "Invalid content-addressed blob path")
        if not uri or len(uri) > 8192 or len(aliases) > 16:
            raise RuminateError("invalid_source_uri", "Invalid source identity or alias count")
        if topic is not None and (
            not topic.strip() or len(topic) > 100 or any(ord(char) < 32 for char in topic)
        ):
            raise RuminateError(
                "invalid_topic", "Topic must be a non-empty name up to 100 characters"
            )
        if document.status not in {"indexed", "needs_ocr", "needs_browser", "needs_attention"}:
            raise RuminateError(
                "invalid_document", "Parser returned an unsupported document status"
            )
        if any(not block.text.strip() or not block.locator for block in document.blocks):
            raise RuminateError("invalid_document", "Evidence blocks require text and locators")
        if any(len(block.text) > 5000 for block in document.blocks):
            raise RuminateError("invalid_document", "Evidence blocks exceed the text size limit")
        identities = tuple(dict.fromkeys((uri, *aliases)))
        if any(not alias or len(alias) > 8192 for alias in identities):
            raise RuminateError("invalid_source_uri", "Invalid source alias")
        with self._connection(readonly=False) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if job_id is not None:
                    self._validate_job(connection, job_id, lease_token)
                persisted_at = datetime.now(UTC).isoformat()
                source_id, update_current = self._resolve_source(
                    connection, identities, source_type, sha256, document.title, persisted_at
                )
                existing = connection.execute(
                    "SELECT * FROM source_versions WHERE source_id=? AND sha256=?",
                    (source_id, sha256),
                ).fetchone()
                if existing:
                    version_id = str(existing["id"])
                    status = str(existing["status"])
                    title = str(existing["title"])
                    mime_type = str(existing["mime_type"])
                    warnings = json.loads(existing["warnings_json"])
                else:
                    version_id = _id("ver")
                    status = document.status
                    title = document.title
                    mime_type = document.mime_type
                    warnings = list(document.warnings)
                    self._insert_version(
                        connection, source_id, version_id, document, sha256, blob_path, retrieved_at
                    )
                if update_current:
                    connection.execute(
                        "UPDATE sources SET title=CASE WHEN title_is_custom=0 "
                        "THEN ? ELSE title END,"
                        "current_version_id=? WHERE id=?",
                        (title, version_id, source_id),
                    )
                topic_id = self._attach_topic(connection, source_id, topic)
                if job_id is not None:
                    job_status = "indexed" if status == "indexed" else "needs_attention"
                    connection.execute(
                        "UPDATE jobs SET status=?,error_code=?,finished_at=? WHERE id=?",
                        (job_status, None if status == "indexed" else status, persisted_at, job_id),
                    )
                    connection.execute(
                        "INSERT INTO job_results(job_id,source_id,version_id) VALUES(?,?,?) "
                        "ON CONFLICT(job_id) DO UPDATE SET source_id=excluded.source_id,"
                        "version_id=excluded.version_id",
                        (job_id, source_id, version_id),
                    )
                    connection.execute(
                        "UPDATE job_inputs SET lease_token=NULL,lease_expires_at=NULL "
                        "WHERE job_id=?",
                        (job_id,),
                    )
                connection.execute(
                    "INSERT INTO audit_events(id,action,object_id,created_at,details_json) "
                    "VALUES(?,?,?,?,?)",
                    (
                        _id("evt"),
                        "source_ingested",
                        source_id,
                        persisted_at,
                        _json(
                            {
                                "version_id": version_id,
                                "deduplicated": existing is not None,
                                "job_id": job_id,
                            }
                        ),
                    ),
                )
                block_count = connection.execute(
                    "SELECT COUNT(*) FROM blocks WHERE version_id=?", (version_id,)
                ).fetchone()[0]
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {
            "source_id": source_id,
            "version_id": version_id,
            "status": status,
            "deduplicated": existing is not None,
            "block_count": block_count,
            "job_id": job_id,
            "topic_id": topic_id,
            "title": title,
            "mime_type": mime_type,
            "warnings": warnings,
        }

    @staticmethod
    def _validate_job(connection: sqlite3.Connection, job_id: str, lease_token: str | None) -> None:
        if connection.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
            raise RuminateError("job_not_found", "Import job does not exist")
        if lease_token is not None:
            lease = connection.execute(
                "SELECT lease_token FROM job_inputs WHERE job_id=?", (job_id,)
            ).fetchone()
            if lease is None or lease[0] != lease_token:
                raise RuminateError(
                    "job_lease_lost", "Import job has been claimed by another worker"
                )

    @staticmethod
    def _resolve_source(
        connection: sqlite3.Connection,
        identities: tuple[str, ...],
        source_type: str,
        sha256: str,
        title: str,
        created_at: str,
    ) -> tuple[str, bool]:
        marks = ",".join("?" for _ in identities)
        matches = connection.execute(
            f"SELECT DISTINCT source_id FROM source_aliases WHERE uri IN ({marks})", identities
        ).fetchall()
        if len(matches) > 1:
            raise RuminateError("source_identity_conflict", "Aliases refer to different sources")
        source_id = str(matches[0][0]) if matches else None
        update_current = source_id is not None
        if source_id is None and source_type == "file":
            match = connection.execute(
                "SELECT s.id FROM sources s JOIN source_versions v ON v.source_id=s.id "
                "WHERE s.source_type='file' AND v.sha256=? ORDER BY s.created_at,s.id LIMIT 1",
                (sha256,),
            ).fetchone()
            source_id = str(match[0]) if match else None
        if source_id is None:
            source_id = _id("src")
            update_current = True
            connection.execute(
                "INSERT INTO sources(id,source_type,canonical_uri,title,created_at) "
                "VALUES(?,?,?,?,?)",
                (source_id, source_type, identities[0], title, created_at),
            )
        else:
            found_type = connection.execute(
                "SELECT source_type FROM sources WHERE id=?", (source_id,)
            ).fetchone()[0]
            if found_type != source_type:
                raise RuminateError("source_identity_conflict", "Source identity has another type")
        for identity in identities:
            connection.execute(
                "INSERT OR IGNORE INTO source_aliases(uri,source_id) VALUES(?,?)",
                (identity, source_id),
            )
        return source_id, update_current

    @staticmethod
    def _insert_version(
        connection: sqlite3.Connection,
        source_id: str,
        version_id: str,
        document: ParsedDocument,
        sha256: str,
        blob_path: str,
        retrieved_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO source_versions(id,source_id,sha256,blob_path,retrieved_at,parser_version,"
            "mime_type,title,authors_json,published_at,warnings_json,status,canonical_url) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_id,
                source_id,
                sha256,
                blob_path,
                retrieved_at,
                "ruminate/" + __version__,
                document.mime_type,
                document.title,
                _json(document.authors),
                document.published_at,
                _json(document.warnings),
                document.status,
                document.canonical_url,
            ),
        )
        for ordinal, block in enumerate(document.blocks):
            block_id = _id("blk")
            connection.execute(
                "INSERT INTO blocks(id,version_id,ordinal,text,page,section,"
                "locator_json,parse_quality) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    block_id,
                    version_id,
                    ordinal,
                    block.text,
                    block.page,
                    block.section,
                    _json(block.locator),
                    block.quality,
                ),
            )
            connection.execute(
                "INSERT INTO block_fts(block_id,indexed_text) VALUES(?,?)",
                (block_id, " ".join(_terms(block.text, indexing=True))),
            )

    @staticmethod
    def _attach_topic(
        connection: sqlite3.Connection, source_id: str, topic: str | None
    ) -> str | None:
        if topic is None:
            return None
        topic = topic.strip()
        existing = find_topic(connection, topic)
        topic_id = str(existing["id"]) if existing else _id("top")
        if not existing:
            validate_topic_slug(topic)
            connection.execute(
                "INSERT INTO topics(id,slug,name) VALUES(?,?,?)", (topic_id, topic, topic)
            )
        connection.execute(
            "INSERT INTO source_topics(source_id,topic_id) VALUES(?,?) "
            "ON CONFLICT(source_id,topic_id) DO UPDATE SET suggestion_id=NULL",
            (source_id, topic_id),
        )
        return topic_id

    def list_sources(
        self, *, topic: str | None = None, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        _limit(limit)
        _offset(offset)
        with self._connection() as connection:
            filter_sql, params = _topic_filter(connection, topic)
            rows = connection.execute(
                "SELECT s.id AS source_id,s.source_type,s.canonical_uri,s.created_at,"
                "v.id AS version_id,s.title,v.title AS original_title,"
                "v.mime_type,v.authors_json,v.published_at,v.status,"
                "v.retrieved_at,(SELECT COUNT(*) FROM blocks b "
                "WHERE b.version_id=v.id) AS block_count "
                "FROM sources s JOIN source_versions v ON v.id=s.current_version_id "
                "WHERE 1=1" + filter_sql + " ORDER BY s.created_at DESC,s.id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["authors"] = json.loads(item.pop("authors_json"))
            result.append(item)
        return result

    def get_source(
        self,
        source_id: str,
        *,
        version_id: str | None = None,
        block_start: int = 0,
        block_limit: int = 20,
    ) -> dict[str, Any]:
        _limit(block_limit)
        _offset(block_start)
        with self._connection() as connection:
            source = connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if source is None:
                raise RuminateError("source_not_found", "Source does not exist")
            selected = version_id or source["current_version_id"]
            version = connection.execute(
                "SELECT * FROM source_versions WHERE id=? AND source_id=?", (selected, source_id)
            ).fetchone()
            if version is None:
                raise RuminateError("version_not_found", "Version does not belong to this source")
            rows = connection.execute(
                "SELECT * FROM blocks WHERE version_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
                (selected, block_limit, block_start),
            ).fetchall()
            block_count = connection.execute(
                "SELECT COUNT(*) FROM blocks WHERE version_id=?", (selected,)
            ).fetchone()[0]
            versions = connection.execute(
                "SELECT id AS version_id,sha256,retrieved_at,status FROM source_versions "
                "WHERE source_id=? ORDER BY retrieved_at DESC,id DESC LIMIT 50",
                (source_id,),
            ).fetchall()
            version_count = connection.execute(
                "SELECT COUNT(*) FROM source_versions WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            topics = connection.execute(
                "SELECT t.id AS topic_id,t.slug,t.name FROM topics t JOIN source_topics st "
                "ON st.topic_id=t.id WHERE st.source_id=? ORDER BY t.slug LIMIT 50",
                (source_id,),
            ).fetchall()
            topic_count = connection.execute(
                "SELECT COUNT(*) FROM source_topics WHERE source_id=?", (source_id,)
            ).fetchone()[0]
        bounded_rows = []
        returned_chars = 0
        for row in rows:
            if returned_chars + len(row["text"]) > 20_000:
                break
            bounded_rows.append(row)
            returned_chars += len(row["text"])
        blocks = [
            {
                "block_id": row["id"],
                "version_id": row["version_id"],
                "ordinal": row["ordinal"],
                "text": row["text"],
                "page": row["page"],
                "section": row["section"],
                "locator": json.loads(row["locator_json"]),
                "quality": row["parse_quality"],
            }
            for row in bounded_rows
        ]
        metadata = _version(version)
        return {
            **metadata,
            "display_title": source["title"],
            "title_is_custom": bool(source["title_is_custom"]),
            "source_type": source["source_type"],
            "canonical_uri": source["canonical_uri"],
            "current_version_id": source["current_version_id"],
            "version": metadata,
            "blocks": blocks,
            "block_count": block_count,
            "next_block_start": block_start + len(blocks)
            if block_start + len(blocks) < block_count
            else None,
            "versions": [dict(row) for row in versions],
            "version_count": version_count,
            "topics": [dict(row) for row in topics],
            "topic_count": topic_count,
            "content_trust": "untrusted_source_text",
        }

    def search(
        self, query: str, *, topic: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        _limit(limit)
        if not query.strip() or len(query) > 512:
            raise RuminateError("invalid_query", "Query must contain 1 to 512 characters")
        tokens = list(dict.fromkeys(_terms(query, indexing=False)))
        if len(tokens) > 64:
            raise RuminateError("invalid_query", "Query contains too many search terms")
        if not tokens:
            return []
        # Treat every input as literal terms; operators, quotes and FTS syntax are not executable.
        match_query = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        with self._connection() as connection:
            filter_sql, params = _topic_filter(connection, topic)
            rows = connection.execute(
                "SELECT b.*,s.id AS source_id,s.source_type,s.canonical_uri,"
                "s.title AS display_title,v.title,v.mime_type,"
                "v.authors_json,v.published_at,v.retrieved_at,v.status,v.warnings_json,"
                "-bm25(block_fts) AS score "
                "FROM block_fts JOIN blocks b ON b.id=block_fts.block_id "
                "JOIN source_versions v ON v.id=b.version_id "
                "JOIN sources s ON s.current_version_id=v.id "
                "WHERE block_fts MATCH ?" + filter_sql + " ORDER BY score DESC,b.id LIMIT ?",
                [match_query, *params, limit],
            ).fetchall()
        results = []
        for row in rows:
            text = str(row["text"])
            positions = [text.casefold().find(term) for term in tokens]
            position = min((value for value in positions if value >= 0), default=0)
            start = max(0, position - 100)
            snippet = text[start : start + 800]
            results.append(
                {
                    "source_id": row["source_id"],
                    "version_id": row["version_id"],
                    "block_id": row["id"],
                    "ordinal": row["ordinal"],
                    "title": row["title"],
                    "display_title": row["display_title"],
                    "page_or_section": row["page"] if row["page"] is not None else row["section"],
                    "locator": json.loads(row["locator_json"]),
                    "snippet": snippet,
                    "snippet_start": start,
                    "snippet_end": start + len(snippet),
                    "score": row["score"],
                    "content_trust": "untrusted_source_text",
                    "metadata": {
                        "title": row["title"],
                        "source_type": row["source_type"],
                        "canonical_uri": row["canonical_uri"],
                        "mime_type": row["mime_type"],
                        "authors": json.loads(row["authors_json"]),
                        "published_at": row["published_at"],
                        "retrieved_at": row["retrieved_at"],
                        "status": row["status"],
                        "warnings": json.loads(row["warnings_json"]),
                        "parse_quality": row["parse_quality"],
                    },
                }
            )
        return results
