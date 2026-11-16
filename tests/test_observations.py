"""Visual notes retain provenance without rewriting the original evidence."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.ingestion.parsers import ParsedBlock, ParsedDocument
from ruminate.persistence.database import apply_migrations, bundled_migrations, connect
from ruminate.persistence.observations import ObservationRepository
from ruminate.persistence.sources import SourceRepository


@pytest.fixture
def store(tmp_path: Path) -> tuple[SourceRepository, ObservationRepository]:
    config = Config(tmp_path / "data", network_mode="off")
    Application(config).initialize()
    return SourceRepository(config.database_path), ObservationRepository(config.database_path)


def source(
    repository: SourceRepository,
    text: str = "Native evidence",
    *,
    uri: str = "file:///test/source.pdf",
    mime_type: str = "application/pdf",
    status: str = "indexed",
) -> dict[str, Any]:
    checksum = hashlib.sha256(text.encode()).hexdigest()
    document = ParsedDocument(
        "Test source",
        mime_type,
        blocks=[ParsedBlock(text, {"type": "pdf_page", "page": 1}, page=1)],
        status=status,
    )
    return repository.save_document(
        document=document,
        sha256=checksum,
        blob_path=f"blobs/{checksum[:2]}/{checksum}",
        uri=uri,
        source_type="file",
        retrieved_at="2026-11-11T08:00:00+00:00",
    )


def payload(saved: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "source_id": saved["source_id"],
        "version_id": saved["version_id"],
        "page": 1,
        "kind": "visual_transcription",
        "text": "  Model transcription, not independently verified.\n",
        "host": "Codex test fixture",
        "request_id": "request-1",
        **changes,
    }


def test_add_preserves_native_source_and_unverified_provenance(
    store: tuple[SourceRepository, ObservationRepository],
) -> None:
    sources, observations = store
    saved = source(sources, status="needs_ocr")
    original = sources.get_source(saved["source_id"])
    result = observations.add(**payload(saved))
    assert result["text"] == payload(saved)["text"]
    assert result["source_id"] == saved["source_id"]
    assert result["version_id"] == saved["version_id"]
    assert result["source_sha256"] == original["sha256"]
    assert result["blob_path"] == original["blob_path"]
    assert result["locator"] == {"type": "pdf_page", "page": 1, "bbox": None}
    assert result["status"] == "unverified"
    assert result["origin"] == "host_model"
    assert result["host_attribution"] == "caller_supplied"
    assert result["content_trust"] == "untrusted_model_output"
    assert sources.get_source(saved["source_id"]) == original
    assert not sources.search("transcription")
    assert sources.search("Native")[0]["metadata"]["parse_quality"] == "native_text"
    with connect(sources.database_path, readonly=True) as connection:
        audit = connection.execute(
            "SELECT * FROM audit_events WHERE action='visual_observation_recorded'"
        ).fetchone()
        assert audit is not None
        details = json.loads(audit["details_json"])
        assert details["status"] == "unverified"
        assert "text" not in details
        assert result["text"] not in audit["details_json"]


def test_idempotent_retry_creates_one_record_and_audit(
    store: tuple[SourceRepository, ObservationRepository],
) -> None:
    sources, observations = store
    saved = source(sources)
    first = observations.add(**payload(saved))
    assert observations.add(**payload(saved)) == first
    assert observations.list(source_id=saved["source_id"]) == [first]
    with connect(sources.database_path, readonly=True) as connection:
        assert connection.execute("SELECT count(*) FROM visual_observations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_events WHERE action='visual_observation_recorded'"
            ).fetchone()[0]
            == 1
        )
    for change in (
        {"text": "different"},
        {"kind": "visual_interpretation"},
        {"page": 2},
        {"host": "different host"},
    ):
        with pytest.raises(RuminateError, match="already used") as error:
            observations.add(**payload(saved, **change))
        assert error.value.code == "idempotency_conflict"
    assert observations.list(source_id=saved["source_id"]) == [first]


def test_historical_version_and_page_filters_remain_exact(
    store: tuple[SourceRepository, ObservationRepository],
) -> None:
    sources, observations = store
    old = source(sources, "old")
    old_note = observations.add(**payload(old))
    new = source(sources, "new")
    new_note = observations.add(**payload(new, request_id="request-2", page=2))
    third_note = observations.add(**payload(new, request_id="request-3", page=3))
    assert new["source_id"] == old["source_id"]
    assert new_note["source_sha256"] != old_note["source_sha256"]
    assert observations.list(source_id=old["source_id"], version_id=old["version_id"]) == [old_note]
    assert observations.list(source_id=new["source_id"], page=2) == [new_note]
    assert observations.list(source_id=new["source_id"], version_id=new["version_id"], page=1) == []
    assert observations.list(source_id=new["source_id"], limit=1) == [third_note]
    assert observations.list(source_id=new["source_id"]) == [third_note, new_note, old_note]


def test_rejects_cross_source_and_non_pdf_versions(
    store: tuple[SourceRepository, ObservationRepository],
) -> None:
    sources, observations = store
    first = source(sources)
    second = source(sources, "different", uri="file:///test/second.pdf")
    markdown = source(sources, "markdown", uri="file:///test/note.md", mime_type="text/markdown")
    for fields, code in (
        ({"source_id": second["source_id"]}, "version_not_found"),
        ({"version_id": "ver_missing"}, "version_not_found"),
        (
            {"source_id": markdown["source_id"], "version_id": markdown["version_id"]},
            "unsupported_visual_source",
        ),
    ):
        with pytest.raises(RuminateError) as error:
            observations.add(**payload(first, **fields))
        assert error.value.code == code
    with pytest.raises(RuminateError) as error:
        observations.list(source_id=second["source_id"], version_id=first["version_id"])
    assert error.value.code == "version_not_found"
    assert observations.list(source_id=first["source_id"]) == []


@pytest.mark.parametrize(
    "change",
    [
        {"page": True},
        {"page": 0},
        {"page": -1},
        {"page": 1.5},
        {"page": "1"},
        {"text": " "},
        {"text": "x" * 20001},
        {"text": "bad\x00content"},
        {"text": None},
        {"text": "surrogate\ud800"},
        {"host": " "},
        {"host": "x" * 101},
        {"host": False},
        {"request_id": ""},
        {"request_id": "x" * 101},
        {"request_id": "bad\nvalue"},
        {"kind": "confirmed_fact"},
        {"kind": ["visual_transcription"]},
        {"source_id": None},
        {"version_id": " "},
    ],
)
def test_invalid_input_does_not_write(
    store: tuple[SourceRepository, ObservationRepository],
    change: dict[str, Any],
) -> None:
    sources, observations = store
    saved = source(sources)
    with pytest.raises(RuminateError):
        observations.add(**payload(saved, **change))
    assert observations.list(source_id=saved["source_id"]) == []


def test_add_and_audit_are_one_transaction(
    store: tuple[SourceRepository, ObservationRepository],
) -> None:
    sources, observations = store
    saved = source(sources)
    with connect(sources.database_path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events "
            "WHEN NEW.action='visual_observation_recorded' "
            "BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        observations.add(**payload(saved))
    assert observations.list(source_id=saved["source_id"]) == []
    with connect(sources.database_path) as connection:
        connection.execute("DROP TRIGGER fail_audit")
    assert observations.add(**payload(saved))["status"] == "unverified"


def test_list_is_bounded_and_read_only(
    store: tuple[SourceRepository, ObservationRepository],
) -> None:
    sources, observations = store
    saved = source(sources)
    observations.add(**payload(saved, text="a" * 12000))
    newest = observations.add(**payload(saved, text="b" * 12000, request_id="new"))
    with connect(sources.database_path, readonly=True) as connection:
        before = list(connection.iterdump())
    assert observations.list(source_id=saved["source_id"]) == [newest]
    with connect(sources.database_path, readonly=True) as connection:
        assert list(connection.iterdump()) == before
    for limit in (0, 21, True, "1"):
        with pytest.raises(RuminateError) as error:
            observations.list(source_id=saved["source_id"], limit=limit)  # type: ignore[arg-type]
        assert error.value.code == "invalid_limit"
    with pytest.raises(RuminateError) as error:
        observations.list(source_id="src_missing")
    assert error.value.code == "source_not_found"


def test_uninitialized_repository_does_not_create_store(tmp_path: Path) -> None:
    repository = ObservationRepository(tmp_path / "missing" / "ruminate.sqlite3")
    with pytest.raises(RuminateError) as error:
        repository.list(source_id="src_missing")
    assert error.value.code == "not_initialized"
    assert not repository.database_path.parent.exists()


def test_concurrent_retries_commit_one_note_and_audit(
    store: tuple[SourceRepository, ObservationRepository],
) -> None:
    sources, observations = store
    saved = source(sources)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(observations.add, **payload(saved)) for _ in range(2)]
        first, second = [future.result(timeout=10) for future in futures]
    assert first == second
    assert observations.list(source_id=saved["source_id"]) == [first]
    with connect(sources.database_path, readonly=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE action='visual_observation_recorded'"
        ).fetchone()[0] == 1


def test_observations_require_migration_without_upgrading_on_read(tmp_path: Path) -> None:
    database_path = tmp_path / "ruminate.sqlite3"
    with connect(database_path) as connection:
        apply_migrations(connection, bundled_migrations()[:2])
    observations = ObservationRepository(database_path)
    with pytest.raises(RuminateError) as error:
        observations.list(source_id="src_missing")
    assert error.value.code == "migration_required"
    with connect(database_path, readonly=True) as connection:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2
