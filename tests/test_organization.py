"""Organization changes survive imports without rewriting evidence or past outputs."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import connect
from ruminate.workspace import WorkspaceAdapter


@pytest.fixture
def app(tmp_path: Path) -> Application:
    result = Application(Config(tmp_path / "store", "off", (tmp_path,)))
    result.initialize()
    return result


def seed(app: Application) -> tuple[dict, dict, dict]:
    topic = app.knowledge.create_topic(slug="agents", name="Agent", request_id="topic")
    source = app.ingest_upload("notes.md", b"# Original title\n\nEvidence stays intact.",
                               topic="agents", request_id="capture")
    document = app.get_source(source["source_id"])
    claim = app.knowledge.create_claim_with_evidence(
        topic_id=topic["topic_id"], statement="What does the source support?",
        claim_kind="question", block_id=document["blocks"][1]["block_id"], request_id="claim",
    )
    project = app.outputs.create_project(topic["topic_id"], "Research", request_id="project")
    app.outputs.create_draft(project["project_id"], request_id="draft")
    return document, claim, project


def test_labels_and_topics_preserve_versions_claims_and_historical_output(app: Application) -> None:
    document, claim, project = seed(app)
    original = app.outputs.get_revision(project["project_id"])
    other = app.knowledge.create_topic(slug="evaluation", name="Evaluation", request_id="other")
    updated = app.management.update_source(
        document["source_id"], title="方便检索的名称 😀", topic_ids=["evaluation"],
        request_id="edit",
    )
    renamed_topic = app.knowledge.update_topic(
        "agents", name="Agent 研究", description="研究范围", request_id="rename-topic",
    )
    assert renamed_topic["slug"] == "agents"
    assert renamed_topic["topic_id"] == claim["topic_id"]
    assert updated["topics"][0]["topic_id"] == other["topic_id"]
    current = app.get_source(document["source_id"])
    assert current["display_title"] == "方便检索的名称 😀"
    assert current["title"] == document["title"] == "Original title"
    assert current["version"] == document["version"]
    assert current["blocks"] == document["blocks"]
    assert app.knowledge.get_claim(claim["claim_id"]) == claim
    assert app.outputs.get_revision(project["project_id"]) == original
    assert app.list_sources(topic="agents") == []
    assert app.list_sources(topic="evaluation")[0]["title"] == updated["display_title"]
    assert app.management.list_items(view="sources", query="方便检索")["total"] == 1
    assert app.management.list_items(view="sources", query="Original title")["total"] == 1
    assert app.management.list_items(view="thoughts", topic="agents")["items"][0][
        "topic_name"
    ] == "Agent 研究"


def test_custom_label_survives_reimports_even_when_original_title_matches(
    app: Application, tmp_path: Path,
) -> None:
    path = tmp_path / "paper.md"
    path.write_text("# First title\n\nOriginal evidence")
    first = app.ingest_file(path)
    app.management.update_source(first["source_id"], title="My label", request_id="label")
    for index, title in enumerate(["My label", "Another original title"]):
        path.write_text(f"# {title}\n\nRevision {index}")
        saved = app.ingest_file(path)
        assert saved["source_id"] == first["source_id"]
        current = app.get_source(saved["source_id"])
        assert current["display_title"] == "My label"
        assert current["title"] == title
        assert current["title_is_custom"] is True
    app.management.update_source(first["source_id"], reset_title=True, request_id="reset")
    assert app.get_source(first["source_id"])["display_title"] == "Another original title"
    path.write_text("# New automatic title\n\nFinal evidence")
    app.ingest_file(path)
    assert app.get_source(first["source_id"])["display_title"] == "New automatic title"
    historical = app.get_source(first["source_id"], version_id=first["version_id"])
    assert historical["title"] == "First title"


def test_retries_return_original_results_without_overwriting_newer_edits(app: Application) -> None:
    document, _, _ = seed(app)
    first = app.management.update_source(document["source_id"], title="First", request_id="first")
    app.management.update_source(document["source_id"], title="Second", request_id="second")
    assert app.management.update_source(
        document["source_id"], title="First", request_id="first",
    ) == first
    assert app.get_source(document["source_id"])["display_title"] == "Second"
    with pytest.raises(RuminateError, match="reused"):
        app.management.update_source(document["source_id"], title="Third", request_id="first")
    topic = app.knowledge.update_topic("agents", name="First topic", request_id="first")
    app.knowledge.update_topic("agents", name="Second topic", request_id="second")
    assert app.knowledge.update_topic("agents", name="First topic", request_id="first") == topic
    assert app.knowledge.list_topics()[0]["name"] == "Second topic"


@pytest.mark.parametrize("changes", [
    {}, {"title": " "}, {"title": 123}, {"title": "x" * 501},
    {"title": "New", "reset_title": True}, {"reset_title": "false"},
    {"topic_ids": "agents"}, {"topic_ids": [False]}, {"topic_ids": ["agents"] * 51},
    {"title": "Would partially save", "topic_ids": ["missing"]},
])
def test_invalid_edits_make_no_partial_changes(app: Application, changes: dict) -> None:
    document, _, _ = seed(app)
    before = app.get_source(document["source_id"])
    with connect(app.config.database_path, readonly=True) as db:
        audit_count = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    with pytest.raises(RuminateError):
        app.management.update_source(document["source_id"], request_id="invalid", **changes)
    assert app.get_source(document["source_id"]) == before
    with connect(app.config.database_path, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == audit_count


def test_topic_changes_and_clear_classification_are_explicit(app: Application) -> None:
    document, _, _ = seed(app)
    topic_id = document["topics"][0]["topic_id"]
    with pytest.raises(RuminateError):
        app.knowledge.update_topic(topic_id, name="", request_id="empty")
    with pytest.raises(RuminateError):
        app.knowledge.update_topic(topic_id, request_id="no-change")
    renamed = app.knowledge.update_topic(topic_id, name="Renamed", request_id="name-only")
    assert renamed["description"] == ""
    result = app.management.update_source(
        document["source_id"], topic_ids=[topic_id, "agents"], request_id="dedup",
    )
    assert len(result["topics"]) == 1
    WorkspaceAdapter(app).write("update_source", {
        "source_id": document["source_id"], "topic_ids": [], "request_id": "clear",
    })
    assert app.get_source(document["source_id"])["topics"] == []
    assert app.management.list_items(view="thoughts")["total"] == 1
    with pytest.raises(RuminateError):
        WorkspaceAdapter(app).write("update_source", {
            "source_id": document["source_id"], "canonical_uri": "https://example.org/changed",
            "request_id": "unsupported",
        })


def test_cli_edits_share_the_same_store_and_backup_preserves_labels(
    app: Application, tmp_path: Path,
) -> None:
    document, _, project = seed(app)
    cli = [sys.executable, "-m", "ruminate", "--data-dir", str(app.config.data_dir)]
    result = subprocess.run([*cli, "sources", "edit", document["source_id"],
                             "--title", "CLI name", "--clear-topics", "--request-id", "cli"],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["display_title"] == "CLI name"
    subprocess.run([*cli, "topics", "edit", "agents", "--name", "CLI topic",
                    "--request-id", "cli-topic"], capture_output=True, text=True, check=True)
    original = app.outputs.get_revision(project["project_id"])
    archive = tmp_path / "backup.zip"
    app.backups.export_backup(archive)
    restored = Application(Config(tmp_path / "restored", "off"))
    restored.backups.import_backup(archive)
    result = restored.get_source(document["source_id"])
    assert result["display_title"] == "CLI name" and result["title_is_custom"] is True
    assert result["topics"] == []
    assert restored.knowledge.list_topics()[0]["name"] == "CLI topic"
    assert restored.outputs.get_revision(project["project_id"]) == original
