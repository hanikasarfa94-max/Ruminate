"""Deterministic, evidence-backed review candidates and explicit exposure tracking."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.common import (
    Store,
    audit,
    json_dump,
    mutate,
    new_id,
    resolve_topic,
    timestamp,
    utc_now,
    validate_limit,
    validate_text,
)

DEFAULT_WEIGHTS = {
    "relevance": 0.25,
    "evidence_novelty": 0.25,
    "belief_conflict": 0.20,
    "task_urgency": 0.15,
    "freshness_or_due": 0.10,
    "diversity": 0.05,
}
_ACTIONS = {
    "useful",
    "not_useful",
    "already_known",
    "needs_more_evidence",
    "postpone",
    "dismiss",
    "discuss",
}
_MAX_CLAIMS = 500
_MAX_EVIDENCE = 20


def _source_ids(evidence: list[dict[str, Any]]) -> set[str]:
    return {item["source_id"] for item in evidence}


def _card(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    evidence = json.loads(row["evidence_json"])
    reason = row["reason"]
    return {
        "id": row["id"],
        "topic_id": row["topic_id"],
        "claim_id": row["claim_id"],
        "old_belief_id": row["belief_id"],
        "title": row["question"],
        "question": row["question"],
        "reason": reason,
        "why_now": [reason],
        "priority": row["priority"],
        "score_breakdown": json.loads(row["score_breakdown_json"]),
        "evidence": evidence,
        "evidence_scope": "bounded snapshot of selected links; read claims for all evidence",
        "supports": [item for item in evidence if item["relation"] == "supports"],
        "challenges": [item for item in evidence if item["relation"] == "challenges"],
        "due_at": row["due_at"],
        "status": row["status"],
        "uncertainty": "requires_user_review",
        "content_trust": "untrusted_source_and_user_supplied_links",
        "suggested_action": "Compare the preserved excerpts and record your assessment",
        "actions": sorted(_ACTIONS),
    }


class ReviewService:
    """No semantic contradiction classifier, notifications or model credentials.

    Candidate links are explicit assertions supplied through the knowledge service.
    Scoring is an explainable heuristic, not a probability of usefulness or truth.
    List is a read-only preview; present is the only action that consumes exposure quota.
    """

    def __init__(self, config: Config, *, weights: Mapping[str, float] | None = None) -> None:
        self.config = config
        self.store = Store(config)
        self.timezone = ZoneInfo(config.review_timezone)
        self.weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
        if (
            self.weights.keys() != DEFAULT_WEIGHTS.keys()
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
                for value in self.weights.values()
            )
            or not math.isclose(sum(self.weights.values()), 1.0)
        ):
            raise RuminateError("invalid_review_weights", "Review weights must sum to one")

    @staticmethod
    def _evidence(connection: sqlite3.Connection, claim_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "WITH ranked AS (SELECT e.*,ROW_NUMBER() OVER (PARTITION BY relation "
            "ORDER BY created_at,id) AS position FROM claim_evidence e WHERE claim_id=?) "
            "SELECT e.*,v.source_id,b.page,b.section,b.locator_json FROM ranked e "
            "JOIN blocks b ON b.id=e.block_id AND b.version_id=e.version_id "
            "JOIN source_versions v ON v.id=e.version_id "
            "ORDER BY e.position,CASE e.relation WHEN 'supports' THEN 0 "
            "WHEN 'challenges' THEN 1 WHEN 'mentions' THEN 2 ELSE 3 END,e.id LIMIT ?",
            (claim_id, _MAX_EVIDENCE),
        ).fetchall()
        result = []
        for row in rows:
            # Returned quotes remain exact slices; full evidence spans stay on their links.
            quote = row["quote"][:500]
            result.append(
                {
                    "evidence_id": row["id"],
                    "claim_id": claim_id,
                    "source_id": row["source_id"],
                    "version_id": row["version_id"],
                    "block_id": row["block_id"],
                    "page": row["page"],
                    "section": row["section"],
                    "locator": json.loads(row["locator_json"]),
                    "span": {"start": row["start_char"], "end": row["start_char"] + len(quote)},
                    "evidence_span": {"start": row["start_char"], "end": row["end_char"]},
                    "quote": quote,
                    "quote_truncated": len(quote) != len(row["quote"]),
                    "relation": row["relation"],
                    "claim_relation": row["relation"],
                    "verifier_status": row["verifier_status"],
                    "created_at": row["created_at"],
                }
            )
        return result

    def _score(
        self,
        connection: sqlite3.Connection,
        topic_id: str,
        evidence: list[dict[str, Any]],
        *,
        conflict: bool,
        active_output: bool,
        now: datetime,
    ) -> tuple[float, dict[str, Any]]:
        recent = connection.execute(
            "SELECT c.evidence_json FROM review_events e "
            "JOIN review_candidates c ON c.id=e.candidate_id "
            "WHERE e.action='shown' AND c.topic_id=? AND e.created_at>=? AND e.created_at<=? "
            "ORDER BY e.created_at DESC LIMIT 100",
            (topic_id, timestamp(now - timedelta(days=7)), timestamp(now)),
        ).fetchall()
        seen_versions: set[str] = set()
        seen_sources: set[str] = set()
        for row in recent:
            for item in json.loads(row["evidence_json"]):
                seen_versions.add(item["version_id"])
                seen_sources.add(item["source_id"])
        versions = {item["version_id"] for item in evidence}
        sources = _source_ids(evidence)
        novelty = len(versions - seen_versions) / len(versions) if versions else 0.0
        diversity = len(sources - seen_sources) / len(sources) if sources else 0.5
        dates = [datetime.fromisoformat(item["created_at"]) for item in evidence]
        age_days = max(0, (now - max(dates)).days) if dates else 0
        features = {
            "relevance": 1.0,
            "evidence_novelty": novelty,
            "belief_conflict": 1.0 if conflict else 0.0,
            "task_urgency": 1.0 if active_output else 0.0,
            "freshness_or_due": 1.0 / (1 + age_days),
            "diversity": diversity,
        }
        fatigue = min(0.30, len(recent) * 0.05)
        weighted = {name: round(value * self.weights[name], 6) for name, value in features.items()}
        priority = round(max(0.0, min(1.0, sum(weighted.values()) - fatigue)), 6)
        return priority, {
            "features": features,
            "weights": self.weights,
            "weighted": weighted,
            "fatigue_penalty": fatigue,
            "priority": priority,
            "interpretation": (
                "heuristic scheduling priority based on feature weights and feedback penalties"
            ),
        }

    def _proposals(
        self,
        connection: sqlite3.Connection,
        topic_id: str | None,
        now: datetime,
    ) -> list[dict[str, Any]]:
        topic = resolve_topic(connection, topic_id)["id"] if topic_id is not None else None
        sql = "SELECT * FROM claims WHERE status='candidate'"
        parameters: list[str | int] = []
        if topic is not None:
            sql += (" AND (topic_id=? OR id IN "
                    "(SELECT claim_id FROM claim_topics WHERE topic_id=?))")
            parameters.extend([topic, topic])
        sql += " ORDER BY created_at,id LIMIT ?"
        parameters.append(_MAX_CLAIMS + 1)
        claims = connection.execute(sql, parameters).fetchall()
        if len(claims) > _MAX_CLAIMS:
            raise RuminateError(
                "review_scope_too_large",
                "Review generation supports up to 500 claims; select a topic",
            )
        proposals = []
        for claim in claims:
            evidence = self._evidence(connection, claim["id"])
            relations = {item["relation"] for item in evidence}
            active_output = (
                connection.execute(
                    "SELECT 1 FROM output_projects WHERE topic_id=? AND status='active' LIMIT 1",
                    (claim["topic_id"],),
                ).fetchone()
                is not None
            )
            reasons: list[tuple[str, str | None, str, list[dict[str, Any]]]] = []
            challenges = connection.execute(
                "SELECT b.* FROM belief_relations r JOIN user_beliefs b ON b.id=r.belief_id "
                "WHERE r.claim_id=? AND r.relation='challenges' AND b.status='confirmed' "
                "AND b.user_confirmed_at IS NOT NULL AND b.origin='human' AND b.topic_id=? "
                "ORDER BY b.id LIMIT 20",
                (claim["id"], claim["topic_id"]),
            ).fetchall()
            # A mere mention or a challenge to the claim is not evidence for that claim.
            if "supports" in relations:
                for belief in challenges:
                    linked = [
                        {**item, "relation": "challenges", "belief_relation": "challenges"}
                        for item in evidence
                        if item["claim_relation"] == "supports"
                    ]
                    old_claims = connection.execute(
                        "SELECT c.id FROM belief_relations r JOIN claims c ON c.id=r.claim_id "
                        "WHERE r.belief_id=? AND r.relation='supports' AND c.status='candidate' "
                        "ORDER BY c.id LIMIT 20",
                        (belief["id"],),
                    ).fetchall()
                    old_evidence: list[dict[str, Any]] = []
                    for old_claim in old_claims:
                        old_evidence.extend(
                            {**item, "relation": "supports", "belief_relation": "supports"}
                            for item in self._evidence(connection, old_claim["id"])
                            if item["claim_relation"] == "supports"
                        )
                    # Preserve space for both sides when either has many links.
                    selected = (
                        linked[: _MAX_EVIDENCE // 2] + old_evidence[: _MAX_EVIDENCE // 2]
                        if old_evidence
                        else linked[:_MAX_EVIDENCE]
                    )
                    reasons.append(
                        (
                            "possible_conflict",
                            belief["id"],
                            f"已确认观点是否需要复核：{belief['text'][:300]}？",
                            selected,
                        )
                    )
            if {"supports", "challenges"} <= relations:
                reasons.append(
                    (
                        "contested_evidence",
                        None,
                        f"支持与质疑材料能否解释同一主张：{claim['statement'][:300]}？",
                        evidence,
                    )
                )
            elif not reasons:
                if "supports" not in relations and (
                    active_output or claim["claim_kind"] == "question"
                ):
                    reasons.append(
                        (
                            "evidence_gap",
                            None,
                            f"还需要哪些证据回答或验证：{claim['statement'][:300]}？",
                            evidence,
                        )
                    )
                elif active_output and evidence:
                    reasons.append(
                        (
                            "active_output",
                            None,
                            f"这些材料如何进入当前输出项目：{claim['statement'][:300]}？",
                            evidence,
                        )
                    )
            for reason, belief_id, question, selected_evidence in reasons:
                priority, breakdown = self._score(
                    connection,
                    claim["topic_id"],
                    selected_evidence,
                    conflict=reason == "possible_conflict",
                    active_output=active_output,
                    now=now,
                )
                fingerprint = hashlib.sha256(
                    json_dump([reason, claim["id"], belief_id]).encode("utf-8")
                ).hexdigest()
                proposals.append(
                    {
                        "id": "rev_" + fingerprint[:32],
                        "fingerprint": fingerprint,
                        "topic_id": claim["topic_id"],
                        "claim_id": claim["id"],
                        "belief_id": belief_id,
                        "question": question,
                        "reason": reason,
                        "priority": priority,
                        "score_breakdown_json": json_dump(breakdown),
                        "evidence_json": json_dump(selected_evidence),
                        "due_at": timestamp(now),
                        "status": "queued",
                        "created_at": timestamp(now),
                        "updated_at": timestamp(now),
                    }
                )
        return sorted(proposals, key=lambda item: (-item["priority"], item["id"]))

    def tick(
        self,
        topic_id: str | None = None,
        *,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if type(dry_run) is not bool:
            raise RuminateError("invalid_input", "dry_run must be a boolean")
        instant = utc_now(now)
        manager = self.store.read() if dry_run else self.store.write()
        with manager as connection:
            proposals = self._proposals(connection, topic_id, instant)
            created = 0
            existing = 0
            cards = []
            for proposal in proposals:
                old = connection.execute(
                    "SELECT * FROM review_candidates WHERE fingerprint=?",
                    (proposal["fingerprint"],),
                ).fetchone()
                if old is not None:
                    existing += 1
                    # Existing candidates retain their evidence snapshot and feedback lifecycle.
                    continue
                cards.append(_card(proposal))
                created += 1
                if dry_run:
                    continue
                connection.execute(
                    "INSERT INTO review_candidates(id,fingerprint,topic_id,claim_id,belief_id,"
                    "question,reason,priority,score_breakdown_json,evidence_json,due_at,status,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(
                        proposal[name]
                        for name in (
                            "id",
                            "fingerprint",
                            "topic_id",
                            "claim_id",
                            "belief_id",
                            "question",
                            "reason",
                            "priority",
                            "score_breakdown_json",
                            "evidence_json",
                            "due_at",
                            "status",
                            "created_at",
                            "updated_at",
                        )
                    ),
                )
                audit(
                    connection,
                    "review_candidate_created",
                    proposal["id"],
                    {
                        "reason": proposal["reason"],
                        "claim_id": proposal["claim_id"],
                        "belief_id": proposal["belief_id"],
                    },
                )
            return {
                "dry_run": dry_run,
                "created": 0 if dry_run else created,
                "would_create": created if dry_run else 0,
                "existing": existing,
                "candidates": cards[:20],
                "candidate_count": len(cards),
                "candidates_truncated": len(cards) > 20,
                "notifications_sent": 0,
            }

    def _daily_state(
        self,
        connection: sqlite3.Connection,
        now: datetime,
    ) -> tuple[int, dict[str, int], set[str], set[str]]:
        local = now.astimezone(self.timezone)
        local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = local_start.astimezone(UTC)
        end = (local_start + timedelta(days=1)).astimezone(UTC)
        rows = connection.execute(
            "SELECT c.id,c.topic_id,c.evidence_json FROM review_events e "
            "JOIN review_candidates c ON c.id=e.candidate_id "
            "WHERE e.action='shown' AND e.created_at>=? AND e.created_at<?",
            (timestamp(start), timestamp(end)),
        ).fetchall()
        topics: dict[str, int] = {}
        sources: set[str] = set()
        ids = set()
        for row in rows:
            topics[row["topic_id"]] = topics.get(row["topic_id"], 0) + 1
            sources.update(_source_ids(json.loads(row["evidence_json"])))
            ids.add(row["id"])
        return len(rows), topics, sources, ids

    def _select(
        self,
        connection: sqlite3.Connection,
        topic_id: str | None,
        limit: int,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], int]:
        topic = resolve_topic(connection, topic_id)["id"] if topic_id is not None else None
        count, topics, seen_sources, shown_ids = self._daily_state(connection, now)
        remaining = max(0, 3 - count)
        if remaining == 0:
            return [], 0
        sql = (
            "SELECT c.* FROM review_candidates c JOIN claims k ON k.id=c.claim_id "
            "WHERE c.status IN ('queued','postponed') AND c.due_at<=? AND k.status='candidate'"
        )
        parameters: list[str | int] = [timestamp(now)]
        if topic is not None:
            sql += (" AND (c.topic_id=? OR c.claim_id IN "
                    "(SELECT claim_id FROM claim_topics WHERE topic_id=?))")
            parameters.extend([topic, topic])
        sql += " ORDER BY c.priority DESC,c.created_at,c.id LIMIT 1000"
        rows = connection.execute(sql, parameters).fetchall()
        selected = []
        for row in rows:
            if row["belief_id"] is not None:
                belief = connection.execute(
                    "SELECT status FROM user_beliefs WHERE id=?", (row["belief_id"],)
                ).fetchone()
                if belief is None or belief["status"] != "confirmed":
                    continue
            if row["id"] in shown_ids or topics.get(row["topic_id"], 0) >= 2:
                continue
            source_ids = _source_ids(json.loads(row["evidence_json"]))
            if source_ids & seen_sources:
                continue
            selected.append(_card(row))
            topics[row["topic_id"]] = topics.get(row["topic_id"], 0) + 1
            seen_sources.update(source_ids)
            if len(selected) >= min(limit, remaining):
                break
        return selected, remaining

    def list_queue(
        self,
        topic_id: str | None = None,
        *,
        limit: int = 3,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        validate_limit(limit, 3)
        with self.store.read() as connection:
            cards, _ = self._select(connection, topic_id, limit, utc_now(now))
            return cards

    def present(
        self,
        topic_id: str | None = None,
        *,
        limit: int = 3,
        request_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        validate_limit(limit, 3)
        instant = utc_now(now)
        with self.store.write() as connection:
            topic = resolve_topic(connection, topic_id)["id"] if topic_id is not None else None

            def perform() -> dict[str, Any]:
                cards, remaining = self._select(connection, topic, limit, instant)
                for card in cards:
                    connection.execute(
                        "INSERT INTO review_events(id,candidate_id,action,created_at) "
                        "VALUES(?,?,'shown',?)",
                        (new_id("revevt"), card["id"], timestamp(instant)),
                    )
                    connection.execute(
                        "UPDATE review_candidates SET due_at=?,updated_at=? WHERE id=?",
                        (timestamp(instant + timedelta(days=1)), timestamp(instant), card["id"]),
                    )
                    audit(
                        connection,
                        "review_shown",
                        card["id"],
                        {"timezone": self.config.review_timezone},
                    )
                return {
                    "cards": cards,
                    "shown": len(cards),
                    "daily_remaining": remaining - len(cards),
                    "timezone": self.config.review_timezone,
                }

            return mutate(
                connection,
                "review.present",
                request_id,
                {"topic_id": topic, "limit": limit},
                perform,
            )

    def feedback(
        self,
        review_id: str,
        action: str,
        *,
        note: str = "",
        postpone_days: int = 7,
        request_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        validate_text(review_id, "review_id", 100)
        validate_text(note, "note", 2000, empty=True)
        if not isinstance(action, str) or action not in _ACTIONS:
            raise RuminateError("invalid_review_action", "Unknown review feedback action")
        if type(postpone_days) is not int or not 1 <= postpone_days <= 365:
            raise RuminateError("invalid_input", "postpone_days must be between 1 and 365")
        instant = utc_now(now)
        with self.store.write() as connection:

            def perform() -> dict[str, Any]:
                row = connection.execute(
                    "SELECT * FROM review_candidates WHERE id=?",
                    (review_id,),
                ).fetchone()
                if row is None:
                    raise RuminateError("review_not_found", "Review candidate does not exist")
                if row["status"] in {"dismissed", "resolved"}:
                    raise RuminateError("review_closed", "This review has already been closed")
                if action == "useful":
                    status = "resolved"
                    due = row["due_at"]
                elif action in {"not_useful", "already_known", "dismiss"}:
                    status = "dismissed"
                    due = row["due_at"]
                else:
                    status = "postponed"
                    days = 1 if action == "discuss" else postpone_days
                    due = timestamp(instant + timedelta(days=days))
                connection.execute(
                    "UPDATE review_candidates SET status=?,due_at=?,updated_at=? WHERE id=?",
                    (status, due, timestamp(instant), review_id),
                )
                connection.execute(
                    "INSERT INTO review_events(id,candidate_id,action,feedback,note,created_at) "
                    "VALUES(?,?,'feedback',?,?,?)",
                    (new_id("revevt"), review_id, action, note, timestamp(instant)),
                )
                audit(
                    connection, "review_feedback", review_id, {"action": action, "status": status}
                )
                return {"id": review_id, "action": action, "status": status, "due_at": due}

            return mutate(
                connection,
                "review.feedback",
                request_id,
                {
                    "review_id": review_id,
                    "action": action,
                    "note": note,
                    "postpone_days": postpone_days,
                },
                perform,
            )
