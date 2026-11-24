"""Traceable, reversible topic suggestions authored by the current AI host."""

from __future__ import annotations

from typing import Any

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
)


class OrganizationService:
    def __init__(self, config: Config) -> None:
        self.store = Store(config)

    def suggest(
        self,
        *,
        target_kind: str,
        target_id: str,
        topic_id: str,
        rationale: str,
        version_id: str | None = None,
        auto_apply: bool = False,
        request_id: str,
    ) -> dict[str, Any]:
        if (not isinstance(target_kind, str) or target_kind not in {"source", "claim"}
                or type(auto_apply) is not bool):
            raise RuminateError("invalid_input", "Choose a source or thought")
        validate_text(target_id, "target_id", 100)
        validate_text(topic_id, "topic_id", 100)
        validate_text(rationale, "rationale", 600)
        if version_id is not None:
            validate_text(version_id, "version_id", 100)
        payload = dict(
            target_kind=target_kind,
            target_id=target_id,
            topic_id=topic_id,
            rationale=rationale,
            version_id=version_id,
            auto_apply=auto_apply,
        )
        with self.store.write() as connection:

            def perform() -> dict[str, Any]:
                topic = resolve_topic(connection, topic_id)
                if topic["status"] != "active":
                    raise RuminateError("invalid_input", "Choose an active topic")
                table = "sources" if target_kind == "source" else "claims"
                target = connection.execute(
                    f"SELECT * FROM {table} WHERE id=?", (target_id,)
                ).fetchone()
                if target is None:
                    raise RuminateError("not_found", "Classification target does not exist")
                if target_kind == "source" and (
                    not version_id or target["current_version_id"] != version_id
                ):
                    raise RuminateError(
                        "stale_source", "Read the current source version before classifying"
                    )
                if target_kind == "claim" and version_id is not None:
                    raise RuminateError("invalid_input", "Thoughts do not take a source version")
                prior = connection.execute(
                    "SELECT * FROM topic_suggestions WHERE target_kind=? AND target_id=? "
                    "AND topic_id=? AND version_id IS ? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (target_kind, target_id, topic["id"], version_id),
                ).fetchone()
                # Respect prior acceptance or rejection; a fresh request ID cannot undo it.
                if prior is not None:
                    return dict(prior)
                memberships = "source_topics" if target_kind == "source" else "claim_topics"
                field = "source_id" if target_kind == "source" else "claim_id"
                exists = connection.execute(
                    f"SELECT 1 FROM {memberships} WHERE {field}=? AND topic_id=?",
                    (target_id, topic["id"]),
                ).fetchone()
                if exists or (target_kind == "claim" and target["topic_id"] == topic["id"]):
                    return {
                        "status": "already_classified",
                        "target_id": target_id,
                        "topic_id": topic["id"],
                    }
                suggestion_id = new_id("tag")
                now = timestamp()
                connection.execute(
                    "INSERT INTO topic_suggestions VALUES(?,?,?,?,?,?,'host_model',?,?,?)",
                    (
                        suggestion_id,
                        target_kind,
                        target_id,
                        version_id,
                        topic["id"],
                        rationale,
                        "applied" if auto_apply else "pending",
                        now,
                        now,
                    ),
                )
                if auto_apply:
                    connection.execute(
                        f"INSERT INTO {memberships}({field},topic_id,suggestion_id) VALUES(?,?,?)",
                        (target_id, topic["id"], suggestion_id),
                    )
                audit(connection, "topic_suggested", suggestion_id, {"auto_applied": auto_apply})
                return dict(
                    connection.execute(
                        "SELECT * FROM topic_suggestions WHERE id=?",
                        (suggestion_id,),
                    ).fetchone()
                )

            return mutate(connection, "suggest_topic", request_id, payload, perform)

    def decide(self, suggestion_id: str, action: str, *, request_id: str) -> dict[str, Any]:
        validate_text(suggestion_id, "suggestion_id", 100)
        if action not in {"apply", "dismiss", "revert"}:
            raise RuminateError("invalid_input", "Choose apply, dismiss or revert")
        with self.store.write() as connection:

            def perform() -> dict[str, Any]:
                row = connection.execute(
                    "SELECT * FROM topic_suggestions WHERE id=?",
                    (suggestion_id,),
                ).fetchone()
                if row is None:
                    raise RuminateError("not_found", "Suggestion does not exist")
                required = "applied" if action == "revert" else "pending"
                if row["status"] != required:
                    raise RuminateError("invalid_state", "This suggestion has already been handled")
                source = row["target_kind"] == "source"
                table, field = (
                    ("source_topics", "source_id") if source else ("claim_topics", "claim_id")
                )
                if action == "apply":
                    if resolve_topic(connection, row["topic_id"])["status"] != "active":
                        raise RuminateError("invalid_input", "Choose an active topic")
                    if source:
                        current = connection.execute(
                            "SELECT current_version_id FROM sources WHERE id=?",
                            (row["target_id"],),
                        ).fetchone()
                        if current is None or current[0] != row["version_id"]:
                            raise RuminateError("stale_source", "The source has a newer version")
                    connection.execute(
                        f"INSERT OR IGNORE INTO {table}({field},topic_id,suggestion_id) "
                        "VALUES(?,?,?)",
                        (row["target_id"], row["topic_id"], suggestion_id),
                    )
                if action == "revert":
                    # Manual source edits clear this ownership token; never remove those choices.
                    connection.execute(
                        f"DELETE FROM {table} WHERE {field}=? AND topic_id=? AND suggestion_id=?",
                        (row["target_id"], row["topic_id"], suggestion_id),
                    )
                status = {"apply": "applied", "dismiss": "dismissed", "revert": "reverted"}[action]
                connection.execute(
                    "UPDATE topic_suggestions SET status=?,updated_at=? WHERE id=?",
                    (status, timestamp(), suggestion_id),
                )
                audit(connection, "topic_suggestion_" + action, suggestion_id)
                return {"id": suggestion_id, "status": status}

            return mutate(
                connection,
                "decide_topic_suggestion",
                request_id,
                {"id": suggestion_id, "action": action},
                perform,
            )

    def list_suggestions(
        self,
        *,
        status: str = "active",
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        validate_limit(limit)
        if status not in {"active", "all", "pending", "applied", "dismissed", "reverted"}:
            raise RuminateError("invalid_input", "Unknown suggestion filter")
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise RuminateError("invalid_input", "Invalid offset")
        where, args = "1=1", []
        if status == "active":
            where = "x.status IN ('pending','applied')"
        elif status != "all":
            where, args = "x.status=?", [status]
        with self.store.read() as connection:
            connection.execute("BEGIN")
            total = connection.execute(
                "SELECT COUNT(*) FROM topic_suggestions x WHERE " + where,
                args,
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT x.*,t.name AS topic_name,COALESCE(s.title,c.statement) AS title "
                "FROM topic_suggestions x JOIN topics t ON t.id=x.topic_id "
                "LEFT JOIN sources s ON x.target_kind='source' AND s.id=x.target_id "
                "LEFT JOIN claims c ON x.target_kind='claim' AND c.id=x.target_id "
                "WHERE " + where + " ORDER BY x.created_at DESC,x.rowid DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            )
            return {
                "items": [dict(row) for row in rows],
                "total": total,
                "next_offset": offset + limit if offset + limit < total else None,
            }
