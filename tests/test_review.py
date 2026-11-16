"""Deterministic review acceptance: evidence, confirmation, throttling and feedback."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.knowledge import KnowledgeService
from ruminate.persistence.common import timestamp
from ruminate.persistence.database import connect
from ruminate.review import DEFAULT_WEIGHTS, ReviewService

NOW = datetime(2026, 11, 12, 3, tzinfo=UTC)


class World:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.config = Config(directory / "data", network_mode="off", allowed_roots=(directory,))
        self.app = Application(self.config)
        self.app.initialize()
        self.knowledge = KnowledgeService(self.config)
        self.review = ReviewService(self.config)
        self.serial = 0

    def key(self) -> str:
        self.serial += 1
        return f"request-{self.serial}"

    def topic(self, slug: str = "research") -> str:
        return str(
            self.knowledge.create_topic(
                slug=slug,
                name=slug,
                request_id=self.key(),
            )["topic_id"]
        )

    def claim(self, topic: str, *, kind: str = "source_assertion") -> str:
        return str(
            self.knowledge.create_claim(
                topic_id=topic,
                statement=f"Research assertion {self.key()}",
                claim_kind=kind,
                origin="human",
                request_id=self.key(),
            )["claim_id"]
        )

    def evidence(self, claim: str, *, relation: str = "supports", text: str | None = None) -> dict:
        path = self.directory / f"{self.key()}.md"
        path.write_text(text or f"Traceable preserved evidence for {self.key()}.", encoding="utf-8")
        source = self.app.ingest_file(path)
        block = self.app.get_source(source["source_id"])["blocks"][0]
        self.knowledge.link_evidence(
            claim_id=claim,
            block_id=block["block_id"],
            relation=relation,
            request_id=self.key(),
        )
        return block

    def belief(self, topic: str, *, confirmed: bool = True, origin: str = "human") -> str:
        return str(
            self.knowledge.create_belief(
                topic_id=topic,
                text="Previous view explicitly provided by user",
                provenance="Self-authored test user statement",
                confirmed=confirmed,
                origin=origin,
                request_id=self.key(),
            )["belief_id"]
        )

    def belief_link(self, belief: str, claim: str, relation: str = "challenges") -> None:
        self.knowledge.link_belief(
            belief_id=belief,
            claim_id=claim,
            relation=relation,
            justification="Explicit test mapping; semantic implication remains unverified",
            request_id=self.key(),
        )

    def conflict(self, topic: str | None = None) -> tuple[str, str]:
        selected_topic = topic or self.topic()
        claim = self.claim(selected_topic)
        self.evidence(claim)
        belief = self.belief(selected_topic)
        self.belief_link(belief, claim)
        return claim, belief

    def project(self, topic: str) -> None:
        with connect(self.config.database_path) as connection:
            connection.execute(
                "INSERT INTO output_projects(id,topic_id,title,kind,created_at) VALUES(?,?,?,?,?)",
                (self.key(), topic, "Active task", "research_note", timestamp(NOW)),
            )

    def dump(self) -> str:
        with connect(self.config.database_path, readonly=True) as connection:
            return "\n".join(connection.iterdump())


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_confirmed_belief_opposing_evidence_preserves_both_sides(world: World) -> None:
    topic = world.topic()
    claim, belief = world.conflict(topic)
    old_claim = world.claim(topic)
    old_block = world.evidence(old_claim)
    world.belief_link(belief, old_claim, "supports")
    result = world.review.tick(now=NOW)
    assert result["created"] == 1
    card = world.review.list_queue(now=NOW)[0]
    assert card["reason"] == "possible_conflict"
    assert card["old_belief_id"] == belief and card["claim_id"] == claim
    assert card["uncertainty"] == "requires_user_review"
    assert card["supports"][0]["block_id"] == old_block["block_id"]
    assert card["challenges"][0]["claim_relation"] == "supports"
    assert card["challenges"][0]["belief_relation"] == "challenges"
    for item in card["evidence"]:
        source = world.app.get_source(item["source_id"], version_id=item["version_id"])
        block = next(b for b in source["blocks"] if b["block_id"] == item["block_id"])
        assert item["quote"] == block["text"][item["span"]["start"] : item["span"]["end"]]
        assert item["locator"] == block["locator"]
    score = card["score_breakdown"]
    assert all(0 <= value <= 1 for value in score["features"].values())
    assert card["priority"] == pytest.approx(
        sum(score["weighted"].values()) - score["fatigue_penalty"]
    )


def test_inferred_or_unconfirmed_beliefs_never_trigger_conflict(world: World) -> None:
    topic = world.topic()
    claim = world.claim(topic, kind="model_inference")
    world.evidence(claim)
    candidate = world.belief(topic, confirmed=False, origin="host_model")
    world.belief_link(candidate, claim)
    assert world.review.tick(now=NOW)["created"] == 0
    confirmed = world.belief(topic)
    world.belief_link(confirmed, claim)
    world.review.tick(now=NOW)
    assert world.review.list_queue(now=NOW)[0]["reason"] == "possible_conflict"
    with connect(world.config.database_path, readonly=True) as connection:
        rows = connection.execute(
            "SELECT status,origin FROM user_beliefs ORDER BY created_at"
        ).fetchall()
    assert [(row["status"], row["origin"]) for row in rows] == [
        ("candidate", "host_model"),
        ("confirmed", "human"),
    ]


@pytest.mark.parametrize("relation", [None, "mentions", "unclear", "challenges"])
def test_conflict_requires_supporting_evidence_for_linked_claim(
    world: World, relation: str | None
) -> None:
    topic = world.topic()
    claim = world.claim(topic)
    if relation is not None:
        world.evidence(claim, relation=relation)
    world.belief_link(world.belief(topic), claim)
    assert world.review.tick(now=NOW)["created"] == 0


def test_dry_run_and_list_are_readonly_and_tick_is_idempotent(world: World) -> None:
    world.conflict()
    before = world.dump()
    preview = world.review.tick(dry_run=True, now=NOW)
    assert preview["created"] == 0 and preview["would_create"] == 1
    assert world.dump() == before
    assert world.review.list_queue(now=NOW) == []
    assert world.dump() == before
    actual = world.review.tick(now=NOW)
    assert actual["candidates"][0]["id"] == preview["candidates"][0]["id"]
    written = world.dump()
    assert world.review.tick(now=NOW)["created"] == 0
    assert world.dump() == written
    assert world.review.list_queue(now=NOW) == world.review.list_queue(now=NOW)
    assert world.dump() == written


def test_empty_store_generates_nothing_and_unbound_claims_are_not_filler(world: World) -> None:
    assert world.review.tick(now=NOW)["created"] == 0
    topic = world.topic()
    world.claim(topic)
    assert world.review.tick(now=NOW)["created"] == 0
    question = world.claim(topic, kind="question")
    result = world.review.tick(now=NOW)
    assert result["created"] == 1
    assert result["candidates"][0]["claim_id"] == question
    assert result["candidates"][0]["supports"] == []
    assert result["candidates"][0]["challenges"] == []


def test_active_project_and_contested_evidence_have_explicit_reasons(world: World) -> None:
    topic = world.topic()
    world.project(topic)
    gap = world.claim(topic, kind="hypothesis")
    supported = world.claim(topic)
    world.evidence(supported)
    contested = world.claim(topic)
    world.evidence(contested)
    world.evidence(contested, relation="challenges")
    cards = world.review.tick(now=NOW)["candidates"]
    reasons = {card["claim_id"]: card["reason"] for card in cards}
    assert reasons == {
        gap: "evidence_gap",
        supported: "active_output",
        contested: "contested_evidence",
    }
    assert all(card["score_breakdown"]["features"]["task_urgency"] == 1 for card in cards)


def test_daily_cap_is_global_and_preview_does_not_consume_it(world: World) -> None:
    topics = [world.topic("a"), world.topic("b")]
    for topic in topics:
        for _ in range(3):
            world.claim(topic, kind="question")
    world.review.tick(now=NOW)
    assert len(world.review.list_queue(now=NOW)) == 3
    assert len(world.review.list_queue(now=NOW)) == 3
    first = world.review.present(topic_id=topics[0], request_id="shown-a", now=NOW)
    assert first["shown"] == 2 and first["daily_remaining"] == 1
    assert world.review.present(topic_id=topics[0], request_id="shown-a", now=NOW) == first
    second = world.review.present(topic_id=topics[1], request_id="shown-b", now=NOW)
    assert second["shown"] == 1 and second["daily_remaining"] == 0
    assert world.review.list_queue(now=NOW) == []
    assert world.review.present(request_id="exhausted", now=NOW)["shown"] == 0
    with connect(world.config.database_path, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM review_events WHERE action='shown'"
            ).fetchone()[0]
            == 3
        )


def test_same_source_is_shown_at_most_once_per_day(world: World) -> None:
    topic = world.topic()
    first = world.claim(topic)
    block = world.evidence(first)
    second = world.claim(topic)
    world.knowledge.link_evidence(
        claim_id=second,
        block_id=block["block_id"],
        relation="supports",
        request_id=world.key(),
    )
    world.project(topic)
    world.review.tick(now=NOW)
    assert len(world.review.list_queue(now=NOW)) == 1
    assert world.review.present(request_id="source-one", now=NOW)["shown"] == 1
    assert world.review.list_queue(now=NOW) == []


def test_midnight_resets_quota_but_does_not_repeat_cards_within_24_hours(world: World) -> None:
    first_topic, second_topic = world.topic("early"), world.topic("late")
    for topic in (first_topic, second_topic):
        for _ in range(3):
            world.claim(topic, kind="question")
    evening = NOW.replace(hour=15, minute=50)  # 23:50 Asia/Shanghai
    world.review.tick(now=evening)
    first = world.review.present(request_id="before-midnight", now=evening)
    assert first["shown"] == 3
    after_midnight = evening + timedelta(minutes=20)
    second = world.review.present(request_id="after-midnight", now=after_midnight)
    assert second["shown"] == 3
    assert {item["id"] for item in first["cards"]}.isdisjoint(
        item["id"] for item in second["cards"]
    )


def test_feedback_postpone_and_dismiss_suppress_repeated_generation(world: World) -> None:
    world.conflict()
    world.review.tick(now=NOW)
    card = world.review.list_queue(now=NOW)[0]
    result = world.review.feedback(
        card["id"], "postpone", postpone_days=7, request_id="later", now=NOW
    )
    assert result["status"] == "postponed"
    assert world.review.list_queue(now=NOW + timedelta(days=6)) == []
    assert world.review.tick(now=NOW + timedelta(days=6))["created"] == 0
    assert len(world.review.list_queue(now=NOW + timedelta(days=7))) == 1
    dismissed = world.review.feedback(
        card["id"], "dismiss", request_id="dismiss", now=NOW + timedelta(days=7)
    )
    assert dismissed["status"] == "dismissed"
    assert (
        world.review.feedback(
            card["id"], "dismiss", request_id="dismiss", now=NOW + timedelta(days=8)
        )
        == dismissed
    )
    assert world.review.tick(now=NOW + timedelta(days=90))["created"] == 0
    assert world.review.list_queue(now=NOW + timedelta(days=90)) == []


@pytest.mark.parametrize(
    "action,status,days",
    [
        ("useful", "resolved", 0),
        ("not_useful", "dismissed", 0),
        ("already_known", "dismissed", 0),
        ("needs_more_evidence", "postponed", 7),
        ("discuss", "postponed", 1),
    ],
)
def test_feedback_lifecycle(world: World, action: str, status: str, days: int) -> None:
    world.conflict()
    world.review.tick(now=NOW)
    card = world.review.list_queue(now=NOW)[0]
    result = world.review.feedback(card["id"], action, request_id=action, now=NOW)
    assert result["status"] == status
    assert world.review.list_queue(now=NOW) == []
    if days:
        assert len(world.review.list_queue(now=NOW + timedelta(days=days))) == 1
    else:
        with pytest.raises(RuminateError) as closed:
            world.review.feedback(card["id"], "discuss", request_id="changed", now=NOW)
        assert closed.value.code == "review_closed"


def test_concurrent_present_never_exceeds_global_quota(world: World) -> None:
    for topic_slug in ("a", "b", "c"):
        topic = world.topic(topic_slug)
        for _ in range(2):
            world.claim(topic, kind="question")
    world.review.tick(now=NOW)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda key: ReviewService(world.config).present(request_id=key, now=NOW),
                ["parallel-a", "parallel-b", "parallel-c", "parallel-d"],
            )
        )
    assert sum(result["shown"] for result in results) == 3
    with connect(world.config.database_path, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM review_events WHERE action='shown'"
            ).fetchone()[0]
            == 3
        )


def test_feedback_and_present_request_ids_detect_different_payloads(world: World) -> None:
    world.conflict()
    world.review.tick(now=NOW)
    card = world.review.list_queue(now=NOW)[0]
    world.review.present(limit=1, request_id="presentation", now=NOW)
    with pytest.raises(RuminateError) as conflict:
        world.review.present(limit=2, request_id="presentation", now=NOW)
    assert conflict.value.code == "idempotency_conflict"
    world.review.feedback(card["id"], "postpone", request_id="feedback", now=NOW)
    with pytest.raises(RuminateError) as conflict:
        world.review.feedback(card["id"], "dismiss", request_id="feedback", now=NOW)
    assert conflict.value.code == "idempotency_conflict"


@pytest.mark.parametrize("limit", [0, 4, True, 1.5])
def test_queue_limits_are_bounded(world: World, limit: Any) -> None:
    with pytest.raises(RuminateError) as invalid:
        world.review.list_queue(limit=limit)
    assert invalid.value.code == "invalid_limit"


def test_invalid_feedback_time_and_weights_do_not_write(world: World) -> None:
    world.conflict()
    world.review.tick(now=NOW)
    card = world.review.list_queue(now=NOW)[0]
    before = world.dump()
    for action, days in [("invented", 7), ("postpone", 0), ("postpone", True), ("postpone", 366)]:
        with pytest.raises(RuminateError):
            world.review.feedback(card["id"], action, postpone_days=days, request_id="bad", now=NOW)
    with pytest.raises(RuminateError) as missing:
        world.review.feedback("missing", "useful", request_id="missing", now=NOW)
    assert missing.value.code == "review_not_found"
    with pytest.raises(RuminateError) as naive:
        world.review.tick(now=NOW.replace(tzinfo=None))
    assert naive.value.code == "invalid_time"
    with pytest.raises(RuminateError):
        ReviewService(world.config, weights={**DEFAULT_WEIGHTS, "relevance": 0.9})
    with pytest.raises(RuminateError):
        ReviewService(world.config, weights={**DEFAULT_WEIGHTS, "relevance": float("nan")})
    assert world.dump() == before


def test_fatigue_score_is_explained_for_new_candidates(world: World) -> None:
    topic = world.topic()
    world.claim(topic, kind="question")
    world.review.tick(now=NOW)
    world.review.present(request_id="exposure", now=NOW)
    world.claim(topic, kind="question")
    new = world.review.tick(now=NOW + timedelta(hours=1))["candidates"][0]
    assert new["score_breakdown"]["fatigue_penalty"] == 0.05


def test_uninitialized_read_and_dry_run_never_create_store(tmp_path: Path) -> None:
    config = Config(tmp_path / "absent")
    review = ReviewService(config)
    with pytest.raises(RuminateError):
        review.tick(dry_run=True)
    with pytest.raises(RuminateError):
        review.list_queue()
    assert not config.data_dir.exists()


def test_timezone_selection_is_configurable(world: World) -> None:
    utc = ReviewService(replace(world.config, review_timezone="UTC"))
    assert str(utc.timezone) == "UTC"


def test_many_links_do_not_hide_counter_evidence_or_old_belief_evidence(world: World) -> None:
    topic = world.topic()
    claim, belief = world.conflict(topic)
    for _ in range(21):
        world.evidence(claim)
    challenge = world.evidence(claim, relation="challenges")
    old_claim = world.claim(topic)
    old_block = world.evidence(old_claim)
    world.belief_link(belief, old_claim, "supports")
    cards = world.review.tick(now=NOW)["candidates"]
    conflict = next(card for card in cards if card["reason"] == "possible_conflict")
    contested = next(card for card in cards if card["reason"] == "contested_evidence")
    assert conflict["supports"][0]["block_id"] == old_block["block_id"]
    assert conflict["challenges"]
    assert contested["challenges"][0]["block_id"] == challenge["block_id"]
    assert len(conflict["evidence"]) <= 20 and len(contested["evidence"]) <= 20
