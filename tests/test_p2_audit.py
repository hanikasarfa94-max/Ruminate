"""Regressions discovered by independent P2 cross-service integrity probes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import connect


@pytest.fixture
def app(tmp_path: Path) -> Application:
    application = Application(
        Config(tmp_path / "store", network_mode="off", allowed_roots=(tmp_path,))
    )
    application.initialize()
    return application


def topic(app: Application, slug: str = "study") -> dict[str, Any]:
    return app.knowledge.create_topic(slug=slug, name=slug, request_id=f"topic-{slug}")


def test_generated_topic_id_namespace_cannot_be_used_for_new_slugs(app: Application) -> None:
    original = topic(app)
    for slug in (original["topic_id"], "top_" + "f" * 32):
        with pytest.raises(RuminateError) as reserved:
            app.knowledge.create_topic(slug=slug, name="Colliding topic", request_id=slug)
        assert reserved.value.code == "reserved_topic_slug"
    assert len(app.knowledge.list_topics()) == 1


def test_ingest_attaches_existing_topic_id_but_never_creates_reserved_slug(
    app: Application, tmp_path: Path
) -> None:
    original = topic(app)
    selected = tmp_path / "note.md"
    selected.write_text("Bounded evidence from a selected source.")
    captured = app.ingest_file(selected, topic=original["topic_id"])
    assert captured["status"] == "indexed" and captured["topic_id"] == original["topic_id"]
    failed = app.ingest_file(selected, topic="top_" + "f" * 32)
    assert failed["status"] == "needs_attention"
    assert failed["error"]["code"] == "reserved_topic_slug"
    assert len(app.knowledge.list_topics()) == 1
    assert app.search_sources("evidence", topic=original["topic_id"])
    assert app.search_sources("evidence", topic=original["slug"])


def test_legacy_ambiguous_topic_fails_all_resolution_paths(
    app: Application, tmp_path: Path
) -> None:
    original = topic(app)
    legacy_id = "top_" + "b" * 32
    with connect(app.config.database_path) as connection:
        connection.execute(
            "INSERT INTO topics(id,slug,name) VALUES(?,?,?)",
            (legacy_id, original["topic_id"], "Legacy colliding slug"),
        )
    ambiguous = original["topic_id"]
    reads = [
        lambda: app.knowledge.topic_context(ambiguous),
        lambda: app.knowledge.list_claims(ambiguous),
        lambda: app.outputs.list_projects(topic_id=ambiguous),
        lambda: app.reviews.list_queue(topic_id=ambiguous),
        lambda: app.reviews.tick(topic_id=ambiguous, dry_run=True),
        lambda: app.list_sources(topic=ambiguous),
        lambda: app.search_sources("evidence", topic=ambiguous),
    ]
    for read in reads:
        with pytest.raises(RuminateError) as collision:
            read()
        assert collision.value.code == "ambiguous_topic"
    with pytest.raises(RuminateError) as collision:
        app.knowledge.create_claim(
            topic_id=ambiguous,
            statement="Question",
            claim_kind="question",
            request_id="wrong-scope",
        )
    assert collision.value.code == "ambiguous_topic"
    selected = tmp_path / "legacy.md"
    selected.write_text("Evidence that must not be silently assigned.")
    failed = app.ingest_file(selected, topic=ambiguous)
    assert failed["status"] == "needs_attention" and failed["error"]["code"] == "ambiguous_topic"
    assert app.knowledge.topic_context(legacy_id)["topic"]["name"] == "Legacy colliding slug"
    assert (
        app.knowledge.topic_context(original["slug"])["topic"]["topic_id"] == original["topic_id"]
    )
    with connect(app.config.database_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM source_topics").fetchone()[0] == 0


def test_missing_topic_filters_keep_empty_search_contract(app: Application) -> None:
    for missing in ("not-created", "top_" + "f" * 32):
        assert app.list_sources(topic=missing) == []
        assert app.search_sources("evidence", topic=missing) == []


def evidence_fixture(app: Application, tmp_path: Path) -> tuple[str, str, str, Path]:
    selected = tmp_path / "source.md"
    selected.write_text("A" * 1000)
    captured = app.ingest_file(selected, topic="study")
    block = app.get_source(captured["source_id"])["blocks"][0]
    claim = app.knowledge.create_claim(
        topic_id=captured["topic_id"],
        statement="A source-linked assertion",
        claim_kind="source_assertion",
        request_id="claim",
    )
    project = app.outputs.create_project(
        captured["topic_id"], "Audit project", request_id="project"
    )
    return claim["claim_id"], block["block_id"], project["project_id"], selected


def link_range(app: Application, claim_id: str, block_id: str, start: int, end: int) -> set[str]:
    ids = set()
    for index in range(start, end):
        linked = app.knowledge.link_evidence(
            claim_id=claim_id,
            block_id=block_id,
            relation="challenges" if index % 7 == 0 else "supports",
            start_char=index,
            end_char=index + 1,
            request_id=f"{claim_id}-{index}",
        )
        ids.add(linked["evidence_id"])
    return ids


def test_impact_excludes_saved_links_before_limiting_and_reports_true_total(
    app: Application, tmp_path: Path
) -> None:
    claim, block, project, _ = evidence_fixture(app, tmp_path)
    saved = link_range(app, claim, block, 0, 200)
    app.outputs.create_draft(project, request_id="draft")
    original = app.outputs.get_revision(project)
    added = link_range(app, claim, block, 200, 210)
    impact = app.outputs.impact(project)
    assert impact["new_evidence_count"] == 10
    assert {item["id"] for item in impact["new_evidence"]} == added
    assert impact["new_evidence_truncated"] is False
    more = link_range(app, claim, block, 210, 411)
    impact = app.outputs.impact(project)
    returned = {item["id"] for item in impact["new_evidence"]}
    assert impact["new_evidence_count"] == 211 and len(returned) == 200
    assert returned <= added | more and returned.isdisjoint(saved)
    assert impact["new_evidence_truncated"] is True and impact["requires_review"] is True
    assert app.outputs.get_revision(project) == original


def test_impact_global_budget_counts_new_links_across_claims(
    app: Application, tmp_path: Path
) -> None:
    first, block, project, _ = evidence_fixture(app, tmp_path)
    selected_topic = app.knowledge.get_claim(first)["topic_id"]
    second = app.knowledge.create_claim(
        topic_id=selected_topic,
        statement="Second assertion",
        claim_kind="source_assertion",
        request_id="second",
    )["claim_id"]
    link_range(app, first, block, 0, 1)
    link_range(app, second, block, 0, 1)
    app.outputs.create_draft(project, request_id="draft")
    link_range(app, first, block, 1, 151)
    link_range(app, second, block, 1, 151)
    impact = app.outputs.impact(project)
    assert impact["new_evidence_count"] == 300
    assert len(impact["new_evidence"]) == 200 and impact["new_evidence_truncated"] is True


def test_old_revision_survives_source_update_and_archiving_current_objects(
    app: Application, tmp_path: Path
) -> None:
    claim, block, project, selected = evidence_fixture(app, tmp_path)
    link_range(app, claim, block, 0, 1)
    app.outputs.create_draft(project, request_id="draft")
    original = app.outputs.get_revision(project)
    selected.write_text("Updated source content belongs to another preserved version.")
    updated = app.ingest_file(selected)
    # Administrative status changes emulate future archive APIs; historical citations stay valid.
    with connect(app.config.database_path) as connection:
        connection.execute("UPDATE claims SET status='archived' WHERE id=?", (claim,))
        connection.execute("UPDATE output_projects SET status='archived' WHERE id=?", (project,))
    assert app.outputs.get_revision(project) == original
    impact = app.outputs.impact(project)
    assert impact["changed_claims"] == [claim]
    assert impact["superseded_source_versions"][0]["current_version_id"] == updated["version_id"]
    assert impact["revision_modified"] is False
    assert Path(app.outputs.export(project)["markdown_path"]).read_text() == original["markdown"]
