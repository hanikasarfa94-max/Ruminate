"""Host-authored reflection guides and a local record of the user's thinking."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.knowledge import _claim_row
from ruminate.persistence.common import (
    Store,
    audit,
    json_dump,
    mutate,
    new_id,
    timestamp,
    utc_now,
    validate_text,
)

LENSES = {"connection", "counterexample", "boundary", "tradeoff", "revisit"}


def guide_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("prompts", "evidence_ids", "links"):
        result[key] = json.loads(result.pop(key + "_json"))
    return result


class ThinkingService:
    def __init__(self, config: Config) -> None:
        self.store = Store(config)

    def save_guide(
        self,
        *,
        claim_id: str,
        lens: str,
        spark: str,
        prompts: list[str],
        evidence_ids: list[str],
        links: list[dict[str, str]],
        request_id: str,
    ) -> dict[str, Any]:
        """Append a model interpretation, with real evidence and explicit related thoughts."""
        if not isinstance(lens, str) or lens not in LENSES:
            raise RuminateError("invalid_input", "Choose a supported thinking lens")
        validate_text(spark, "spark", 600)
        if not isinstance(prompts, list) or not 1 <= len(prompts) <= 3:
            raise RuminateError("invalid_input", "Provide one to three questions")
        for prompt in prompts:
            validate_text(prompt, "prompt", 240)
        if (
            not isinstance(evidence_ids, list)
            or not 1 <= len(evidence_ids) <= 6
            or not all(isinstance(i, str) for i in evidence_ids)
        ):
            raise RuminateError("invalid_input", "Provide one to six evidence IDs")
        if not isinstance(links, list) or len(links) > 3:
            raise RuminateError("invalid_input", "At most three thought links")
        validate_text(claim_id, "claim_id", 100)
        seen = {claim_id}
        for link in links:
            if not isinstance(link, dict) or set(link) != {"claim_id", "reason"}:
                raise RuminateError("invalid_input", "Each link needs claim_id and reason")
            validate_text(link["claim_id"], "claim_id", 100)
            validate_text(link["reason"], "reason", 300)
            if link["claim_id"] in seen:
                raise RuminateError("invalid_input", "Thought links must be distinct")
            seen.add(link["claim_id"])
        payload = dict(
            claim_id=claim_id,
            lens=lens,
            spark=spark,
            prompts=prompts,
            evidence_ids=evidence_ids,
            links=links,
        )
        with self.store.write() as connection:

            def perform() -> dict[str, Any]:
                _claim_row(connection, claim_id)
                for evidence_id in evidence_ids:
                    if not connection.execute(
                        "SELECT 1 FROM claim_evidence WHERE id=? AND claim_id=?",
                        (evidence_id, claim_id),
                    ).fetchone():
                        raise RuminateError(
                            "invalid_evidence", "Use evidence linked to this thought"
                        )
                for link in links:
                    _claim_row(connection, link["claim_id"])
                guide_id = new_id("guide")
                connection.execute(
                    "INSERT INTO reflection_guides VALUES(?,?,?,?,?,?,?,'host_model',?)",
                    (
                        guide_id,
                        claim_id,
                        lens,
                        spark,
                        json_dump(prompts),
                        json_dump(evidence_ids),
                        json_dump(links),
                        timestamp(),
                    ),
                )
                audit(connection, "reflection_guide_saved", guide_id, {"claim_id": claim_id})
                return {"guide_id": guide_id, "claim_id": claim_id, "origin": "host_model"}

            return mutate(connection, "save_reflection_guide", request_id, payload, perform)

    def save_note(
        self,
        *,
        claim_id: str,
        text: str,
        origin: str = "host_model",
        request_id: str,
    ) -> dict[str, Any]:
        validate_text(text, "text", 4000)
        if not isinstance(origin, str) or origin not in {"human", "host_model"}:
            raise RuminateError("invalid_input", "Invalid note origin")
        with self.store.write() as connection:

            def perform() -> dict[str, Any]:
                _claim_row(connection, claim_id)
                note = dict(
                    id=new_id("note"),
                    claim_id=claim_id,
                    text=text,
                    origin=origin,
                    created_at=timestamp(),
                )
                connection.execute(
                    "INSERT INTO reflection_notes VALUES(:id,:claim_id,:text,:origin,:created_at)",
                    note,
                )
                audit(connection, "reflection_note_saved", note["id"], {"origin": origin})
                return note

            return mutate(
                connection,
                "save_reflection_note",
                request_id,
                dict(claim_id=claim_id, text=text, origin=origin),
                perform,
            )

    def context(self, claim_id: str, *, offset: int = 0) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise RuminateError("invalid_input", "Invalid offset")
        with self.store.read() as connection:
            connection.execute("BEGIN")
            _claim_row(connection, claim_id)
            guide = guide_row(
                connection.execute(
                    "SELECT * FROM reflection_guides WHERE claim_id=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (claim_id,),
                ).fetchone()
            )
            notes = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM reflection_notes WHERE claim_id=? "
                    "ORDER BY created_at DESC,id LIMIT 20 OFFSET ?",
                    (claim_id, offset),
                )
            ]
            total = connection.execute(
                "SELECT COUNT(*) FROM reflection_notes WHERE claim_id=?",
                (claim_id,),
            ).fetchone()[0]
            related = []
            for link in (guide or {}).get("links", []):
                row = _claim_row(connection, link["claim_id"])
                related.append(
                    {
                        **dict(row),
                        "claim_id": row["id"],
                        "reason": link["reason"],
                        "direction": "outgoing",
                        "link_origin": "host_model",
                    }
                )
            # Only the latest guide per thought contributes backlinks.
            backlinks = connection.execute(
                "SELECT c.*,json_extract(j.value,'$.reason') AS reason FROM reflection_guides g "
                "JOIN claims c ON c.id=g.claim_id JOIN json_each(g.links_json) j "
                "WHERE json_extract(j.value,'$.claim_id')=? AND g.id=(SELECT x.id "
                "FROM reflection_guides x WHERE x.claim_id=g.claim_id "
                "ORDER BY x.created_at DESC,x.rowid DESC LIMIT 1) "
                "ORDER BY g.created_at DESC LIMIT 4",
                (claim_id,),
            ).fetchall()
            known = {link["claim_id"] for link in related}
            for row in backlinks[:3]:
                if row["id"] not in known:
                    related.append(
                        {
                            **dict(row),
                            "claim_id": row["id"],
                            "direction": "incoming",
                            "link_origin": "host_model",
                        }
                    )
            return {
                "claim_id": claim_id,
                "guide": guide,
                "notes": notes,
                "note_count": total,
                "next_offset": offset + 20 if offset + 20 < total else None,
                "related": related,
                "backlinks_truncated": len(backlinks) > 3,
            }

    def activity(
        self,
        *,
        timezone: str = "Asia/Shanghai",
        day: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        validate_text(timezone, "timezone", 80)
        try:
            zone = ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise RuminateError("invalid_input", "Unknown timezone") from exc
        today = utc_now(now).astimezone(zone).date()
        start = today - timedelta(days=today.weekday() + 77)
        end = start + timedelta(days=84)
        if day is not None:
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise RuminateError("invalid_input", "Use YYYY-MM-DD")
            try:
                selected = date.fromisoformat(day)
            except ValueError as exc:
                raise RuminateError("invalid_input", "Invalid date") from exc
            if not start <= selected <= today:
                raise RuminateError("invalid_input", "Choose a day in the displayed twelve weeks")
        lower = timestamp(datetime.combine(start, time.min, zone))
        upper = timestamp(datetime.combine(min(end, today + timedelta(days=1)), time.min, zone))
        # No exposure events, background model writes or imports count as human thinking.
        sql = (
            "SELECT id, id AS claim_id, statement AS text, created_at, 'thought' AS kind "
            "FROM claims WHERE origin='human' UNION ALL "
            "SELECT id,claim_id,text,created_at,'note' FROM reflection_notes WHERE origin='human' "
            "UNION ALL SELECT e.id,r.claim_id,e.feedback,e.created_at,'review' "
            "FROM review_events e JOIN review_candidates r ON r.id=e.candidate_id "
            "WHERE e.action='feedback'"
        )
        days: dict[str, dict[str, Any]] = {}
        for index in range(84):
            current = start + timedelta(days=index)
            days[current.isoformat()] = {
                "date": current.isoformat(),
                "count": 0,
                "future": current > today,
            }
        entries: list[dict[str, Any]] = []
        seen_reviews: set[tuple[str, str]] = set()
        with self.store.read() as connection:
            for row in connection.execute(
                "SELECT * FROM (" + sql + ") WHERE created_at>=? AND created_at<? "
                "ORDER BY created_at DESC,id",
                (lower, upper),
            ):
                item = dict(row)
                date_key = (
                    datetime.fromisoformat(item["created_at"]).astimezone(zone).date().isoformat()
                )
                if item["kind"] == "review":
                    key = (date_key, item["claim_id"])
                    if key in seen_reviews:
                        continue
                    seen_reviews.add(key)
                days[date_key]["count"] += 1
                if day == date_key and len(entries) < 100:
                    claim = _claim_row(connection, item["claim_id"])
                    entries.append({**item, "title": claim["statement"]})
        return {
            "timezone": timezone,
            "today": today.isoformat(),
            "days": list(days.values()),
            "active_days": sum(d["count"] > 0 for d in days.values()),
            "total": sum(d["count"] for d in days.values()),
            "day": day,
            "entries": entries,
            "entries_truncated": bool(day and days[day]["count"] > 100),
        }
