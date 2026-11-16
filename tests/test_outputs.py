"""Actual multi-source output snapshots, citation rejection and filesystem exports."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.outputs import TEMPLATES, OutputService, _render
from ruminate.persistence.common import new_id, timestamp
from ruminate.persistence.database import connect

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def bundle(tmp_path: Path) -> dict[str, Any]:
    source_dir = tmp_path / "selected"
    source_dir.mkdir()
    paths = [source_dir / "first.md", source_dir / "second.md", source_dir / "paper.pdf"]
    paths[0].write_text("# First report\n\nThe sample contains 24 observations.\n")
    paths[1].write_text("# Second report\n\nAnother study measured 39 observations.\n")
    paths[2].write_bytes((FIXTURES / "single_column.pdf").read_bytes())
    config = Config(tmp_path / "data", network_mode="off", allowed_roots=(source_dir,))
    app = Application(config)
    app.initialize()
    imported = [app.ingest_file(path, topic="sample-study") for path in paths]
    sources = [app.get_source(item["source_id"]) for item in imported]
    topic = imported[0]["topic_id"]
    claims = []
    for index, source in enumerate(sources):
        block = source["blocks"][-1]
        claim = "clm_" + str(index)
        claims.append(claim)
        with connect(config.database_path) as connection:
            connection.execute(
                "INSERT INTO claims(id,topic_id,statement,claim_kind,origin,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    claim,
                    topic,
                    f"Recorded finding {index}",
                    "source_assertion",
                    "human",
                    timestamp(),
                ),
            )
            add_evidence(connection, claim, block, relation="supports")
    return {
        "app": app,
        "service": OutputService(config),
        "config": config,
        "topic": topic,
        "claims": claims,
        "sources": sources,
        "paths": paths,
    }


def add_evidence(
    connection: sqlite3.Connection,
    claim: str,
    block: dict[str, Any],
    *,
    relation: str,
) -> str:
    evidence_id = new_id("ev")
    connection.execute(
        "INSERT INTO claim_evidence(id,claim_id,block_id,version_id,relation,"
        "start_char,end_char,quote,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            evidence_id,
            claim,
            block["block_id"],
            block["version_id"],
            relation,
            0,
            len(block["text"]),
            block["text"],
            timestamp(),
        ),
    )
    return evidence_id


def project(bundle: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return bundle["service"].create_project(
        bundle["topic"],
        kwargs.pop("title", "Research outline"),
        request_id=kwargs.pop("request_id", new_id("request")),
        **kwargs,
    )


def draft(bundle: dict[str, Any], **kwargs: Any) -> tuple[str, dict[str, Any]]:
    created = project(bundle, **kwargs)
    result = bundle["service"].create_draft(created["project_id"], request_id="first-draft")
    return created["project_id"], result


def test_multi_source_draft_exports_every_citation_and_sidecar(bundle: dict[str, Any]) -> None:
    service = bundle["service"]
    project_id, created = draft(bundle, kind="research_proposal")
    assert created["provider"] == "none" and created["review_status"] == "needs_review"
    assert created["claim_count"] == created["citation_count"] == 3
    revision = service.get_revision(project_id)
    assert len(re.findall(r"^\[\^e\d+\]:", revision["markdown"], flags=re.M)) == 3
    assert "研究设计" in revision["markdown"] and "待人工审核" in revision["markdown"]
    citations = [item for claim in revision["snapshot"]["claims"] for item in claim["evidence"]]
    assert len({citation["source_id"] for citation in citations}) == 3
    assert any(citation["page"] is not None for citation in citations)
    for citation in citations:
        saved = bundle["app"].get_source(citation["source_id"], version_id=citation["version_id"])
        block = next(
            block for block in saved["blocks"] if block["block_id"] == citation["block_id"]
        )
        assert block["text"][citation["start_char"] : citation["end_char"]] == citation["quote"]
        assert citation["locator"] == block["locator"]
    exported = service.export(project_id)
    markdown = Path(exported["markdown_path"])
    assert markdown.read_text() == revision["markdown"]
    sidecar = json.loads(Path(exported["sidecar_path"]).read_text())
    assert sidecar["snapshot"] == revision["snapshot"]
    for relative in re.findall(r"\[保存的原件\]\(([^)]+)\)", markdown.read_text()):
        assert (markdown.parent / relative).resolve().is_file()
    assert service.export(project_id) == exported
    assert list(markdown.parent.iterdir()) and len(list(markdown.parent.iterdir())) == 2


@pytest.mark.parametrize("kind", sorted(TEMPLATES))
def test_all_five_templates_have_outlines_and_valid_citations(
    bundle: dict[str, Any],
    kind: str,
) -> None:
    project_id, _ = draft(bundle, kind=kind)
    result = bundle["service"].get_revision(project_id)
    for heading in TEMPLATES[kind]:
        assert heading in result["markdown"]
    assert result["citation_audit"] == "exact_stored_spans"


def test_model_inferences_and_hypotheses_never_promoted(bundle: dict[str, Any]) -> None:
    with connect(bundle["config"].database_path) as connection:
        connection.execute(
            "UPDATE claims SET claim_kind='model_inference',origin='host_model' WHERE id=?",
            (bundle["claims"][0],),
        )
        connection.execute(
            "UPDATE claims SET claim_kind='hypothesis' WHERE id=?", (bundle["claims"][1],)
        )
        connection.execute("DELETE FROM claim_evidence WHERE claim_id=?", (bundle["claims"][2],))
    project_id, _ = draft(bundle)
    claims = bundle["service"].get_revision(project_id)["snapshot"]["claims"]
    assert [claim["classification"] for claim in claims] == [
        "needs_verification",
        "hypothesis",
        "needs_verification",
    ]
    with connect(bundle["config"].database_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_beliefs").fetchone()[0] == 0


def test_conflicting_evidence_is_contested_and_both_sides_retained(bundle: dict[str, Any]) -> None:
    with connect(bundle["config"].database_path) as connection:
        add_evidence(
            connection,
            bundle["claims"][0],
            bundle["sources"][1]["blocks"][-1],
            relation="challenges",
        )
    project_id, _ = draft(bundle)
    claim = bundle["service"].get_revision(project_id)["snapshot"]["claims"][0]
    assert claim["classification"] == "contested"
    assert {item["relation"] for item in claim["evidence"]} == {"supports", "challenges"}


def test_idempotent_requests_and_atomic_conflicts(bundle: dict[str, Any]) -> None:
    service = bundle["service"]
    one = project(bundle, request_id="project-once")
    assert project(bundle, request_id="project-once") == one
    with pytest.raises(RuminateError) as conflict:
        project(bundle, request_id="project-once", title="Different")
    assert conflict.value.code == "idempotency_conflict"
    created = service.create_draft(one["project_id"], request_id="draft-once")
    assert service.create_draft(one["project_id"], request_id="draft-once") == created
    with pytest.raises(RuminateError) as changed:
        service.create_draft(
            one["project_id"], claim_ids=bundle["claims"][:1], request_id="draft-once"
        )
    assert changed.value.code == "idempotency_conflict"
    assert service.get_project(one["project_id"])["revision_count"] == 1


def test_historical_revision_survives_source_update_and_reports_impact(
    bundle: dict[str, Any],
) -> None:
    service = bundle["service"]
    project_id, _ = draft(bundle)
    original = service.get_revision(project_id)
    bundle["paths"][0].write_text("# Revised report\n\nThis changes the observed sample.\n")
    updated = bundle["app"].ingest_file(bundle["paths"][0])
    source = bundle["app"].get_source(updated["source_id"])
    with connect(bundle["config"].database_path) as connection:
        add_evidence(connection, bundle["claims"][0], source["blocks"][-1], relation="challenges")
    impact = service.impact(project_id)
    assert impact["requires_review"] and not impact["revision_modified"]
    assert impact["superseded_source_versions"][0]["current_version_id"] == updated["version_id"]
    assert impact["new_evidence"][0]["relation"] == "challenges"
    assert service.get_revision(project_id, revision=1) == original
    service.create_draft(project_id, request_id="second-draft")
    assert service.get_revision(project_id)["version"] == 2
    assert service.get_revision(project_id, revision=1) == original
    first = service.export(project_id, revision=1)
    second = service.export(project_id, revision=2)
    assert first["markdown_path"] != second["markdown_path"]
    assert Path(first["markdown_path"]).read_text() == original["markdown"]


@pytest.mark.parametrize(
    "tamper", ["quote", "page", "relation", "block_id", "markdown", "claim_kind", "project"]
)
def test_citation_audit_rejects_fabricated_saved_content(
    bundle: dict[str, Any], tamper: str
) -> None:
    service = bundle["service"]
    project_id, created = draft(bundle)
    result = service.get_revision(project_id)
    snapshot = result["snapshot"]
    citation = snapshot["claims"][0]["evidence"][0]
    if tamper == "quote":
        citation["quote"] = "Invented result"
    elif tamper == "page":
        citation["page"] = 99
    elif tamper == "relation":
        citation["relation"] = "mentions"
        snapshot["claims"][0]["classification"] = "needs_verification"
    elif tamper == "block_id":
        citation["block_id"] = bundle["sources"][1]["blocks"][-1]["block_id"]
    elif tamper == "claim_kind":
        snapshot["claims"][0]["claim_kind"] = "hypothesis"
        snapshot["claims"][0]["classification"] = "hypothesis"
    elif tamper == "project":
        snapshot["project"]["project_id"] = "out_fake"
    markdown = _render(snapshot)
    if tamper == "markdown":
        markdown += "\nThis unsupported fact was added later.\n"
    with connect(bundle["config"].database_path) as connection:
        connection.execute(
            "UPDATE output_revisions SET markdown=?,snapshot_json=? WHERE id=?",
            (markdown, json.dumps(snapshot), created["revision_id"]),
        )
    with pytest.raises(RuminateError) as invalid:
        service.export(project_id)
    assert invalid.value.code == "citation_invalid"
    assert not list((bundle["config"].data_dir / "outputs").iterdir())


def test_new_draft_rejects_corrupt_native_quote_before_committing(bundle: dict[str, Any]) -> None:
    created = project(bundle)
    with connect(bundle["config"].database_path) as connection:
        connection.execute(
            "UPDATE claim_evidence SET quote='fake' WHERE claim_id=?", (bundle["claims"][0],)
        )
    with pytest.raises(RuminateError) as invalid:
        bundle["service"].create_draft(created["project_id"], request_id="invalid-draft")
    assert invalid.value.code == "citation_invalid"
    assert bundle["service"].get_project(created["project_id"])["revision_count"] == 0


def test_blob_corruption_blocks_export_without_changing_revision(bundle: dict[str, Any]) -> None:
    project_id, _ = draft(bundle)
    source = bundle["sources"][0]
    (bundle["config"].data_dir / source["blob_path"]).write_bytes(b"damaged")
    with pytest.raises(RuminateError) as invalid:
        bundle["service"].export(project_id)
    assert invalid.value.code == "blob_corrupt"
    assert bundle["service"].get_project(project_id)["revision_count"] == 1


def test_untrusted_markdown_and_html_are_escaped(bundle: dict[str, Any]) -> None:
    malicious = "<script>alert(1)</script>\n# Injected [click](javascript:bad) `code`"
    with connect(bundle["config"].database_path) as connection:
        connection.execute(
            "UPDATE claims SET statement=? WHERE id=?", (malicious, bundle["claims"][0])
        )
    project_id, _ = draft(bundle, title=malicious)
    markdown = bundle["service"].get_revision(project_id)["markdown"]
    assert "<script>" not in markdown
    assert "\n# Injected" not in markdown
    assert "[click](javascript:bad)" not in markdown
    assert "&lt;script&gt;" in markdown
    assert "\\[click\\]\\(javascript:bad\\)" in markdown


def test_export_refuses_overwriting_edited_or_linked_files(
    bundle: dict[str, Any], tmp_path: Path
) -> None:
    project_id, _ = draft(bundle)
    exported = bundle["service"].export(project_id)
    path = Path(exported["markdown_path"])
    path.write_text("User revision")
    with pytest.raises(RuminateError) as exists:
        bundle["service"].export(project_id)
    assert exists.value.code == "output_exists"
    assert path.read_text() == "User revision"
    path.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    path.symlink_to(outside)
    with pytest.raises((OSError, RuminateError)):
        bundle["service"].export(project_id)
    assert outside.read_text() == "untouched"


def test_export_does_not_follow_project_directory_symlink(
    bundle: dict[str, Any], tmp_path: Path
) -> None:
    project_id, _ = draft(bundle)
    outside = tmp_path / "outside"
    outside.mkdir()
    (bundle["config"].data_dir / "outputs" / project_id).symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(OSError):
        bundle["service"].export(project_id)
    assert list(outside.iterdir()) == []


def test_failed_export_is_retryable_and_does_not_publish_half_a_revision(
    bundle: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, _ = draft(bundle)
    original_fsync = os.fsync

    def fail_fsync(_: int) -> None:
        raise OSError("synthetic full disk")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        bundle["service"].export(project_id)
    output = bundle["config"].data_dir / "outputs" / project_id
    assert list(output.iterdir()) == []
    assert bundle["service"].get_project(project_id)["revision_count"] == 1
    monkeypatch.setattr(os, "fsync", original_fsync)
    assert Path(bundle["service"].export(project_id)["markdown_path"]).is_file()


def test_concurrent_drafting_and_export_do_not_lose_revisions(bundle: dict[str, Any]) -> None:
    service = bundle["service"]
    created = project(bundle)

    def make(index: int) -> dict[str, Any]:
        return service.create_draft(created["project_id"], request_id=f"parallel-{index}")

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(make, range(3)))
    assert sorted(result["version"] for result in results) == [1, 2, 3]
    with ThreadPoolExecutor(max_workers=3) as executor:
        exports = list(executor.map(lambda _: service.export(created["project_id"]), range(3)))
    assert exports[0] == exports[1] == exports[2]
    assert Path(exports[0]["markdown_path"]).read_text()


def test_empty_cross_topic_and_invalid_selections_are_rejected(bundle: dict[str, Any]) -> None:
    created = project(bundle)
    service = bundle["service"]
    for claim_ids in ([], [bundle["claims"][0]] * 2, ["missing"], ["x"] * 51):
        with pytest.raises(RuminateError):
            service.create_draft(
                created["project_id"], claim_ids=claim_ids, request_id=new_id("req")
            )
    with pytest.raises(RuminateError):
        project(bundle, kind="unknown")
    with pytest.raises(RuminateError):
        service.list_projects(limit=True)
    with pytest.raises(RuminateError):
        service.list_projects(status=[])
    with pytest.raises(RuminateError):
        service.get_revision(created["project_id"], revision=True)
    with pytest.raises(RuminateError):
        service.get_revision(created["project_id"], revision=2**100)
    assert service.get_project(created["project_id"])["revision_count"] == 0


def test_read_methods_and_export_do_not_mutate_database_records(bundle: dict[str, Any]) -> None:
    project_id, _ = draft(bundle)

    def dump() -> str:
        with connect(bundle["config"].database_path, readonly=True) as connection:
            return "\n".join(connection.iterdump())

    before = dump()
    service = bundle["service"]
    assert service.list_projects(topic_id="sample-study")[0]["project_id"] == project_id
    service.get_project(project_id)
    service.get_revision(project_id)
    service.impact(project_id)
    service.export(project_id)
    assert dump() == before


def test_legacy_output_format_matches_preserved_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures"
    snapshot = json.loads((fixture / "output_v1_snapshot.json").read_text())
    assert _render(snapshot) == (fixture / "output_v1.md").read_text()


def test_new_copy_preserves_legacy_revision_read_and_export(bundle: dict[str, Any]) -> None:
    service = bundle["service"]
    project_id, first = draft(bundle)
    original = service.get_revision(project_id)
    snapshot = original["snapshot"]
    snapshot["format_version"] = 1
    legacy_markdown = _render(snapshot)
    with connect(bundle["config"].database_path) as connection:
        connection.execute(
            "UPDATE output_revisions SET markdown=?,snapshot_json=?,prompt_version=? WHERE id=?",
            (
                legacy_markdown,
                json.dumps(snapshot),
                "deterministic-output-v1",
                first["revision_id"],
            ),
        )
    assert service.get_revision(project_id)["markdown"] == legacy_markdown
    exported = service.export(project_id)
    assert Path(exported["markdown_path"]).read_text() == legacy_markdown
    second = service.create_draft(project_id, request_id="new-copy")
    updated = service.get_revision(project_id)
    assert second["version"] == 2
    assert updated["snapshot"]["format_version"] == 2
    assert updated["prompt_version"] == "deterministic-output-v2"
    assert updated["snapshot"]["claims"] == original["snapshot"]["claims"]
    assert "研究草稿，待人工审核" in updated["markdown"]
    assert service.get_revision(project_id, revision=1)["markdown"] == legacy_markdown
    assert Path(exported["markdown_path"]).read_text() == legacy_markdown
