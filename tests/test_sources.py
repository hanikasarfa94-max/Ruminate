"""P1 storage tests use real SQLite transactions and immutable evidence locators."""

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.ingestion.parsers import ParsedBlock, ParsedDocument
from ruminate.persistence.database import connect
from ruminate.persistence.sources import SourceRepository


@pytest.fixture
def repository(tmp_path: Path) -> SourceRepository:
    config = Config(tmp_path / "data", network_mode="off")
    Application(config).initialize()
    return SourceRepository(config.database_path)


def document(text: str, *, title: str = "Research fixture", page: int | None = 3) -> ParsedDocument:
    return ParsedDocument(
        title=title,
        mime_type="application/pdf",
        blocks=[ParsedBlock(text, {"type": "pdf_page", "page": page}, page=page)],
    )


def save(
    repository: SourceRepository,
    text: str,
    *,
    uri: str = "file:///approved/fixture.pdf",
    source_type: str = "file",
    parsed: ParsedDocument | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    checksum = hashlib.sha256(text.encode()).hexdigest()
    return repository.save_document(
        document=parsed or document(text),
        sha256=checksum,
        blob_path=f"blobs/{checksum[:2]}/{checksum}",
        uri=uri,
        source_type=source_type,
        retrieved_at="2026-11-11T08:30:00+00:00",
        **kwargs,
    )


def test_file_checksum_dedup_preserves_source_version_block_and_metadata(
    repository: SourceRepository,
) -> None:
    first = save(repository, "Original evidence")
    original = repository.get_source(first["source_id"])
    same = save(
        repository, "Original evidence", parsed=document("ignored changed parse", title="Wrong")
    )
    copy = save(repository, "Original evidence", uri="file:///approved/copy.pdf")
    assert first["deduplicated"] is False
    assert same["deduplicated"] is copy["deduplicated"] is True
    assert first["source_id"] == same["source_id"] == copy["source_id"]
    assert first["version_id"] == same["version_id"] == copy["version_id"]
    assert same["title"] == "Research fixture"
    assert same["mime_type"] == "application/pdf"
    assert repository.get_source(first["source_id"])["blocks"] == original["blocks"]
    assert repository.get_source(first["source_id"])["title"] == "Research fixture"
    assert len(repository.list_sources()) == 1
    assert repository.get_source(first["source_id"])["published_at"] is None


def test_url_versions_preserve_old_metadata_locators_and_only_search_current(
    repository: SourceRepository,
) -> None:
    uri = "https://example.org/article"
    old_doc = document("superseded result", title="Old title", page=2)
    old_doc.authors = ["Original author"]
    old_doc.published_at = "2020-02-02"
    old = save(repository, "superseded result", uri=uri, source_type="url", parsed=old_doc)
    before = repository.get_source(old["source_id"])
    new_doc = document("current revision", title="New title", page=7)
    new = save(repository, "current revision", uri=uri, source_type="url", parsed=new_doc)
    assert new["source_id"] == old["source_id"]
    assert new["version_id"] != old["version_id"]
    historical = repository.get_source(old["source_id"], version_id=old["version_id"])
    assert historical["title"] == "Old title"
    assert historical["authors"] == ["Original author"]
    assert historical["published_at"] == "2020-02-02"
    assert historical["blocks"] == before["blocks"]
    assert historical["current_version_id"] == new["version_id"]
    assert repository.search("superseded") == []
    result = repository.search("current revision")[0]
    assert result["version_id"] == new["version_id"]
    assert result["page_or_section"] == result["locator"]["page"] == 7


def test_duplicate_historical_file_copy_does_not_roll_back_current_version(
    repository: SourceRepository,
) -> None:
    old = save(repository, "old text")
    new = save(repository, "new text")
    copied = save(repository, "old text", uri="file:///approved/old-copy.pdf")
    assert copied["version_id"] == old["version_id"]
    assert repository.get_source(old["source_id"])["version_id"] == new["version_id"]
    reverted = save(repository, "old text")
    assert reverted["version_id"] == old["version_id"]
    assert repository.get_source(old["source_id"])["version_id"] == old["version_id"]


def test_distinct_url_identities_are_not_merged_by_content_or_canonical_hint(
    repository: SourceRepository,
) -> None:
    parsed = document("same published bytes")
    parsed.canonical_url = "https://elsewhere.invalid/claimed-canonical"
    first = save(
        repository, "same published bytes", uri="https://example.org/one", source_type="url"
    )
    second = save(
        repository,
        "same published bytes",
        uri="https://example.org/two",
        source_type="url",
        parsed=parsed,
    )
    assert first["source_id"] != second["source_id"]
    assert len(repository.list_sources()) == 2
    with pytest.raises(RuminateError, match="does not belong"):
        repository.get_source(first["source_id"], version_id=second["version_id"])


def test_redirect_alias_tracks_same_source_and_conflicting_alias_is_rejected(
    repository: SourceRepository,
) -> None:
    first = save(
        repository,
        "first URL",
        uri="https://example.org/one",
        source_type="url",
        aliases=("https://example.org/final",),
    )
    redirected = save(repository, "new URL", uri="https://example.org/final", source_type="url")
    assert redirected["source_id"] == first["source_id"]
    save(repository, "other URL", uri="https://example.org/other", source_type="url")
    with pytest.raises(RuminateError) as error:
        save(
            repository,
            "conflicting",
            uri="https://example.org/one",
            source_type="url",
            aliases=("https://example.org/other",),
        )
    assert error.value.code == "source_identity_conflict"
    assert len(repository.list_sources()) == 2


def test_chinese_english_and_topic_search_return_exact_original_slices(
    repository: SourceRepository,
) -> None:
    text = (
        "材料证据：认知反刍可以帮助复核。"
        "Agent skills retain evidence without rewriting source text."
    )
    captured = save(repository, text, topic="ecommerce-agents")
    for query in ("反刍", "证", "认知反刍", "agent skills", "EVIDENCE", "Agent反刍"):
        hits = repository.search(query, topic="ecommerce-agents")
        assert len(hits) == 1
        hit = hits[0]
        assert hit["source_id"] == captured["source_id"]
        source = repository.get_source(hit["source_id"], version_id=hit["version_id"])
        assert hit["block_id"] == source["blocks"][0]["block_id"]
        assert hit["ordinal"] == source["blocks"][0]["ordinal"]
        assert hit["snippet"] == text[hit["snippet_start"] : hit["snippet_end"]]
        assert hit["metadata"]["published_at"] is None
        assert hit["content_trust"] == "untrusted_source_text"
    assert repository.search("反刍", topic="unknown-topic") == []
    assert repository.search("反刍", topic=captured["topic_id"])
    assert (
        repository.list_sources(topic=captured["topic_id"])[0]["source_id"] == captured["source_id"]
    )


def test_queries_cannot_execute_fts_syntax_and_bounds_are_enforced(
    repository: SourceRepository,
) -> None:
    save(repository, "alpha evidence")
    save(repository, "beta only", uri="file:///approved/other.pdf")
    assert repository.search("alpha OR beta") == []
    assert repository.search('alpha" OR "beta') == []
    assert repository.search('"; DROP TABLE blocks; --') == []
    assert repository.search("***") == []
    assert len(repository.search("alpha")) == 1
    for query in ("", " " * 2, "x" * 513):
        with pytest.raises(RuminateError) as error:
            repository.search(query)
        assert error.value.code == "invalid_query"
    for limit in (0, 51, True):
        with pytest.raises(RuminateError):
            repository.search("alpha", limit=limit)


def test_long_block_snippet_and_get_source_pagination_are_bounded(
    repository: SourceRepository,
) -> None:
    paragraphs = [
        ParsedBlock(f"paragraph {i}", {"type": "paragraph", "number": i}) for i in range(61)
    ]
    paragraphs[30].text = "prefix " * 300 + "needle" + " suffix" * 300
    captured = save(repository, "large", parsed=ParsedDocument("Long", "text/plain", paragraphs))
    source = repository.get_source(captured["source_id"], block_start=20, block_limit=10)
    assert len(source["blocks"]) == 10
    assert source["blocks"][0]["ordinal"] == 20
    assert source["next_block_start"] == 30
    hit = repository.search("needle")[0]
    assert len(hit["snippet"]) <= 800
    assert "needle" in hit["snippet"]
    assert hit["snippet"] in paragraphs[30].text
    with pytest.raises(RuminateError):
        repository.get_source(captured["source_id"], block_limit=51)
    with pytest.raises(RuminateError):
        repository.get_source(captured["source_id"], block_start=-1)


def test_insertion_error_rolls_back_source_version_blocks_fts_and_topics(
    repository: SourceRepository,
) -> None:
    invalid = document("invalid page", page=0)
    with pytest.raises(sqlite3.IntegrityError):
        save(repository, "invalid page", parsed=invalid, topic="must-not-exist")
    with connect(repository.database_path, readonly=True) as connection:
        for table in (
            "sources",
            "source_versions",
            "blocks",
            "block_fts",
            "topics",
            "audit_events",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_job_result_commit_validates_lease_and_preserves_snapshot(
    repository: SourceRepository,
) -> None:
    with connect(repository.database_path) as connection:
        connection.execute("INSERT INTO jobs(id,kind,status) VALUES('job_1','file','queued')")
        connection.execute(
            "INSERT INTO job_inputs(job_id,topic,snapshot_json,lease_token,lease_expires_at) "
            "VALUES('job_1','research','{}','active',9999999999)"
        )
    with pytest.raises(RuminateError) as error:
        save(repository, "job evidence", job_id="job_1", lease_token="expired")
    assert error.value.code == "job_lease_lost"
    assert repository.list_sources() == []
    captured = save(repository, "job evidence", job_id="job_1", lease_token="active")
    with connect(repository.database_path, readonly=True) as connection:
        assert (
            connection.execute("SELECT status FROM jobs WHERE id='job_1'").fetchone()[0]
            == "indexed"
        )
        row = connection.execute("SELECT * FROM job_inputs WHERE job_id='job_1'").fetchone()
        assert row["lease_token"] is row["lease_expires_at"] is None
        assert row["snapshot_json"] == "{}"
        result = connection.execute("SELECT * FROM job_results WHERE job_id='job_1'").fetchone()
        assert result["source_id"] == captured["source_id"]
        assert result["version_id"] == captured["version_id"]


def test_scanned_document_retains_version_and_marks_job_attention(
    repository: SourceRepository,
) -> None:
    with connect(repository.database_path) as connection:
        connection.execute("INSERT INTO jobs(id,kind,status) VALUES('job_scan','file','queued')")
    parsed = ParsedDocument(
        "Scanned", "application/pdf", [], warnings=["needs_ocr"], status="needs_ocr"
    )
    captured = save(repository, "scan bytes", parsed=parsed, job_id="job_scan")
    assert captured["status"] == "needs_ocr"
    assert captured["warnings"] == ["needs_ocr"]
    source = repository.get_source(captured["source_id"])
    assert source["blocks"] == []
    assert source["warnings"] == ["needs_ocr"]
    with connect(repository.database_path, readonly=True) as connection:
        row = connection.execute(
            "SELECT status,error_code FROM jobs WHERE id='job_scan'"
        ).fetchone()
        assert tuple(row) == ("needs_attention", "needs_ocr")


def test_missing_store_has_no_implicit_initialization(tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "absent" / "ruminate.sqlite3")
    with pytest.raises(RuminateError) as error:
        repository.list_sources()
    assert error.value.code == "not_initialized"
    assert not repository.database_path.parent.exists()


def test_get_source_text_budget_preserves_whole_blocks_and_cursor(
    repository: SourceRepository,
) -> None:
    blocks = [ParsedBlock("a" * 5000, {"type": "paragraph", "number": i}) for i in range(8)]
    captured = save(repository, "budget", parsed=ParsedDocument("Large", "text/plain", blocks))
    source = repository.get_source(captured["source_id"], block_limit=50)
    assert len(source["blocks"]) == 4
    assert sum(len(block["text"]) for block in source["blocks"]) == 20_000
    assert source["next_block_start"] == 4
    remaining = repository.get_source(captured["source_id"], block_start=4, block_limit=50)
    assert len(remaining["blocks"]) == 4
    assert remaining["next_block_start"] is None


@pytest.mark.parametrize(
    ("table", "column"),
    [("sources", "created_at"), ("source_versions", "mime_type"), ("blocks", "parse_quality")],
)
def test_health_rejects_missing_columns_required_by_repository(
    repository: SourceRepository, table: str, column: str
) -> None:
    with connect(repository.database_path) as connection:
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    health = Application(Config(repository.database_path.parent)).health_check()
    assert health["status"] == "needs_attention"
    assert health["error"]["code"] == "schema_incomplete"


def test_job_completion_uses_current_time_without_changing_snapshot_time(
    repository: SourceRepository,
) -> None:
    started_at = datetime.now(UTC).isoformat()
    with connect(repository.database_path) as connection:
        connection.execute(
            "INSERT INTO jobs(id,kind,status,started_at) VALUES('job_later','file','queued',?)",
            (started_at,),
        )
    digest = hashlib.sha256(b"historical snapshot").hexdigest()
    result = repository.save_document(
        document=document("historical snapshot"),
        sha256=digest,
        blob_path=f"blobs/{digest[:2]}/{digest}",
        uri="file:///approved/earlier.md",
        source_type="file",
        retrieved_at="2020-01-01T00:00:00+00:00",
        job_id="job_later",
    )
    with connect(repository.database_path, readonly=True) as connection:
        finished = connection.execute(
            "SELECT finished_at FROM jobs WHERE id='job_later'"
        ).fetchone()[0]
        audited = connection.execute("SELECT created_at FROM audit_events").fetchone()[0]
    assert datetime.fromisoformat(finished) >= datetime.fromisoformat(started_at)
    assert datetime.fromisoformat(audited) >= datetime.fromisoformat(started_at)
    assert repository.get_source(result["source_id"])["retrieved_at"] == "2020-01-01T00:00:00+00:00"
