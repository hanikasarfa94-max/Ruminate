"""Actual SQLite/WAL snapshots and adversarial archive restore boundaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ruminate import backup
from ruminate.application import Application
from ruminate.backup import BackupService
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.knowledge import KnowledgeService
from ruminate.outputs import OutputService
from ruminate.persistence.database import connect
from ruminate.review import ReviewService


@pytest.fixture
def prepared(tmp_path: Path) -> dict[str, Any]:
    config = Config(tmp_path / "original", network_mode="off", allowed_roots=(tmp_path,))
    app = Application(config)
    app.initialize()
    path = tmp_path / "paper.md"
    path.write_text("The first study reports 24 measurements.")
    imported = app.ingest_file(path, topic="measurements")
    original = app.get_source(imported["source_id"])
    knowledge = KnowledgeService(config)
    claim = knowledge.create_claim(
        topic_id="measurements",
        statement="The source reports 24.",
        claim_kind="source_assertion",
        request_id="claim",
    )
    knowledge.link_evidence(
        claim_id=claim["claim_id"],
        block_id=original["blocks"][0]["block_id"],
        relation="supports",
        request_id="link",
    )
    belief = knowledge.create_belief(
        topic_id="measurements",
        text="I expected 12 measurements.",
        provenance="Explicit test statement",
        confirmed=True,
        request_id="belief",
    )
    knowledge.link_belief(
        belief_id=belief["belief_id"],
        claim_id=claim["claim_id"],
        relation="challenges",
        justification="Possible difference to inspect",
        request_id="relation",
    )
    ReviewService(config).tick()
    outputs = OutputService(config)
    project = outputs.create_project("measurements", "Research note", request_id="project")
    outputs.create_draft(project["project_id"], request_id="draft")
    path.write_text("The revised study reports 39 measurements.")
    app.ingest_file(path, topic="measurements")
    # Neither arbitrary files nor pre-existing output directories are archive input.
    (config.data_dir / "private-config.json").write_text('{"secret":"test-private-value"}')
    (config.data_dir / "outputs" / "unrelated.txt").write_text("unrelated private material")
    return {
        "config": config,
        "app": app,
        "source": original,
        "claim": claim,
        "project": project,
        "outputs": outputs,
    }


def export(prepared: dict[str, Any], tmp_path: Path) -> Path:
    target = tmp_path / "snapshot.zip"
    BackupService(prepared["config"]).export_backup(target)
    return target


def read_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def write_members(path: Path, members: dict[str, bytes], *, update_manifest: bool = False) -> Path:
    if update_manifest:
        manifest = json.loads(members["manifest.json"])
        for entry in manifest["entries"]:
            if entry["path"] in members:
                data = members[entry["path"]]
                entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        members["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


@pytest.mark.parametrize("legacy_schema", [5, 6])
def test_older_backup_is_verified_then_upgraded_in_new_directory(
    prepared: dict[str, Any], tmp_path: Path, legacy_schema: int,
) -> None:
    members = read_members(export(prepared, tmp_path))
    legacy = tmp_path / "legacy.sqlite3"
    legacy.write_bytes(members["ruminate.sqlite3"])
    with connect(legacy) as db:
        db.execute("DROP TABLE claim_topics")
        db.execute("ALTER TABLE source_topics DROP COLUMN suggestion_id")
        db.execute("DROP TABLE topic_suggestions")
        db.execute("DROP TABLE reflection_notes")
        db.execute("DROP TABLE reflection_guides")
        if legacy_schema == 5:
            db.execute("ALTER TABLE sources DROP COLUMN title_is_custom")
        db.execute("DELETE FROM schema_migrations WHERE version>?", (legacy_schema,))
    records = tmp_path / "legacy-records.json"
    backup._write_records(legacy, records)
    members["ruminate.sqlite3"] = legacy.read_bytes()
    members["records.json"] = records.read_bytes()
    manifest = json.loads(members["manifest.json"])
    manifest["schema_version"] = legacy_schema
    members["manifest.json"] = json.dumps(manifest).encode()
    archive = write_members(tmp_path / "legacy.zip", members, update_manifest=True)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    restored = Application(Config(tmp_path / "restored-legacy", "off"))
    result = restored.backups.import_backup(archive)
    assert result["schema_version"] == 7 and result["restored_from_schema"] == legacy_schema
    assert restored.health_check()["status"] == "ok"
    assert restored.get_source(prepared["source"]["source_id"])["title_is_custom"] is False
    assert restored.outputs.get_revision(prepared["project"]["project_id"]) == (
        prepared["outputs"].get_revision(prepared["project"]["project_id"])
    )
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == checksum


def test_backup_manifest_cannot_misstate_its_schema(
    prepared: dict[str, Any], tmp_path: Path,
) -> None:
    members = read_members(export(prepared, tmp_path))
    manifest = json.loads(members["manifest.json"])
    manifest["schema_version"] = 5
    members["manifest.json"] = json.dumps(manifest).encode()
    archive = write_members(tmp_path / "wrong-schema.zip", members)
    destination = tmp_path / "not-published"
    with pytest.raises(RuminateError, match="schema differs"):
        BackupService(Config(destination, "off")).import_backup(archive)
    assert not destination.exists()


def assert_failed_restore_is_clean(archive: Path, tmp_path: Path) -> None:
    target = tmp_path / "rejected-store"
    with pytest.raises(RuminateError):
        BackupService(Config(target, network_mode="off")).import_backup(archive)
    assert not target.exists()
    assert not list(tmp_path.glob(".ruminate-restore-*"))


def test_full_roundtrip_keeps_history_citations_beliefs_reviews_and_markdown(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    archive = export(prepared, tmp_path)
    members = read_members(archive)
    assert "private-config.json" not in members
    assert "outputs/unrelated.txt" not in members
    assert "records.json" in members
    assert any(name.endswith("/draft.md") for name in members)
    assert not any(name.endswith(("-wal", "-shm")) for name in members)
    restored = Config(tmp_path / "restored", network_mode="off")
    result = BackupService(restored).import_backup(archive)
    assert result["source_blobs"] == 2
    assert result["output_revisions"] == 1
    app = Application(restored)
    assert app.health_check()["status"] == "ok"
    original = prepared["source"]
    assert (
        app.get_source(original["source_id"], version_id=original["version_id"])["blocks"]
        == original["blocks"]
    )
    assert app.search_sources("39 measurements")
    assert (
        KnowledgeService(restored).get_claim(prepared["claim"]["claim_id"])["evidence"][0][
            "version_id"
        ]
        == original["version_id"]
    )
    assert KnowledgeService(restored).topic_context("measurements")["confirmed_beliefs"]
    assert ReviewService(restored).list_queue()
    project_id = prepared["project"]["project_id"]
    assert (
        OutputService(restored).get_revision(project_id)["markdown"]
        == prepared["outputs"].get_revision(project_id)["markdown"]
    )
    exported = OutputService(restored).export(project_id)
    assert Path(exported["markdown_path"]).is_file()
    assert not (restored.data_dir / "private-config.json").exists()


def test_committed_wal_rows_are_in_online_snapshot(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    config = prepared["config"]
    with connect(config.database_path) as connection:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO jobs(id,kind,status) VALUES('wal-record','test','queued')")
        assert (config.data_dir / "ruminate.sqlite3-wal").stat().st_size > 0
        archive = export(prepared, tmp_path)
    restored = Config(tmp_path / "wal-restored")
    BackupService(restored).import_backup(archive)
    with connect(restored.database_path, readonly=True) as connection:
        assert connection.execute("SELECT id FROM jobs WHERE id='wal-record'").fetchone()


def test_export_does_not_overwrite_existing_archive(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    archive = export(prepared, tmp_path)
    before = archive.read_bytes()
    with pytest.raises(RuminateError) as error:
        BackupService(prepared["config"]).export_backup(archive)
    assert error.value.code == "backup_exists"
    assert archive.read_bytes() == before


def test_restore_does_not_overwrite_even_empty_directory(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    archive = export(prepared, tmp_path)
    target = tmp_path / "already-exists"
    target.mkdir()
    with pytest.raises(RuminateError) as error:
        BackupService(Config(target)).import_backup(archive)
    assert error.value.code == "restore_exists"
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/tmp/escape",
        "blobs/../escape",
        "outputs\\escape",
        "ruminate.sqlite3-wal",
        "private-config.json",
        "./records.json",
    ],
)
def test_unsafe_or_extra_paths_are_rejected(
    prepared: dict[str, Any], tmp_path: Path, path: str
) -> None:
    members = read_members(export(prepared, tmp_path))
    members[path] = b"untrusted"
    assert_failed_restore_is_clean(write_members(tmp_path / "unsafe.zip", members), tmp_path)


def test_corrupt_member_does_not_publish_partial_store(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    members = read_members(export(prepared, tmp_path))
    blob = next(name for name in members if name.startswith("blobs/"))
    members[blob] = b"changed"
    assert_failed_restore_is_clean(write_members(tmp_path / "corrupt.zip", members), tmp_path)


def test_forged_blob_hash_still_must_match_database(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    members = read_members(export(prepared, tmp_path))
    blob = next(name for name in members if name.startswith("blobs/"))
    members[blob] = b"changed"
    archive = write_members(tmp_path / "forged.zip", members, update_manifest=True)
    assert_failed_restore_is_clean(archive, tmp_path)


def test_foreign_database_rejected_before_publication(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    members = read_members(export(prepared, tmp_path))
    foreign = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE strangers(secret TEXT)")
    members["ruminate.sqlite3"] = foreign.read_bytes()
    archive = write_members(tmp_path / "foreign.zip", members, update_manifest=True)
    assert_failed_restore_is_clean(archive, tmp_path)


def test_records_json_must_match_database_even_with_valid_hash(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    members = read_members(export(prepared, tmp_path))
    members["records.json"] = b'{"forged":"records"}'
    archive = write_members(tmp_path / "records.zip", members, update_manifest=True)
    assert_failed_restore_is_clean(archive, tmp_path)


def test_duplicate_members_and_symlinks_are_rejected(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    original = export(prepared, tmp_path)
    duplicate = tmp_path / "duplicate.zip"
    duplicate.write_bytes(original.read_bytes())
    with pytest.warns(UserWarning), zipfile.ZipFile(duplicate, "a") as archive:
        archive.writestr("records.json", b"duplicate")
    assert_failed_restore_is_clean(duplicate, tmp_path)
    members = read_members(original)
    link_archive = tmp_path / "link.zip"
    with zipfile.ZipFile(link_archive, "w") as archive:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)
            if name == "records.json":
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)
    assert_failed_restore_is_clean(link_archive, tmp_path)


def test_archive_ratio_and_total_limits(
    prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = export(prepared, tmp_path)
    members = read_members(original)
    members["records.json"] = b"x" * 200000
    compressed = tmp_path / "ratio.zip"
    with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    assert_failed_restore_is_clean(compressed, tmp_path)
    monkeypatch.setattr(backup, "MAX_TOTAL_BYTES", 1)
    assert_failed_restore_is_clean(original, tmp_path)


def test_concurrent_destination_creation_is_not_replaced(
    prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = export(prepared, tmp_path)
    target = tmp_path / "racing-store"
    real_publish = backup._publish_directory

    def racing_publish(parent: int, temporary: str, final: str) -> None:
        target.mkdir()
        real_publish(parent, temporary, final)

    monkeypatch.setattr(backup, "_publish_directory", racing_publish)
    with pytest.raises(FileExistsError):
        BackupService(Config(target)).import_backup(archive)
    assert target.is_dir() and not list(target.iterdir())
    assert not list(tmp_path.glob(".ruminate-restore-*"))


def test_corrupt_blob_blocks_export_and_keeps_destination_absent(
    prepared: dict[str, Any], tmp_path: Path
) -> None:
    source = prepared["source"]
    (prepared["config"].data_dir / source["blob_path"]).write_bytes(b"corrupted")
    target = tmp_path / "not-created.zip"
    with pytest.raises(RuminateError):
        BackupService(prepared["config"]).export_backup(target)
    assert not target.exists()
    assert not list(tmp_path.glob(".ruminate-backup-*"))


def test_failed_capture_snapshot_can_retry_after_restore_without_original(
    prepared: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "recoverable.md"
    selected.write_text("Recoverable source bytes survive backup.")

    def unavailable(*args: object, **kwargs: object) -> None:
        raise RuminateError("temporary_parser_failure", "Synthetic parser failure")

    with monkeypatch.context() as patch:
        patch.setattr("ruminate.ingestion.service.parse_document", unavailable)
        failed = prepared["app"].ingest_file(selected, topic="measurements")
    assert failed["status"] == "needs_attention"
    selected.unlink()
    archive = export(prepared, tmp_path)
    restored = Config(tmp_path / "retry-restored", network_mode="off")
    BackupService(restored).import_backup(archive)
    result = Application(restored).retry_job(failed["job_id"])
    assert result["status"] == "indexed"
    assert Application(restored).search_sources("Recoverable source bytes")
