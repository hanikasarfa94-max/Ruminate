"""Knowledge semantics on real stored source versions, never model self-evaluation."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.knowledge import KnowledgeService
from ruminate.persistence.database import connect


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Application, KnowledgeService, dict]:
    config = Config(tmp_path / "store", network_mode="off", allowed_roots=(tmp_path,))
    app = Application(config)
    app.initialize()
    knowledge = KnowledgeService(config)
    topic = knowledge.create_topic(
        slug="agent-evaluation", name="Agent evaluation", request_id="topic-init"
    )
    return app, knowledge, topic


def add_source(app: Application, tmp_path: Path, text: str, name: str = "source.md") -> dict:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    result = app.ingest_file(path, topic="agent-evaluation")
    return app.get_source(result["source_id"], version_id=result["version_id"])


def claim(knowledge: KnowledgeService, **changes: object) -> dict:
    args = dict(
        topic_id="agent-evaluation",
        statement="Agents may need additional evaluation.",
        claim_kind="source_assertion",
        origin="human",
        request_id="claim-default",
    )
    return knowledge.create_claim(**{**args, **changes})


def test_topic_creation_reuses_idempotent_result_and_checks_conflicts(workspace: tuple) -> None:
    _, service, topic = workspace
    assert (
        service.create_topic(
            slug="agent-evaluation", name="Agent evaluation", request_id="topic-init"
        )
        == topic
    )
    assert service.list_topics()[0]["topic_id"] == topic["topic_id"]
    with pytest.raises(RuminateError) as error:
        service.create_topic(slug="changed", name="Changed", request_id="topic-init")
    assert error.value.code == "idempotency_conflict"
    with pytest.raises(RuminateError):
        service.create_topic(slug="agent-evaluation", name="Another topic", request_id="new")


def test_claim_without_evidence_never_becomes_confirmed(workspace: tuple) -> None:
    _, service, _ = workspace
    created = claim(service, origin="host_model")
    assert created["status"] == "candidate"
    assert created["evidence_status"] == "needs_verification"
    assert created["evidence"] == []
    context = service.topic_context("agent-evaluation")
    assert context["confirmed_beliefs"] == []
    assert context["supported"] == []


def test_quote_and_locator_pin_original_block_after_source_changes(
    workspace: tuple, tmp_path: Path
) -> None:
    app, service, _ = workspace
    original = add_source(app, tmp_path, "The first study reports 7 successful tasks.")
    block = original["blocks"][0]
    created = claim(service)
    quote = "7 successful tasks"
    start = block["text"].index(quote)
    linked = service.link_evidence(
        claim_id=created["claim_id"],
        block_id=block["block_id"],
        relation="supports",
        start_char=start,
        end_char=start + len(quote),
        request_id="link-1",
    )
    assert linked["quote"] == quote
    assert linked["version_id"] == original["version_id"]
    assert linked["locator"] == block["locator"]
    assert linked["verifier_status"] == "exact_span"
    assert linked["semantic_verification"] == "not_verified"
    add_source(app, tmp_path, "The revised study reports 3 successful tasks.")
    preserved = service.get_claim(created["claim_id"])
    assert preserved["evidence"][0]["quote"] == quote
    assert preserved["evidence"][0]["version_id"] == original["version_id"]
    assert preserved["evidence_status"] == "supported"
    assert preserved["status"] == "candidate"
    context = service.topic_context("agent-evaluation")
    reference = context["supported"][0]["evidence_refs"][0]
    assert reference["block_id"] == block["block_id"]
    assert reference["version_id"] == original["version_id"]
    assert reference["locator"] == block["locator"]
    assert any(item["version_id"] == original["version_id"] for item in context["sources"])


@pytest.mark.parametrize("kind", ["model_inference", "hypothesis", "question"])
def test_supporting_links_do_not_promote_inferences_or_hypotheses(
    workspace: tuple, tmp_path: Path, kind: str
) -> None:
    app, service, _ = workspace
    source = add_source(app, tmp_path, "An observed study result.")
    created = claim(service, claim_kind=kind, origin="host_model")
    service.link_evidence(
        claim_id=created["claim_id"],
        block_id=source["blocks"][0]["block_id"],
        relation="supports",
        request_id="support",
    )
    after = service.get_claim(created["claim_id"])
    assert after["claim_kind"] == kind
    assert after["evidence_status"] != "supported"
    assert service.topic_context("agent-evaluation")["supported"] == []


@pytest.mark.parametrize("start,end", [(-1, 2), (0, 999), (2, 1), (1, 1), (True, 2), (0, False)])
def test_invalid_evidence_spans_do_not_create_records(
    workspace: tuple, tmp_path: Path, start: object, end: object
) -> None:
    app, service, _ = workspace
    source = add_source(app, tmp_path, "Short evidence.")
    created = claim(service)
    with pytest.raises(RuminateError):
        service.link_evidence(
            claim_id=created["claim_id"],
            block_id=source["blocks"][0]["block_id"],
            relation="supports",
            start_char=start,
            end_char=end,
            request_id="invalid-span",
        )
    assert service.get_claim(created["claim_id"])["evidence"] == []


def test_invalid_link_and_duplicate_span_are_atomic(workspace: tuple, tmp_path: Path) -> None:
    app, service, _ = workspace
    source = add_source(app, tmp_path, "Evidence text.")
    created = claim(service)
    with pytest.raises(RuminateError):
        service.link_evidence(
            claim_id=created["claim_id"],
            block_id="missing-block",
            relation="supports",
            request_id="missing",
        )
    args = dict(
        claim_id=created["claim_id"],
        block_id=source["blocks"][0]["block_id"],
        relation="supports",
        request_id="valid",
    )
    first = service.link_evidence(**args)
    assert service.link_evidence(**args) == first
    assert (
        service.link_evidence(**{**args, "request_id": "same-span"})["evidence_id"]
        == first["evidence_id"]
    )
    assert len(service.get_claim(created["claim_id"])["evidence"]) == 1
    with pytest.raises(RuminateError) as changed:
        service.link_evidence(**{**args, "relation": "challenges"})
    assert changed.value.code == "idempotency_conflict"


def test_confirmed_user_beliefs_require_explicit_human_origin(workspace: tuple) -> None:
    _, service, _ = workspace
    args = dict(
        topic_id="agent-evaluation",
        text="I prefer repeatable evaluations.",
        provenance="Explicit statement in my note",
        request_id="belief-1",
    )
    candidate = service.create_belief(**args)
    assert candidate["status"] == "candidate"
    assert candidate["user_confirmed_at"] is None
    with pytest.raises(RuminateError):
        service.create_belief(
            **{
                **args,
                "confirmed": True,
                "origin": "host_model",
                "request_id": "host-cannot-confirm",
            }
        )
    confirmed = service.create_belief(**{**args, "confirmed": True, "request_id": "user-yes"})
    assert confirmed["status"] == "confirmed"
    assert confirmed["user_confirmed_at"]
    context = service.topic_context("agent-evaluation")
    assert [item["belief_id"] for item in context["confirmed_beliefs"]] == [confirmed["belief_id"]]
    assert [item["belief_id"] for item in context["belief_candidates"]] == [candidate["belief_id"]]


def test_belief_relations_require_real_objects_same_topic(workspace: tuple) -> None:
    _, service, _ = workspace
    created = claim(service)
    belief = service.create_belief(
        topic_id="agent-evaluation",
        text="My stated position.",
        provenance="Explicit test input",
        confirmed=True,
        request_id="belief",
    )
    linked = service.link_belief(
        belief_id=belief["belief_id"],
        claim_id=created["claim_id"],
        relation="challenges",
        justification="A possible counterexample, to review.",
        request_id="r",
    )
    assert linked["relation"] == "challenges"
    assert linked["semantic_verification"] == "not_verified"
    other = service.create_topic(slug="other-topic", name="Other", request_id="other-topic")
    other_claim = claim(service, topic_id=other["topic_id"], request_id="other-claim")
    with pytest.raises(RuminateError):
        service.link_belief(
            belief_id=belief["belief_id"],
            claim_id=other_claim["claim_id"],
            relation="challenges",
            justification="Different topic",
            request_id="cross-topic",
        )


def test_context_separates_contested_claims_and_remains_readonly(
    workspace: tuple, tmp_path: Path
) -> None:
    app, service, _ = workspace
    first = add_source(app, tmp_path, "A study reports improvement.", "first.md")
    second = add_source(app, tmp_path, "Another study reports regression.", "second.md")
    created = claim(service)
    for index, (source, relation) in enumerate(((first, "supports"), (second, "challenges"))):
        service.link_evidence(
            claim_id=created["claim_id"],
            block_id=source["blocks"][0]["block_id"],
            relation=relation,
            request_id=f"link-{index}",
        )
    with connect(app.config.database_path, readonly=True) as connection:
        before = "\n".join(connection.iterdump())
    context = service.topic_context("agent-evaluation")
    assert context["supported"] == []
    assert context["contested"][0]["claim_id"] == created["claim_id"]
    assert context["contested"][0]["evidence_status"] == "contested"
    assert context["content_trust"] == "untrusted_research_data"
    with connect(app.config.database_path, readonly=True) as connection:
        assert "\n".join(connection.iterdump()) == before


def test_concurrent_request_creates_one_claim_and_one_audit(workspace: tuple) -> None:
    app, service, _ = workspace
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: claim(service), range(4)))
    assert len({result["claim_id"] for result in results}) == 1
    with connect(app.config.database_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"claim_kind": "confirmed_fact"},
        {"origin": "unknown"},
        {"statement": ""},
        {"statement": "x" * 2001},
        {"request_id": ""},
        {"topic_id": "missing"},
    ],
)
def test_invalid_claim_input_is_rejected(workspace: tuple, changes: dict) -> None:
    _, service, _ = workspace
    with pytest.raises(RuminateError):
        claim(service, **changes)


@pytest.mark.parametrize("limit", [0, 51, True, -1])
def test_read_limits_rejected(workspace: tuple, limit: object) -> None:
    _, service, _ = workspace
    with pytest.raises(RuminateError):
        service.list_claims("agent-evaluation", limit=limit)


def test_uninitialized_reads_do_not_create_store(tmp_path: Path) -> None:
    config = Config(tmp_path / "absent")
    with pytest.raises(RuminateError):
        KnowledgeService(config).list_topics()
    assert not config.data_dir.exists()
