"""Management must show durable knowledge without consuming proactive review quotas."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import connect
from ruminate.workspace import WorkspaceAdapter

NOW = datetime(2026, 11, 16, 1, tzinfo=UTC)


@pytest.fixture
def app(tmp_path: Path) -> Application:
    application = Application(
        Config(tmp_path / "data", network_mode="off", allowed_roots=(tmp_path,))
    )
    application.initialize()
    return application


def seed(app: Application, count: int = 5) -> tuple[str, list[str]]:
    topic = app.knowledge.create_topic(slug="agents", name="Agent 研究", request_id="topic")
    source = app.ingest_upload(
        "note.md", "# Evidence\n\n原文 😀 精确定位不能丢失。".encode(),
        topic="agents", request_id="upload",
    )
    block = app.get_source(source["source_id"])["blocks"][1]
    ids = []
    for index in range(count):
        claim = app.knowledge.create_claim_with_evidence(
            topic_id=topic["topic_id"],
            statement=(f"问题 {index}？\n\n为什么值得回看：生成建议也需要出处。"
                       "\n\n下一步：检查原文。"),
            claim_kind="question", origin="host_model", block_id=block["block_id"],
            relation="mentions", request_id=f"claim-{index}",
        )
        ids.append(claim["claim_id"])
    app.reviews.tick(topic_id=topic["topic_id"], now=NOW)
    return topic["topic_id"], ids


def snapshot(app: Application) -> str:
    with connect(app.config.database_path, readonly=True) as db:
        return "\n".join(db.iterdump())


def test_full_pending_list_ignores_source_diversity_and_daily_quota(app: Application) -> None:
    topic, ids = seed(app)
    # The proactive queue is intentionally limited to one item from the same source.
    assert len(app.reviews.list_queue(topic_id=topic, now=NOW)) == 1
    before = snapshot(app)
    first = app.management.list_items(view="thoughts", status="pending", limit=2, now=NOW)
    second = app.management.list_items(
        view="thoughts", status="pending", limit=2, offset=first["next_offset"], now=NOW
    )
    third = app.management.list_items(
        view="thoughts", status="pending", limit=2, offset=second["next_offset"], now=NOW
    )
    items = first["items"] + second["items"] + third["items"]
    assert first["total"] == first["pending_count"] == 5
    assert third["next_offset"] is None
    assert {item["claim_id"] for item in items} == set(ids)
    assert all(item["origin"] == "host_model" and item["evidence_count"] == 1 for item in items)
    assert app.management.list_items(view="sources", now=NOW)["total"] == 1
    assert snapshot(app) == before  # No exposure, feedback, inference, or content writes.
    shown = app.reviews.present(topic_id=topic, request_id="present", now=NOW)
    assert len(shown["cards"]) == 1
    assert not app.reviews.list_queue(topic_id=topic, now=NOW)
    assert app.management.list_items(view="thoughts", status="pending", now=NOW)["total"] == 4


def test_feedback_lifecycle_changes_pending_without_changing_claim_or_quote(
    app: Application,
) -> None:
    _, ids = seed(app, 3)
    originals = [app.knowledge.get_claim(claim_id) for claim_id in ids]
    rows = app.management.list_items(view="thoughts", status="pending", now=NOW)["items"]
    for row, action in zip(rows, ["postpone", "useful", "dismiss"], strict=True):
        app.reviews.feedback(row["review_id"], action, request_id=action, now=NOW)
    assert app.management.list_items(view="thoughts", status="pending", now=NOW)["total"] == 0
    assert app.management.list_items(view="thoughts", status="later", now=NOW)["total"] == 1
    assert app.management.list_items(view="thoughts", status="processed", now=NOW)["total"] == 2
    assert app.management.list_items(view="thoughts", now=NOW)["total"] == 3
    future = NOW + timedelta(days=7)
    assert app.management.list_items(view="thoughts", status="pending", now=future)["total"] == 1
    assert app.management.list_items(view="thoughts", status="later", now=future)["total"] == 0
    assert [app.knowledge.get_claim(claim_id) for claim_id in ids] == originals
    with connect(app.config.database_path, readonly=True) as db:
        shown = db.execute("SELECT COUNT(*) FROM review_events WHERE action='shown'").fetchone()[0]
        assert shown == 0
        assert db.execute("SELECT COUNT(*) FROM user_beliefs").fetchone()[0] == 0


def test_scoped_search_pagination_and_source_fulltext_returns_unique_current_sources(
    app: Application,
) -> None:
    topic, _ = seed(app, 4)
    app.ingest_upload(
        "second.md", "# Second\n\n精确定位重复。\n\n精确定位再次重复。".encode(),
        topic="other", request_id="second",
    )
    result = app.management.list_items(view="sources", query="精确定位", limit=1)
    assert result["total"] == 2
    assert app.management.list_items(view="sources", query="精确定位", topic=topic)["total"] == 1
    remaining = app.management.list_items(view="sources", query="精确定位", offset=1)
    assert result["items"][0]["id"] != remaining["items"][0]["id"]
    assert app.management.list_items(view="sources", query="Evidence")["total"] == 1
    assert app.management.list_items(view="thoughts", query="问题 2", topic=topic)["total"] == 1
    assert app.management.list_items(view="thoughts", query="%_")["total"] == 0
    assert app.management.list_items(view="thoughts", topic="other")["total"] == 0
    project = app.outputs.create_project(
        topic_id=topic, title="Agent 提纲", kind="research_proposal",
        goal="对照组", request_id="project",
    )
    app.outputs.create_draft(project["project_id"], request_id="draft")
    assert app.management.list_items(view="outputs", query="对照组")["total"] == 1
    assert app.management.list_items(view="outputs", query="原文 😀")["total"] == 1
    assert app.management.list_items(view="outputs", query="原文 😀", topic="other")["total"] == 0


def test_multiple_review_reasons_keep_remaining_work_visible(app: Application) -> None:
    topic, ids = seed(app, 1)
    # A second explicit conflict is a separate review of the same thought.
    belief = app.knowledge.create_belief(
        topic_id=topic, text="用户明确说过的观点", provenance="用户测试输入", origin="human",
        confirmed=True, request_id="belief",
    )
    app.knowledge.link_belief(
        belief_id=belief["belief_id"], claim_id=ids[0], relation="challenges",
        justification="测试中显式关联", request_id="link",
    )
    evidence = app.knowledge.get_claim(ids[0])["evidence"][0]
    app.knowledge.link_evidence(
        claim_id=ids[0], block_id=evidence["block_id"], relation="supports",
        request_id="support",
    )
    app.reviews.tick(topic_id=topic, now=NOW)
    first = app.management.list_items(view="thoughts", status="pending", now=NOW)
    assert first["total"] == 1
    app.reviews.feedback(first["items"][0]["review_id"], "useful", request_id="done", now=NOW)
    second = app.management.list_items(view="thoughts", status="pending", now=NOW)
    assert second["total"] == 1
    assert first["items"][0]["review_id"] != second["items"][0]["review_id"]


@pytest.mark.parametrize("params", [
    {"view": "invalid"}, {"sort": "id; DROP TABLE claims"}, {"status": "pending"},
    {"offset": -1}, {"limit": 1000}, {"query": "a" * 513}, {"topic": "missing"},
])
def test_management_rejects_invalid_or_ambiguous_requests(app: Application, params: dict) -> None:
    with pytest.raises(RuminateError):
        app.management.list_items(**params)


def test_workspace_management_uses_application_boundary(app: Application) -> None:
    seed(app)
    result = WorkspaceAdapter(app).read(
        "management", {"view": "thoughts", "status": "pending", "limit": "2", "offset": "1"}
    )
    assert result["total"] == 5
    assert len(result["items"]) == 2
    with pytest.raises(RuminateError):
        WorkspaceAdapter(app).read("management", {"sql": "SELECT * FROM claims"})


def test_confirmed_user_views_stay_distinct_from_ai_suggestions(app: Application) -> None:
    topic, ids = seed(app, 1)
    confirmed = app.knowledge.create_belief(
        topic_id=topic, text="这是用户明确确认的观点", provenance="用户在测试里逐字给出",
        origin="human", confirmed=True, request_id="confirmed",
    )
    app.knowledge.create_belief(
        topic_id=topic, text="模型猜测用户的观点", provenance="未确认的测试候选",
        origin="host_model", request_id="unconfirmed",
    )
    app.knowledge.link_belief(
        belief_id=confirmed["belief_id"], claim_id=ids[0], relation="supports",
        justification="显式关联", request_id="belief-link",
    )
    before = snapshot(app)
    all_items = app.management.list_items(view="thoughts", topic=topic)
    assert all_items["total"] == 2
    assert {item["row_kind"] for item in all_items["items"]} == {"claim", "belief"}
    assert app.management.list_items(view="thoughts", status="pending", now=NOW)["total"] == 1
    assert app.management.list_items(view="thoughts", query="明确确认")["total"] == 1
    detail = WorkspaceAdapter(app).read("belief", {"belief_id": confirmed["belief_id"]})
    assert detail["provenance"] == "用户在测试里逐字给出"
    assert detail["user_confirmed_at"]
    assert detail["related_claims"][0]["claim_id"] == ids[0]
    assert detail["next_offset"] is None
    assert snapshot(app) == before


def test_source_management_searches_current_text_and_citations_keep_old_versions(
    app: Application, tmp_path: Path,
) -> None:
    note = tmp_path / "versioned.md"
    note.write_text("# First\n\nOld purple evidence.", encoding="utf-8")
    old = app.ingest_file(note, topic="versioning")
    block = app.get_source(old["source_id"])["blocks"][1]
    claim = app.knowledge.create_claim_with_evidence(
        topic_id="versioning", statement="What changed?", claim_kind="question",
        origin="host_model", block_id=block["block_id"], relation="mentions",
        request_id="version-claim",
    )
    note.write_text("# Second\n\nNew orange evidence.\n\nMore orange evidence.", encoding="utf-8")
    new = app.ingest_file(note, topic="versioning")
    assert old["source_id"] == new["source_id"]
    assert old["version_id"] != new["version_id"]
    assert app.management.list_items(view="sources", query="purple")["total"] == 0
    current = app.management.list_items(view="sources", query="orange")
    assert current["total"] == 1
    assert current["items"][0]["version_id"] == new["version_id"]
    evidence = app.knowledge.get_claim(claim["claim_id"])["evidence"][0]
    assert evidence["version_id"] == old["version_id"]
    assert evidence["quote"] == "Old purple evidence."
