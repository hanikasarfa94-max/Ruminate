"""Explicit claims and source links; user beliefs are never inferred by this service."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.common import (
    Store,
    audit,
    mutate,
    new_id,
    resolve_topic,
    timestamp,
    validate_limit,
    validate_text,
    validate_topic_slug,
)

CLAIM_KINDS = {"source_assertion", "model_inference", "hypothesis", "question"}
RELATIONS = {"supports", "challenges", "mentions", "unclear"}
CLAIM_ORIGINS = {"human", "host_model", "deterministic"}


def _choice(value: str, name: str, choices: set[str]) -> None:
    if not isinstance(value, str) or value not in choices:
        raise RuminateError(
            "invalid_input", f"Invalid {name}; choose from {', '.join(sorted(choices))}"
        )


def _topic(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "topic_id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "status": row["status"],
    }


def _claim_row(connection: sqlite3.Connection, claim_id: str) -> sqlite3.Row:
    validate_text(claim_id, "claim_id", 100)
    row = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if row is None:
        raise RuminateError("claim_not_found", "Claim does not exist")
    return cast(sqlite3.Row, row)


def _belief_row(connection: sqlite3.Connection, belief_id: str) -> sqlite3.Row:
    validate_text(belief_id, "belief_id", 100)
    row = connection.execute("SELECT * FROM user_beliefs WHERE id=?", (belief_id,)).fetchone()
    if row is None:
        raise RuminateError("belief_not_found", "Belief does not exist")
    return cast(sqlite3.Row, row)


def _belief(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "belief_id": row["id"],
        "topic_id": row["topic_id"],
        "text": row["text"],
        "provenance": row["provenance"],
        "origin": row["origin"],
        "status": row["status"],
        "user_confirmed_at": row["user_confirmed_at"],
        "created_at": row["created_at"],
        "confirmation_attribution": "caller_supplied",
    }


def _evidence(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "evidence_id": row["id"],
        "claim_id": row["claim_id"],
        "block_id": row["block_id"],
        "version_id": row["version_id"],
        "source_id": row["source_id"],
        "title": row["title"],
        "canonical_uri": row["canonical_uri"],
        "source_sha256": row["sha256"],
        "page": row["page"],
        "section": row["section"],
        "locator": json.loads(row["locator_json"]),
        "span": {"start": row["start_char"], "end": row["end_char"]},
        "quote": row["quote"],
        "relation": row["relation"],
        "verifier_status": row["verifier_status"],
        "semantic_verification": "not_verified",
        "content_trust": "untrusted_source_text",
    }


EVIDENCE_SELECT = (
    "SELECT e.*,b.page,b.section,b.locator_json,v.source_id,v.title,v.sha256,s.canonical_uri "
    "FROM claim_evidence e JOIN blocks b ON b.id=e.block_id "
    "JOIN source_versions v ON v.id=e.version_id JOIN sources s ON s.id=v.source_id "
)


def _summary(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    counts = {
        item["relation"]: item["count"]
        for item in connection.execute(
            "SELECT relation,COUNT(*) AS count FROM claim_evidence "
            "WHERE claim_id=? GROUP BY relation",
            (row["id"],),
        )
    }
    kind = row["claim_kind"]
    if kind == "hypothesis":
        state = "hypothesis"
    elif counts.get("challenges", 0):
        state = "contested"
    elif kind == "source_assertion" and counts.get("supports", 0):
        state = "supported"
    else:
        state = "needs_verification"
    return {
        "claim_id": row["id"],
        "topic_id": row["topic_id"],
        "statement": row["statement"],
        "claim_kind": kind,
        "status": row["status"],
        "origin": row["origin"],
        "created_at": row["created_at"],
        "evidence_status": state,
        "evidence_count": sum(counts.values()),
        "supports_count": counts.get("supports", 0),
        "challenges_count": counts.get("challenges", 0),
        "semantic_verification": "not_verified",
    }


def _claim(connection: sqlite3.Connection, claim_id: str) -> dict[str, Any]:
    row = _claim_row(connection, claim_id)
    result = _summary(connection, row)
    links = connection.execute(
        EVIDENCE_SELECT + "WHERE e.claim_id=? ORDER BY e.created_at,e.id LIMIT 20", (claim_id,)
    ).fetchall()
    result.update(
        evidence=[_evidence(link) for link in links],
        evidence_truncated=result["evidence_count"] > len(links),
        content_trust="untrusted_research_data",
        interpretation=(
            "supported indicates a recorded support relation; review status is reported separately"
        ),
    )
    return result


def _selected_evidence(
    connection: sqlite3.Connection,
    block_id: str,
    relation: str,
    start_char: int,
    end_char: int | None,
) -> tuple[sqlite3.Row, int, str]:
    validate_text(block_id, "block_id", 100)
    _choice(relation, "relation", RELATIONS)
    block = connection.execute("SELECT * FROM blocks WHERE id=?", (block_id,)).fetchone()
    if block is None:
        raise RuminateError("block_not_found", "Evidence block does not exist")
    end = len(block["text"]) if end_char is None else end_char
    if (
        type(start_char) is not int
        or type(end) is not int
        or not 0 <= start_char < end <= len(block["text"])
        or end - start_char > 1000
    ):
        raise RuminateError("invalid_span", "Evidence must select 1 to 1000 block characters")
    quote = block["text"][start_char:end]
    if not quote.strip():
        raise RuminateError("invalid_span", "Evidence cannot contain only whitespace")
    return block, end, quote


class KnowledgeService:
    def __init__(self, config: Config) -> None:
        self.store = Store(config)

    def create_topic(
        self, *, slug: str, name: str, description: str = "", request_id: str
    ) -> dict[str, Any]:
        validate_text(slug, "slug", 100)
        validate_topic_slug(slug)
        validate_text(name, "name", 200)
        validate_text(description, "description", 2000, empty=True)
        if slug != slug.strip() or any(char.isspace() for char in slug):
            raise RuminateError("invalid_input", "Topic slug cannot contain whitespace")
        payload = {"slug": slug, "name": name, "description": description}
        with self.store.write() as connection:

            def perform() -> dict[str, Any]:
                existing = connection.execute(
                    "SELECT * FROM topics WHERE slug=?", (slug,)
                ).fetchone()
                if existing is not None:
                    if (existing["name"], existing["description"]) != (name, description):
                        raise RuminateError("topic_exists", "Topic slug is already used")
                    return _topic(existing)
                topic_id = new_id("top")
                connection.execute(
                    "INSERT INTO topics(id,slug,name,description,status) VALUES(?,?,?,?,'active')",
                    (topic_id, slug, name, description),
                )
                audit(connection, "topic_created", topic_id)
                return _topic(resolve_topic(connection, topic_id))

            return mutate(connection, "create_topic", request_id, payload, perform)

    def list_topics(self, limit: int = 20) -> list[dict[str, Any]]:
        validate_limit(limit)
        with self.store.read() as connection:
            return [
                _topic(row)
                for row in connection.execute(
                    "SELECT * FROM topics WHERE status='active' ORDER BY slug LIMIT ?", (limit,)
                )
            ]

    def update_topic(
        self, topic_id: str, *, name: str | None = None,
        description: str | None = None, request_id: str,
    ) -> dict[str, Any]:
        if name is None and description is None:
            raise RuminateError("invalid_input", "Provide a topic name or description")
        if name is not None:
            validate_text(name, "name", 200)
        if description is not None:
            validate_text(description, "description", 2000, empty=True)
        with self.store.write() as connection:
            topic = resolve_topic(connection, topic_id)
            payload = {"topic_id": topic["id"], "name": name, "description": description}

            def perform() -> dict[str, Any]:
                connection.execute(
                    "UPDATE topics SET name=?,description=? WHERE id=?",
                    (name if name is not None else topic["name"],
                     description if description is not None else topic["description"], topic["id"]),
                )
                audit(connection, "topic_updated", topic["id"])
                return _topic(resolve_topic(connection, topic["id"]))

            return mutate(connection, "update_topic", request_id, payload, perform)

    def create_claim(
        self,
        *,
        topic_id: str,
        statement: str,
        claim_kind: str,
        origin: str = "human",
        request_id: str,
    ) -> dict[str, Any]:
        validate_text(statement, "statement", 2000)
        _choice(claim_kind, "claim_kind", CLAIM_KINDS)
        _choice(origin, "origin", CLAIM_ORIGINS)
        with self.store.write() as connection:
            topic = resolve_topic(connection, topic_id)
            payload = {
                "topic_id": topic["id"],
                "statement": statement,
                "claim_kind": claim_kind,
                "origin": origin,
            }

            def perform() -> dict[str, Any]:
                claim_id = new_id("clm")
                connection.execute(
                    "INSERT INTO claims(id,topic_id,statement,claim_kind,origin,status,created_at) "
                    "VALUES(?,?,?,?,?,'candidate',?)",
                    (claim_id, topic["id"], statement, claim_kind, origin, timestamp()),
                )
                audit(
                    connection,
                    "claim_created",
                    claim_id,
                    {"topic_id": topic["id"], "claim_kind": claim_kind, "origin": origin},
                )
                return _claim(connection, claim_id)

            return mutate(connection, "create_claim", request_id, payload, perform)

    def create_claim_with_evidence(
        self,
        *,
        topic_id: str,
        statement: str,
        claim_kind: str,
        block_id: str,
        relation: str = "supports",
        start_char: int = 0,
        end_char: int | None = None,
        origin: str = "human",
        request_id: str,
    ) -> dict[str, Any]:
        """Save a reader's thought and selected original passage in one transaction."""
        validate_text(statement, "statement", 2000)
        _choice(claim_kind, "claim_kind", CLAIM_KINDS)
        _choice(origin, "origin", CLAIM_ORIGINS)
        with self.store.write() as connection:
            topic = resolve_topic(connection, topic_id)
            block, end, quote = _selected_evidence(
                connection, block_id, relation, start_char, end_char
            )
            payload = {
                "topic_id": topic["id"],
                "statement": statement,
                "claim_kind": claim_kind,
                "origin": origin,
                "block_id": block_id,
                "relation": relation,
                "start_char": start_char,
                "end_char": end,
            }

            def perform() -> dict[str, Any]:
                claim_id = new_id("clm")
                evidence_id = new_id("evd")
                created_at = timestamp()
                connection.execute(
                    "INSERT INTO claims(id,topic_id,statement,claim_kind,origin,status,created_at) "
                    "VALUES(?,?,?,?,?,'candidate',?)",
                    (claim_id, topic["id"], statement, claim_kind, origin, created_at),
                )
                connection.execute(
                    "INSERT INTO claim_evidence(id,claim_id,block_id,version_id,relation,"
                    "start_char,end_char,quote,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id,
                        claim_id,
                        block_id,
                        block["version_id"],
                        relation,
                        start_char,
                        end,
                        quote,
                        created_at,
                    ),
                )
                audit(
                    connection,
                    "claim_created",
                    claim_id,
                    {"topic_id": topic["id"], "claim_kind": claim_kind, "origin": origin},
                )
                audit(
                    connection,
                    "evidence_linked",
                    evidence_id,
                    {"claim_id": claim_id, "block_id": block_id, "relation": relation},
                )
                return _claim(connection, claim_id)

            return mutate(connection, "create_claim_with_evidence", request_id, payload, perform)

    def link_evidence(
        self,
        *,
        claim_id: str,
        block_id: str,
        relation: str,
        start_char: int = 0,
        end_char: int | None = None,
        request_id: str,
    ) -> dict[str, Any]:
        with self.store.write() as connection:
            _claim_row(connection, claim_id)
            block, end, quote = _selected_evidence(
                connection, block_id, relation, start_char, end_char
            )
            payload = {
                "claim_id": claim_id,
                "block_id": block_id,
                "relation": relation,
                "start_char": start_char,
                "end_char": end,
            }

            def perform() -> dict[str, Any]:
                existing = connection.execute(
                    EVIDENCE_SELECT + "WHERE e.claim_id=? AND e.block_id=? AND e.relation=? "
                    "AND e.start_char=? AND e.end_char=?",
                    (claim_id, block_id, relation, start_char, end),
                ).fetchone()
                if existing is not None:
                    return _evidence(existing)
                evidence_id = new_id("evd")
                connection.execute(
                    "INSERT INTO claim_evidence(id,claim_id,block_id,version_id,relation,"
                    "start_char,end_char,quote,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id,
                        claim_id,
                        block_id,
                        block["version_id"],
                        relation,
                        start_char,
                        end,
                        quote,
                        timestamp(),
                    ),
                )
                audit(
                    connection,
                    "evidence_linked",
                    evidence_id,
                    {"claim_id": claim_id, "block_id": block_id, "relation": relation},
                )
                row = connection.execute(
                    EVIDENCE_SELECT + "WHERE e.id=?", (evidence_id,)
                ).fetchone()
                assert row is not None
                return _evidence(row)

            return mutate(connection, "link_evidence", request_id, payload, perform)

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        with self.store.read() as connection:
            return _claim(connection, claim_id)

    def get_belief(self, belief_id: str, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        validate_limit(limit)
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise RuminateError("invalid_input", "Invalid offset")
        with self.store.read() as connection:
            connection.execute("BEGIN")
            belief = _belief(_belief_row(connection, belief_id))
            total = connection.execute(
                "SELECT COUNT(*) FROM belief_relations r JOIN claims c ON c.id=r.claim_id "
                "WHERE r.belief_id=? AND c.status='candidate'",
                (belief_id,),
            ).fetchone()[0]
            belief["related_claims"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT c.id AS claim_id,c.statement,c.claim_kind,c.origin,c.created_at,"
                    "r.relation,r.justification FROM belief_relations r JOIN claims c "
                    "ON c.id=r.claim_id WHERE r.belief_id=? AND c.status='candidate' "
                    "ORDER BY c.created_at,c.id LIMIT ? OFFSET ?",
                    (belief_id, limit, offset),
                )
            ]
            belief["related_count"] = total
            belief["next_offset"] = offset + limit if offset + limit < total else None
            return belief

    def list_claims(self, topic_id: str, limit: int = 20) -> list[dict[str, Any]]:
        validate_limit(limit)
        with self.store.read() as connection:
            topic = resolve_topic(connection, topic_id)
            return [
                _summary(connection, row)
                for row in connection.execute(
                    "SELECT * FROM claims WHERE topic_id=? AND status='candidate' "
                    "ORDER BY created_at,id LIMIT ?",
                    (topic["id"], limit),
                )
            ]

    def create_belief(
        self,
        *,
        topic_id: str,
        text: str,
        provenance: str,
        confirmed: bool = False,
        origin: str = "human",
        request_id: str,
    ) -> dict[str, Any]:
        validate_text(text, "belief text", 2000)
        validate_text(provenance, "provenance", 1000)
        _choice(origin, "origin", {"human", "host_model"})
        if type(confirmed) is not bool or (confirmed and origin != "human"):
            raise RuminateError(
                "invalid_confirmation", "Only explicit human input may confirm belief"
            )
        with self.store.write() as connection:
            topic = resolve_topic(connection, topic_id)
            payload = {
                "topic_id": topic["id"],
                "text": text,
                "provenance": provenance,
                "confirmed": confirmed,
                "origin": origin,
            }

            def perform() -> dict[str, Any]:
                belief_id = new_id("bel")
                now = timestamp()
                connection.execute(
                    "INSERT INTO user_beliefs(id,topic_id,text,provenance,origin,status,"
                    "user_confirmed_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        belief_id,
                        topic["id"],
                        text,
                        provenance,
                        origin,
                        "confirmed" if confirmed else "candidate",
                        now if confirmed else None,
                        now,
                    ),
                )
                audit(
                    connection,
                    "belief_created",
                    belief_id,
                    {"topic_id": topic["id"], "confirmed": confirmed, "origin": origin},
                )
                return _belief(_belief_row(connection, belief_id))

            return mutate(connection, "create_belief", request_id, payload, perform)

    def link_belief(
        self, *, belief_id: str, claim_id: str, relation: str, justification: str, request_id: str
    ) -> dict[str, Any]:
        _choice(relation, "relation", RELATIONS)
        validate_text(justification, "justification", 2000)
        with self.store.write() as connection:
            belief = _belief_row(connection, belief_id)
            claim = _claim_row(connection, claim_id)
            if belief["topic_id"] != claim["topic_id"]:
                raise RuminateError(
                    "topic_mismatch", "Belief and claim must belong to the same topic"
                )
            payload = {
                "belief_id": belief_id,
                "claim_id": claim_id,
                "relation": relation,
                "justification": justification,
            }

            def perform() -> dict[str, Any]:
                old = connection.execute(
                    "SELECT * FROM belief_relations "
                    "WHERE belief_id=? AND claim_id=? AND relation=?",
                    (belief_id, claim_id, relation),
                ).fetchone()
                if old is not None:
                    if old["justification"] != justification:
                        raise RuminateError(
                            "relation_exists", "Existing justification is immutable"
                        )
                    relation_id, created_at = old["id"], old["created_at"]
                else:
                    relation_id, created_at = new_id("rel"), timestamp()
                    connection.execute(
                        "INSERT INTO belief_relations(id,belief_id,claim_id,relation,justification,"
                        "created_at) VALUES(?,?,?,?,?,?)",
                        (relation_id, belief_id, claim_id, relation, justification, created_at),
                    )
                    audit(
                        connection,
                        "belief_linked",
                        relation_id,
                        {"belief_id": belief_id, "claim_id": claim_id, "relation": relation},
                    )
                return {
                    "relation_id": relation_id,
                    **payload,
                    "created_at": created_at,
                    "semantic_verification": "not_verified",
                }

            return mutate(connection, "link_belief", request_id, payload, perform)

    def topic_context(self, topic_id: str, max_claims: int = 20) -> dict[str, Any]:
        """Return a bounded map, not model-written conclusions or inferred user beliefs."""
        validate_limit(max_claims, 20)
        with self.store.read() as connection:
            topic = resolve_topic(connection, topic_id)
            rows = connection.execute(
                "SELECT * FROM claims WHERE topic_id=? AND status='candidate' "
                "ORDER BY created_at,id LIMIT ?",
                (topic["id"], max_claims),
            ).fetchall()
            claims = [_summary(connection, row) for row in rows]
            for claim in claims:
                refs = connection.execute(
                    "SELECT e.id AS evidence_id,e.block_id,e.version_id,e.relation,v.source_id,"
                    "v.published_at,v.retrieved_at,b.page,b.section,b.locator_json "
                    "FROM claim_evidence e JOIN blocks b ON b.id=e.block_id "
                    "JOIN source_versions v ON v.id=e.version_id WHERE e.claim_id=? "
                    "ORDER BY e.created_at,e.id LIMIT 3",
                    (claim["claim_id"],),
                ).fetchall()
                claim["evidence_refs"] = []
                for ref in refs:
                    item = dict(ref)
                    item["locator"] = json.loads(item.pop("locator_json"))
                    claim["evidence_refs"].append(item)
                claim["evidence_refs_truncated"] = claim["evidence_count"] > len(refs)
            count = connection.execute(
                "SELECT COUNT(*) FROM claims WHERE topic_id=? AND status='candidate'",
                (topic["id"],),
            ).fetchone()[0]
            beliefs: list[dict[str, Any]] = []
            belief_counts: dict[str, int] = {}
            for status in ("confirmed", "candidate"):
                beliefs.extend(
                    _belief(row)
                    for row in connection.execute(
                        "SELECT * FROM user_beliefs WHERE topic_id=? AND status=? "
                        "ORDER BY created_at DESC,id LIMIT 10",
                        (topic["id"], status),
                    )
                )
                belief_counts[status] = connection.execute(
                    "SELECT COUNT(*) FROM user_beliefs WHERE topic_id=? AND status=?",
                    (topic["id"], status),
                ).fetchone()[0]
            sources = [
                dict(row)
                for row in connection.execute(
                    "SELECT s.id AS source_id,v.id AS version_id,v.title,v.status,"
                    "v.published_at,v.retrieved_at FROM source_versions v "
                    "JOIN sources s ON s.id=v.source_id WHERE v.id IN ("
                    "SELECT e.version_id FROM claim_evidence e JOIN claims c ON c.id=e.claim_id "
                    "WHERE c.topic_id=? AND c.status='candidate' UNION "
                    "SELECT s.current_version_id FROM sources s JOIN source_topics st "
                    "ON s.id=st.source_id WHERE st.topic_id=?) "
                    "ORDER BY v.retrieved_at DESC,v.id LIMIT 10",
                    (topic["id"], topic["id"]),
                )
            ]
            projects = [
                dict(row)
                for row in connection.execute(
                    "SELECT id AS project_id,title,kind,status FROM output_projects "
                    "WHERE topic_id=? "
                    "AND status='active' ORDER BY created_at DESC LIMIT 10",
                    (topic["id"],),
                )
            ]
        return {
            "topic": _topic(topic),
            "claims": claims,
            "claim_count": count,
            "claims_truncated": count > len(claims),
            "supported": [c for c in claims if c["evidence_status"] == "supported"],
            "model_inferences": [
                c for c in claims if c["claim_kind"] in {"model_inference", "hypothesis"}
            ],
            "contested": [c for c in claims if c["challenges_count"] > 0],
            "research_gaps": [
                c
                for c in claims
                if c["evidence_status"] == "needs_verification"
                or c["claim_kind"] in {"hypothesis", "question"}
            ],
            "actionable_experiments": [],
            "experiment_planning_status": "requires_host_or_human_design",
            "confirmed_beliefs": [b for b in beliefs if b["status"] == "confirmed"],
            "belief_candidates": [b for b in beliefs if b["status"] == "candidate"],
            "belief_counts": belief_counts,
            "beliefs_truncated": any(count > 10 for count in belief_counts.values()),
            "coverage_limits": {
                "claims": max_claims,
                "evidence_refs_per_claim": 3,
                "beliefs_per_status": 10,
                "sources": 10,
                "output_projects": 10,
            },
            "sources": sources,
            "output_projects": projects,
            "content_trust": "untrusted_research_data",
            "interpretation": "Evidence links retain their recorded relation; "
            "exact_span identifies the quoted text in a saved source.",
        }
