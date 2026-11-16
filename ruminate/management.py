"""Read-only management lists, independent of proactive review exposure budgets."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.common import (
    Store,
    resolve_topic,
    timestamp,
    validate_limit,
    validate_text,
)
from ruminate.persistence.sources import _terms


class ManagementService:
    def __init__(self, config: Config) -> None:
        self.store = Store(config)

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
            "SELECT s.id,s.id AS source_id,v.id AS version_id,v.title,s.canonical_uri,"
            "v.mime_type,v.status,v.retrieved_at AS saved_at "
            "FROM sources s JOIN source_versions v ON v.id=s.current_version_id WHERE 1=1"
        )
        args: list[Any] = []
        if topic:
            sql += " AND EXISTS(SELECT 1 FROM source_topics st WHERE st.source_id=s.id "
            sql += "AND st.topic_id=?)"
            args.append(topic)
        if query:
            sql += " AND (instr(lower(v.title),lower(?))>0"
            args.append(query)
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
            sql += " AND c.topic_id=?"
            args.append(topic)
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
