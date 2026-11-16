"""Bounded workspace operations shared by the MCP App and optional HTTP preview."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from ruminate.application import Application
from ruminate.errors import RuminateError
from ruminate.persistence.common import validate_text

ReadAction = Literal[
    "belief",
    "management",
    "state",
    "topics",
    "sources",
    "source",
    "source_page",
    "observations",
    "topic_context",
    "claim",
    "reviews",
    "projects",
    "project",
    "revision",
    "impact",
    "job",
]
WriteAction = Literal[
    "create_topic",
    "update_topic",
    "update_source",
    "capture_url",
    "upload_file",
    "record_claim",
    "generate_reviews",
    "present_reviews",
    "review_feedback",
    "create_project",
    "create_draft",
]

_READ_FIELDS = {
    "belief": {"belief_id", "limit", "offset"},
    "management": {"view", "topic", "query", "status", "sort", "limit", "offset"},
    "state": set(),
    "topics": set(),
    "sources": {"topic", "query", "limit", "offset"},
    "source": {"source_id", "version", "start", "limit"},
    "source_page": {"source_id", "version", "page"},
    "observations": {"source_id", "version", "page"},
    "topic_context": {"topic_id"},
    "claim": {"claim_id"},
    "reviews": {"topic"},
    "projects": {"topic"},
    "project": {"project_id"},
    "revision": {"project_id", "revision"},
    "impact": {"project_id"},
    "job": {"job_id"},
}
_WRITE_FIELDS = {
    "create_topic": {"slug", "name", "description"},
    "update_topic": {"topic_id", "name", "description"},
    "update_source": {"source_id", "title", "topic_ids", "reset_title"},
    "capture_url": {"url", "topic"},
    "upload_file": {"filename", "content_base64", "topic"},
    "record_claim": {
        "topic_id",
        "statement",
        "claim_kind",
        "block_id",
        "relation",
        "start_char",
        "end_char",
    },
    "generate_reviews": {"topic_id"},
    "present_reviews": {"topic_id", "limit"},
    "review_feedback": {"review_id", "action", "note", "postpone_days"},
    "create_project": {"topic_id", "title", "kind", "target_audience", "goal"},
    "create_draft": {"project_id", "claim_ids"},
}


@dataclass(frozen=True)
class WorkspaceImage:
    metadata: dict[str, Any]
    png: bytes


def _fields(action: str, params: dict[str, Any], *, write: bool) -> None:
    fields = _WRITE_FIELDS if write else _READ_FIELDS
    if action not in fields:
        raise RuminateError("invalid_workspace_action", "没有找到这个工作区操作。")
    allowed = fields[action] | ({"request_id"} if write else set())
    if set(params) - allowed:
        raise RuminateError("invalid_input", "提交内容包含不支持的字段。")


def _optional(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RuminateError("invalid_input", f"{key} must be text")
    return validate_text(value, key, 8192)


def _required(data: dict[str, Any], key: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise RuminateError("invalid_input", f"{key} must be text")
    return validate_text(value, key, 8192, empty=default == "")


def _integer(data: dict[str, Any], key: str, default: int, *, query: bool = False) -> int:
    value = data.get(key, default)
    if query and isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        value = int(value)
    if type(value) is not int:
        raise RuminateError("invalid_input", f"{key} must be an integer")
    return value


def _provided(data: dict[str, Any], key: str) -> bool:
    return data.get(key) is not None and data[key] != ""


class WorkspaceAdapter:
    """Only named local workflows; never an arbitrary URL, SQL or file-path proxy."""

    def __init__(self, app: Application) -> None:
        self.app = app

    def read(self, action: ReadAction, params: dict[str, Any]) -> Any:
        _fields(action, params, write=False)
        app = self.app
        if action == "belief":
            return app.knowledge.get_belief(
                _required(params, "belief_id"),
                limit=_integer(params, "limit", 25, query=True),
                offset=_integer(params, "offset", 0, query=True),
            )
        if action == "management":
            return app.management.list_items(
                view=_required(params, "view", "sources"),
                topic=_optional(params, "topic"),
                query=_required(params, "query", ""),
                status=_required(params, "status", "all"),
                sort=_required(params, "sort", "recent"),
                limit=_integer(params, "limit", 25, query=True),
                offset=_integer(params, "offset", 0, query=True),
            )
        if action == "state":
            return {
                "topics": app.knowledge.list_topics(limit=50),
                "sources": app.list_sources(limit=50),
                "projects": app.outputs.list_projects(limit=50),
                "health": app.health_check(),
            }
        if action == "topics":
            return app.knowledge.list_topics(limit=50)
        if action == "sources":
            term = _optional(params, "query")
            if term:
                return app.search_sources(
                    term,
                    topic=_optional(params, "topic"),
                    limit=_integer(params, "limit", 50, query=True),
                )
            return app.list_sources(
                topic=_optional(params, "topic"),
                limit=_integer(params, "limit", 50, query=True),
                offset=_integer(params, "offset", 0, query=True),
            )
        if action == "source":
            return app.get_source(
                _required(params, "source_id"),
                version_id=_optional(params, "version"),
                block_start=_integer(params, "start", 0, query=True),
                block_limit=_integer(params, "limit", 20, query=True),
            )
        if action == "source_page":
            metadata, png = app.get_source_page_image(
                _required(params, "source_id"),
                version_id=_optional(params, "version"),
                page=_integer(params, "page", 1, query=True),
            )
            return WorkspaceImage(metadata, png)
        if action == "observations":
            return app.list_visual_observations(
                _required(params, "source_id"),
                version_id=_optional(params, "version"),
                page=_integer(params, "page", 1, query=True) if _provided(params, "page") else None,
            )
        if action == "topic_context":
            return app.knowledge.topic_context(_required(params, "topic_id"), max_claims=20)
        if action == "claim":
            return app.knowledge.get_claim(_required(params, "claim_id"))
        if action == "reviews":
            return app.reviews.list_queue(_optional(params, "topic"), limit=3)
        if action == "projects":
            return app.outputs.list_projects(topic_id=_optional(params, "topic"), limit=50)
        if action == "project":
            return app.outputs.get_project(_required(params, "project_id"))
        if action == "revision":
            return app.outputs.get_revision(
                _required(params, "project_id"),
                revision=_integer(params, "revision", 1, query=True)
                if _provided(params, "revision")
                else None,
            )
        if action == "impact":
            return app.outputs.impact(_required(params, "project_id"))
        if action == "job":
            return app.get_job(_required(params, "job_id"))
        raise RuminateError("invalid_workspace_action", "没有找到这个工作区操作。")

    def write(self, action: WriteAction, params: dict[str, Any]) -> dict[str, Any]:
        _fields(action, params, write=True)
        app = self.app
        request_id = _required(params, "request_id")
        validate_text(request_id, "request_id", 100)
        if action == "create_topic":
            return app.knowledge.create_topic(
                slug=_required(
                    params, "slug", "topic-" + hashlib.sha256(request_id.encode()).hexdigest()[:12]
                ),
                name=_required(params, "name"),
                description=_required(params, "description", ""),
                request_id=request_id,
            )
        if action == "update_topic":
            return app.knowledge.update_topic(
                _required(params, "topic_id"), name=params.get("name"),
                description=params.get("description"), request_id=request_id,
            )
        if action == "update_source":
            return app.management.update_source(
                _required(params, "source_id"), title=params.get("title"),
                topic_ids=params.get("topic_ids"), reset_title=params.get("reset_title", False),
                request_id=request_id,
            )
        if action == "capture_url":
            return app.ingest_url(
                _required(params, "url"),
                topic=_optional(params, "topic"),
                network_allowed=True,
                request_id=request_id,
            )
        if action == "upload_file":
            encoded = params.get("content_base64")
            if not isinstance(encoded, str):
                raise RuminateError("invalid_input", "Expected base64 file content")
            if len(encoded) > 4 * ((app.config.max_source_bytes + 2) // 3):
                raise RuminateError("source_too_large", "文件超出了资料库的大小限制。")
            try:
                body = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise RuminateError("invalid_upload", "没有读到完整文件，请重新选择。") from exc
            return app.ingest_upload(
                _required(params, "filename"),
                body,
                topic=_optional(params, "topic"),
                request_id=request_id,
            )
        if action == "record_claim":
            values: dict[str, Any] = {
                "topic_id": _required(params, "topic_id"),
                "statement": _required(params, "statement"),
                "claim_kind": _required(params, "claim_kind", "question"),
                "origin": "human",
                "request_id": request_id,
            }
            block_id = _optional(params, "block_id")
            if block_id:
                values.update(
                    block_id=block_id,
                    relation=_required(params, "relation", "supports"),
                    start_char=_integer(params, "start_char", 0),
                    end_char=_integer(params, "end_char", 0)
                    if params.get("end_char") is not None
                    else None,
                )
                return app.knowledge.create_claim_with_evidence(**values)
            return app.knowledge.create_claim(**values)
        if action == "generate_reviews":
            return app.reviews.tick(_optional(params, "topic_id"))
        if action == "present_reviews":
            return app.reviews.present(
                _optional(params, "topic_id"),
                limit=_integer(params, "limit", 3),
                request_id=request_id,
            )
        if action == "review_feedback":
            return app.reviews.feedback(
                _required(params, "review_id"),
                _required(params, "action"),
                note=_required(params, "note", ""),
                postpone_days=_integer(params, "postpone_days", 7),
                request_id=request_id,
            )
        if action == "create_project":
            return app.outputs.create_project(
                topic_id=_required(params, "topic_id"),
                title=_required(params, "title"),
                kind=_required(params, "kind", "research_note"),
                target_audience=_required(params, "target_audience", ""),
                goal=_required(params, "goal", ""),
                request_id=request_id,
            )
        if action == "create_draft":
            claim_ids = params.get("claim_ids")
            if claim_ids is not None and (
                not isinstance(claim_ids, list) or not all(isinstance(i, str) for i in claim_ids)
            ):
                raise RuminateError("invalid_input", "claim_ids must be a list of text IDs")
            return app.outputs.create_draft(
                _required(params, "project_id"), claim_ids=claim_ids, request_id=request_id
            )
        raise RuminateError("invalid_workspace_action", "没有找到这个工作区操作。")
