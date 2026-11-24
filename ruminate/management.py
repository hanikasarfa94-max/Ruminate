"""Workspace organization and lists, independent of proactive review exposure budgets."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.common import (
    Store,
    audit,
    mutate,
    resolve_topic,
    timestamp,
    validate_limit,
    validate_text,
)
from ruminate.persistence.sources import _terms


class ManagementService:
    def __init__(self, config: Config) -> None:
        self.store = Store(config)

    def update_source(
        self, source_id: str, *, title: str | None = None,
        topic_ids: list[str] | None = None, reset_title: bool = False, request_id: str,
    ) -> dict[str, Any]:
        """Edit the source label and classification; evidence versions remain immutable."""
        validate_text(source_id, "source_id", 100)
        if type(reset_title) is not bool or (reset_title and title is not None):
            raise RuminateError("invalid_input", "Choose a new title or reset_title")
        if title is not None:
            validate_text(title, "title", 500)
        if title is None and topic_ids is None and not reset_title:
            raise RuminateError("invalid_input", "Provide a title, topics or reset_title")
        if topic_ids is not None:
            if not isinstance(topic_ids, list) or len(topic_ids) > 50:
                raise RuminateError("invalid_input", "Choose at most 50 topics")
            for topic_id in topic_ids:
                validate_text(topic_id, "topic_id", 100)
        with self.store.write() as connection:
            source = connection.execute(
                "SELECT s.title,v.title AS original_title FROM sources s "
                "JOIN source_versions v ON v.id=s.current_version_id WHERE s.id=?", (source_id,),
            ).fetchone()
            if source is None:
                raise RuminateError("source_not_found", "Source does not exist")
            selected = None
            if topic_ids is not None:
                topics = [resolve_topic(connection, topic_id) for topic_id in topic_ids]
                if any(topic["status"] != "active" for topic in topics):
                    raise RuminateError("invalid_input", "Choose active topics")
                selected = sorted({str(topic["id"]) for topic in topics})
            payload = {"source_id": source_id, "title": title, "topic_ids": selected,
                       "reset_title": reset_title}

            def perform() -> dict[str, Any]:
                if title is not None or reset_title:
                    connection.execute(
                        "UPDATE sources SET title=?,title_is_custom=? WHERE id=?",
                        (source["original_title"] if reset_title else title,
                         0 if reset_title else 1, source_id),
                    )
                if selected is not None:
                    connection.execute("DELETE FROM source_topics WHERE source_id=?", (source_id,))
                    connection.executemany(
                        "INSERT INTO source_topics(source_id,topic_id) VALUES(?,?)",
                        [(source_id, topic_id) for topic_id in selected],
                    )
                audit(connection, "source_updated", source_id, {
                    "title_changed": title is not None or reset_title,
                    "topics_changed": selected is not None,
                })
                return {
                    "source_id": source_id,
                    "display_title": connection.execute(
                        "SELECT title FROM sources WHERE id=?", (source_id,),
                    ).fetchone()[0],
                    "topics": [dict(row) for row in connection.execute(
                        "SELECT t.id AS topic_id,t.slug,t.name FROM topics t JOIN source_topics st "
                        "ON st.topic_id=t.id WHERE st.source_id=? ORDER BY t.name,t.id",
                        (source_id,),
                    )],
                }

            return mutate(connection, "update_source", request_id, payload, perform)

    def list_items(
        self,
        *,
        view: str = "sources",
        topic: str | None = None,
        query: str = "",
        status: str = "all",
        sort: str = "recent",
        limit: int = 25,
        offset: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        validate_limit(limit)
        validate_text(query, "query", 512, empty=True)
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise RuminateError("invalid_input", "Invalid offset")
        if view not in {"sources", "thoughts", "outputs"}:
            raise RuminateError("invalid_input", "Unknown management view")
        if status not in {"all", "pending", "later", "processed"} or sort not in {
            "recent", "oldest", "title"
        }:
            raise RuminateError("invalid_input", "Invalid management filter")
        if view != "thoughts" and status != "all":
            raise RuminateError("invalid_input", "Review status applies to thoughts")
        current = timestamp(now)
        with self.store.read() as connection:
            # Counts, pagination and review badges describe one SQLite snapshot, even
            # when a scheduled reflection commits new suggestions during this read.
            connection.execute("BEGIN")
            topic_id = resolve_topic(connection, topic)["id"] if topic else None
            if view == "sources":
                sql, args = self._sources(topic_id, query.strip())
            elif view == "thoughts":
                sql, args = self._thoughts(topic_id, query.strip(), status, current)
                if status == "all":
                    beliefs, belief_args = self._beliefs(topic_id, query.strip())
                    sql += " UNION ALL " + beliefs
                    args.extend(belief_args)
            else:
                sql, args = self._outputs(topic_id, query.strip())
            total = connection.execute("SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
            order = {"recent": "saved_at DESC,id", "oldest": "saved_at,id",
                     "title": "title COLLATE NOCASE,id"}[sort]
            rows = connection.execute(
                f"SELECT * FROM ({sql}) ORDER BY {order} LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            items = [dict(row) for row in rows]
            if view == "sources":
                for item in items:
                    item["topics"] = [dict(row) for row in connection.execute(
                        "SELECT t.id AS topic_id,t.name FROM topics t JOIN source_topics st "
                        "ON st.topic_id=t.id WHERE st.source_id=? ORDER BY t.name,t.id",
                        (item["id"],),
                    )]
            if view == "thoughts":
                from ruminate.thinking import guide_row

                for item in items:
                    if item["row_kind"] == "claim":
                        item["guide"] = guide_row(connection.execute(
                            "SELECT * FROM reflection_guides WHERE claim_id=? "
                            "ORDER BY created_at DESC,rowid DESC LIMIT 1", (item["id"],),
                        ).fetchone())
                        item["topics"] = [dict(row) for row in connection.execute(
                            "SELECT id AS topic_id,name FROM topics WHERE id=? OR id IN "
                            "(SELECT topic_id FROM claim_topics WHERE claim_id=?) ORDER BY name,id",
                            (item["topic_id"], item["id"]),
                        )]
            pending_sql, pending_args = self._thoughts(topic_id, "", "pending", current)
            pending = connection.execute(
                "SELECT COUNT(*) FROM (" + pending_sql + ")", pending_args
            ).fetchone()[0]
        return {
            "items": items, "total": total, "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < total else None,
            "pending_count": pending, "as_of": current,
        }

    @staticmethod
    def _sources(topic: str | None, query: str) -> tuple[str, list[Any]]:
        sql = (
            "SELECT s.id,s.id AS source_id,v.id AS version_id,s.title,s.canonical_uri,"
            "v.mime_type,v.status,v.retrieved_at AS saved_at "
            "FROM sources s JOIN source_versions v ON v.id=s.current_version_id WHERE 1=1"
        )
        args: list[Any] = []
        if topic:
            sql += " AND EXISTS(SELECT 1 FROM source_topics st WHERE st.source_id=s.id "
            sql += "AND st.topic_id=?)"
            args.append(topic)
        if query:
            sql += " AND (instr(lower(s.title),lower(?))>0 OR instr(lower(v.title),lower(?))>0"
            args.extend([query, query])
            tokens = list(dict.fromkeys(_terms(query, indexing=False)))
            if len(tokens) > 64:
                raise RuminateError("invalid_query", "Query contains too many search terms")
            if tokens:
                sql += (
                    " OR EXISTS(SELECT 1 FROM block_fts JOIN blocks b ON b.id=block_fts.block_id "
                    "WHERE b.version_id=v.id AND block_fts MATCH ?)"
                )
                args.append(" AND ".join('"' + token.replace('"', '""') + '"' for token in tokens))
            sql += ")"
        return sql, args

    @staticmethod
    def _thoughts(
        topic: str | None, query: str, status: str, now: str
    ) -> tuple[str, list[Any]]:
        # One row per thought. Pick the next eligible review, never consume an exposure.
        # A claim may have several review reasons; handling one leaves the others visible.
        review_filter = {
            "all": "1=1",
            "pending": "r.status IN ('queued','postponed') AND r.due_at<=?",
            "later": "r.status IN ('queued','postponed') AND r.due_at>?",
            "processed": "r.status IN ('resolved','dismissed')",
        }[status]
        args: list[Any] = [now] if status in {"pending", "later"} else []
        sql = (
            "SELECT c.id,c.id AS claim_id,c.statement AS title,c.claim_kind,c.origin,"
            "c.created_at AS saved_at,c.topic_id,t.name AS topic_name,"
            "(SELECT COUNT(*) FROM claim_evidence e WHERE e.claim_id=c.id) AS evidence_count,"
            "r.id AS review_id,r.question AS review_question,r.reason AS review_reason,"
            "r.due_at,r.status AS review_status,'claim' AS row_kind "
            "FROM claims c JOIN topics t ON t.id=c.topic_id LEFT JOIN review_candidates r "
            "ON r.id=(SELECT r.id FROM review_candidates r WHERE r.claim_id=c.id "
            "AND (r.belief_id IS NULL OR EXISTS(SELECT 1 FROM user_beliefs ub "
            "WHERE ub.id=r.belief_id AND ub.status='confirmed')) AND " + review_filter +
            " ORDER BY CASE WHEN r.status IN ('queued','postponed') THEN 0 ELSE 1 END,"
            "r.due_at,r.id LIMIT 1) WHERE c.status='candidate'"
        )
        if status != "all":
            sql += " AND r.id IS NOT NULL"
        if topic:
            sql += (" AND (c.topic_id=? OR EXISTS(SELECT 1 FROM claim_topics ct "
                    "WHERE ct.claim_id=c.id AND ct.topic_id=?))")
            args.extend([topic, topic])
        if query:
            sql += " AND instr(lower(c.statement),lower(?))>0"
            args.append(query)
        return sql, args

    @staticmethod
    def _beliefs(topic: str | None, query: str) -> tuple[str, list[Any]]:
        sql = (
            "SELECT b.id,NULL AS claim_id,b.text AS title,'user_belief' AS claim_kind,b.origin,"
            "b.created_at AS saved_at,b.topic_id,t.name AS topic_name,NULL AS evidence_count,"
            "NULL AS review_id,NULL AS review_question,NULL AS review_reason,NULL AS due_at,"
            "NULL AS review_status,'belief' AS row_kind "
            "FROM user_beliefs b JOIN topics t ON t.id=b.topic_id "
            "WHERE b.status='confirmed' AND b.origin='human' AND b.user_confirmed_at IS NOT NULL"
        )
        args: list[Any] = []
        if topic:
            sql += " AND b.topic_id=?"
            args.append(topic)
        if query:
            sql += " AND instr(lower(b.text),lower(?))>0"
            args.append(query)
        return sql, args

    @staticmethod
    def _outputs(topic: str | None, query: str) -> tuple[str, list[Any]]:
        sql = (
            "SELECT p.id,p.id AS project_id,p.title,p.kind,p.goal,p.topic_id,t.name AS topic_name,"
            "COALESCE((SELECT MAX(r.created_at) FROM output_revisions r WHERE r.project_id=p.id),"
            "p.created_at) AS saved_at,"
            "(SELECT MAX(r.version) FROM output_revisions r WHERE r.project_id=p.id) AS version "
            "FROM output_projects p JOIN topics t ON t.id=p.topic_id WHERE p.status='active'"
        )
        args: list[Any] = []
        if topic:
            sql += " AND p.topic_id=?"
            args.append(topic)
        if query:
            sql += (
                " AND (instr(lower(p.title),lower(?))>0 OR instr(lower(p.goal),lower(?))>0 "
                "OR EXISTS(SELECT 1 FROM output_revisions r WHERE r.project_id=p.id "
                "AND r.version=(SELECT MAX(rv.version) FROM output_revisions rv "
                "WHERE rv.project_id=p.id) AND instr(lower(r.markdown),lower(?))>0))"
            )
            args.extend([query, query, query])
        return sql, args
