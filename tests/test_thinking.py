"""Real evidence, reversible classification and human-only activity in temporary stores."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import connect
from ruminate.workspace import WorkspaceAdapter


@pytest.fixture
def setup(tmp_path: Path):
    app = Application(Config(tmp_path / "data"))
    app.initialize()
    topic = app.knowledge.create_topic(slug="research", name="研究", request_id="topic")
    other = app.knowledge.create_topic(slug="memory", name="记忆", request_id="other")
    source = app.ingest_upload(
        "note.md", b"# Notes\n\nActions need feedback.", topic="research", request_id="source"
    )
    block = app.get_source(source["source_id"])["blocks"][1]
    claims = [
        app.knowledge.create_claim_with_evidence(
            topic_id=topic["topic_id"],
            statement=f"Question {i}?",
            claim_kind="question",
            origin="host_model",
            block_id=block["block_id"],
            relation="mentions",
            request_id=f"q{i}",
        )
        for i in range(2)
    ]
    return app, source, claims, topic, other


def guide(claim, other=None):
    return dict(
        claim_id=claim["claim_id"],
        lens="connection",
        spark="反馈与记忆的联系是一个假设。",
        prompts=["上一次判断改变，是哪条反馈带来的？", "什么情况下这个联系不成立？"],
        evidence_ids=[claim["evidence"][0]["evidence_id"]],
        links=[]
        if other is None
        else [{"claim_id": other["claim_id"], "reason": "同样关心反馈。"}],
    )


def test_guide_revisions_backlinks_exact_evidence_and_idempotency(setup):
    app, _, claims, _, _ = setup
    original = app.knowledge.get_claim(claims[0]["claim_id"])
    values = guide(claims[0], claims[1])
    first = app.thinking.save_guide(**values, request_id="g1")
    assert app.thinking.save_guide(**values, request_id="g1") == first
    assert app.thinking.context(claims[1]["claim_id"])["related"][0]["direction"] == "incoming"
    with pytest.raises(RuminateError, match="different input"):
        app.thinking.save_guide(**{**values, "spark": "different"}, request_id="g1")
    app.thinking.save_guide(**{**values, "links": [], "spark": "新的解释"}, request_id="g2")
    assert app.thinking.context(claims[1]["claim_id"])["related"] == []
    assert app.thinking.context(claims[0]["claim_id"])["guide"]["spark"] == "新的解释"
    assert app.knowledge.get_claim(claims[0]["claim_id"]) == original
    rows = app.management.list_items(view="thoughts")["items"]
    assert next(r for r in rows if r["id"] == claims[0]["claim_id"])["guide"]["spark"] == "新的解释"
    with connect(app.config.database_path, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM reflection_guides").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM user_beliefs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"prompts": []},
        {"prompts": ["x"] * 4},
        {"lens": "certainty"},
        {"evidence_ids": ["missing"]},
        {"links": [{"claim_id": "missing", "reason": "link"}]},
        {"links": [{"claim_id": "missing", "reason": "link", "extra": True}]},
    ],
)
def test_guide_validation_is_atomic(setup, change):
    app, _, claims, _, _ = setup
    with pytest.raises(RuminateError):
        app.thinking.save_guide(**{**guide(claims[0]), **change}, request_id="bad")
    assert app.thinking.context(claims[0]["claim_id"])["guide"] is None


def test_foreign_evidence_and_self_links_rejected(setup):
    app, _, claims, _, _ = setup
    for changed in (
        dict(evidence_ids=[claims[1]["evidence"][0]["evidence_id"]]),
        dict(links=[{"claim_id": claims[0]["claim_id"], "reason": "self"}]),
    ):
        with pytest.raises(RuminateError):
            app.thinking.save_guide(**{**guide(claims[0]), **changed}, request_id="bad")


def test_notes_origin_pagination_and_activity_timezone(setup):
    app, _, claims, _, _ = setup
    cid = claims[0]["claim_id"]
    model = app.thinking.save_note(claim_id=cid, text="AI wording", request_id="model")
    assert model["origin"] == "host_model"
    adapter = WorkspaceAdapter(app)
    saved = adapter.write(
        "save_reflection_note", {"claim_id": cid, "text": "自己的思考", "request_id": "me"}
    )
    assert (
        adapter.write(
            "save_reflection_note", {"claim_id": cid, "text": "自己的思考", "request_id": "me"}
        )
        == saved
    )
    with pytest.raises(RuminateError):
        adapter.write(
            "save_reflection_note",
            {"claim_id": cid, "text": "x", "origin": "human", "request_id": "extra"},
        )
    with connect(app.config.database_path) as db:
        db.execute("UPDATE reflection_notes SET created_at='2026-11-16T16:30:00.000000+00:00'")
        db.commit()
    now = datetime(2026, 11, 17, 1, tzinfo=UTC)
    east = app.thinking.activity(now=now, day="2026-11-17")
    utc = app.thinking.activity(now=now, timezone="UTC", day="2026-11-16")
    assert east["total"] == east["active_days"] == utc["total"] == 1
    assert east["entries"][0]["text"] == "自己的思考"
    assert east["days"][0]["date"] == "2026-08-31" and len(east["days"]) == 84
    for i in range(21):
        app.thinking.save_note(claim_id=cid, text=f"Note {i}", request_id=f"n{i}")
    assert app.thinking.context(cid)["next_offset"] == 20
    assert len(app.thinking.context(cid, offset=20)["notes"]) == 3
    assert app.thinking.context(cid, offset=20)["next_offset"] is None


def test_activity_counts_feedback_not_background_or_exposures(setup):
    app, _, claims, _, _ = setup
    now = datetime(2026, 11, 17, 1, tzinfo=UTC)
    app.thinking.save_guide(**guide(claims[0]), request_id="guide")
    app.reviews.tick(now=now)
    shown = app.reviews.present(request_id="shown", now=now)
    assert app.thinking.activity(now=now)["total"] == 0
    app.reviews.feedback(shown["cards"][0]["id"], "useful", request_id="feedback", now=now)
    assert app.thinking.activity(now=now)["total"] == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"timezone": "bad/zone"}, {"day": "bad"}, {"day": "2026-02-30"}, {"day": "1900-01-01"}],
)
def test_activity_validation(setup, kwargs):
    with pytest.raises(RuminateError):
        setup[0].thinking.activity(**kwargs)


def suggest(app, source, other, **kwargs):
    return app.organization.suggest(
        target_kind="source",
        target_id=source["source_id"],
        version_id=source["version_id"],
        topic_id=other["topic_id"],
        rationale="资料讨论反馈记忆。",
        request_id=kwargs.pop("request_id", "suggest"),
        **kwargs,
    )


def test_auto_classification_reversible_and_manual_choices_survive(setup):
    app, source, _, topic, other = setup
    result = suggest(app, source, other, auto_apply=True)
    assert result["status"] == "applied"
    assert len(app.list_sources(topic=other["topic_id"])) == 1
    app.organization.decide(result["id"], "revert", request_id="undo")
    assert not app.list_sources(topic=other["topic_id"])
    assert app.list_sources(topic=topic["topic_id"])
    assert suggest(app, source, other, auto_apply=True, request_id="again")["status"] == "reverted"


def test_manual_edit_takes_ownership_before_undo(setup):
    app, source, _, topic, other = setup
    result = suggest(app, source, other, auto_apply=True)
    app.management.update_source(
        source["source_id"], topic_ids=[topic["topic_id"], other["topic_id"]], request_id="manual"
    )
    app.organization.decide(result["id"], "revert", request_id="undo")
    assert len(app.list_sources(topic=other["topic_id"])) == 1


def test_pending_source_version_must_match_on_apply(setup):
    app, source, _, _, other = setup
    result = suggest(app, source, other)
    with connect(app.config.database_path) as db:
        # A different preserved version already exists after ingesting another source.
        new = app.ingest_upload("second.md", b"new text", request_id="second")
        db.execute(
            "UPDATE sources SET current_version_id=? WHERE id=?",
            (new["version_id"], source["source_id"]),
        )
        db.commit()
    with pytest.raises(RuminateError, match="newer version"):
        app.organization.decide(result["id"], "apply", request_id="apply")
    assert app.organization.list_suggestions()["items"][0]["status"] == "pending"


def test_secondary_thought_topic_is_consistent_across_readers(setup):
    app, _, claims, topic, other = setup
    cid = claims[0]["claim_id"]
    result = app.organization.suggest(
        target_kind="claim",
        target_id=cid,
        topic_id=other["topic_id"],
        rationale="这个问题也涉及记忆",
        auto_apply=True,
        request_id="claim-tag",
    )
    assert app.knowledge.get_claim(cid)["topic_id"] == topic["topic_id"]
    assert len(app.knowledge.list_claims(other["topic_id"])) == 1
    assert app.knowledge.topic_context(other["topic_id"])["claims"][0]["claim_id"] == cid
    assert app.management.list_items(view="thoughts", topic=other["topic_id"])["total"] == 1
    app.reviews.tick(other["topic_id"])
    assert app.reviews.list_queue(other["topic_id"])[0]["claim_id"] == cid
    app.organization.decide(result["id"], "revert", request_id="undo")
    assert app.management.list_items(view="thoughts", topic=other["topic_id"])["total"] == 0


def test_backup_roundtrip_includes_guides_notes_suggestions(setup, tmp_path):
    app, source, claims, _, other = setup
    app.thinking.save_guide(**guide(claims[0], claims[1]), request_id="guide")
    app.thinking.save_note(
        claim_id=claims[0]["claim_id"], text="my note", origin="human", request_id="note"
    )
    result = suggest(app, source, other, auto_apply=True)
    archive = tmp_path / "backup.zip"
    app.backups.export_backup(archive)
    restored = Application(Config(tmp_path / "restored"))
    restored.backups.import_backup(archive)
    assert restored.thinking.context(claims[0]["claim_id"]) == app.thinking.context(
        claims[0]["claim_id"]
    )
    restored.organization.decide(result["id"], "revert", request_id="undo")
    assert not restored.list_sources(topic=other["topic_id"])


def test_output_retains_secondary_topic_provenance_after_undo(setup, tmp_path):
    app, _, claims, _, other = setup
    cid = claims[0]["claim_id"]
    suggestion = app.organization.suggest(
        target_kind="claim",
        target_id=cid,
        topic_id=other["topic_id"],
        rationale="关联的研究问题",
        auto_apply=True,
        request_id="tag",
    )
    project = app.outputs.create_project(
        topic_id=other["topic_id"],
        title="跨主题输出",
        kind="research_note",
        target_audience="",
        goal="",
        request_id="project",
    )
    app.outputs.create_draft(project["project_id"], claim_ids=[cid], request_id="draft")
    original = app.outputs.get_revision(project["project_id"])
    app.organization.decide(suggestion["id"], "revert", request_id="undo")
    assert app.outputs.get_revision(project["project_id"]) == original
    archive = tmp_path / "cross-topic.zip"
    app.backups.export_backup(archive)
    restored = Application(Config(tmp_path / "restored-output"))
    restored.backups.import_backup(archive)
    assert restored.outputs.get_revision(project["project_id"]) == original


def test_cli_uses_same_thinking_service(setup, capsys):
    import json

    from ruminate.cli import main

    app, _, claims, _, other = setup
    cid = claims[0]["claim_id"]
    args = ["--data-dir", str(app.config.data_dir)]
    assert (
        main(
            [
                *args,
                "thinking",
                "note",
                cid,
                "--text",
                "CLI note",
                "--origin",
                "human",
                "--request-id",
                "cli-note",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["origin"] == "human"
    assert main([*args, "thinking", "get", cid]) == 0
    assert json.loads(capsys.readouterr().out)["note_count"] == 1
    assert (
        main(
            [
                *args,
                "topics",
                "suggest",
                "claim",
                cid,
                "--topic",
                other["topic_id"],
                "--rationale",
                "Related question",
                "--auto-apply",
                "--request-id",
                "cli-tag",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert (
        main(
            [
                *args,
                "thinking",
                "guide",
                cid,
                "--lens",
                "boundary",
                "--spark",
                "A contrast",
                "--prompt",
                "When?",
                "--evidence-id",
                claims[0]["evidence"][0]["evidence_id"],
                "--request-id",
                "cli-guide",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["origin"] == "host_model"
