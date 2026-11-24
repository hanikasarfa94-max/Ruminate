"""Deterministic research drafts with immutable evidence snapshots and local export."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, cast

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.ingestion.files import BlobStore, directory_fd, read_at
from ruminate.persistence.common import (
    Store,
    audit,
    json_dump,
    mutate,
    new_id,
    resolve_topic,
    timestamp,
    validate_limit,
    validate_text,
)

TEMPLATES = {
    "research_note": ("研究问题", "已有证据", "待复核判断", "下一步阅读"),
    "literature_review": ("材料覆盖范围", "已有主张", "证据分歧", "研究缺口"),
    "research_proposal": ("研究问题", "已有依据", "待验证假设", "研究设计", "预期产出"),
    "product_spec": ("目标问题", "已有依据", "需求假设", "验证计划", "验收条件"),
    "article": ("文章主题", "论证依据", "争议与边界", "待补充材料"),
}
OUTPUT_FORMAT_VERSION = 2
PROMPT_VERSIONS = {1: "deterministic-output-v1", 2: "deterministic-output-v2"}
PROMPT_VERSION = PROMPT_VERSIONS[OUTPUT_FORMAT_VERSION]
MAX_SNAPSHOT_CHARS = 200_000
MAX_CITATIONS = 200


def _escape(value: str) -> str:
    """Treat arbitrary titles, claims and quotes as single-line Markdown text."""
    escaped = html.escape(" ".join(value.split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>\-])", r"\\\1", escaped)


def _classification(claim: dict[str, Any]) -> str:
    if claim["claim_kind"] == "hypothesis":
        return "hypothesis"
    if claim["claim_kind"] != "source_assertion":
        return "needs_verification"
    relations = {item["relation"] for item in claim["evidence"]}
    if "challenges" in relations:
        return "contested"
    return "supported" if "supports" in relations else "needs_verification"


def _location(citation: dict[str, Any]) -> str:
    if citation["page"] is not None:
        return f"PDF 页 {citation['page']}"
    if citation["section"]:
        return f"章节 {citation['section']}"
    locator = citation["locator"]
    if "line_start" in locator:
        return f"行 {locator['line_start']}–{locator.get('line_end', locator['line_start'])}"
    return "定位 " + json_dump(locator)


def _render(snapshot: dict[str, Any]) -> str:
    # Format 1 must reproduce saved revisions byte for byte for validation and export.
    if (
        type(snapshot["format_version"]) is not int
        or snapshot["format_version"] not in PROMPT_VERSIONS
    ):
        raise ValueError("Unsupported output format")
    legacy = snapshot["format_version"] == 1
    project = snapshot["project"]
    lines = [
        f"# {_escape(project['title'])}",
        "",
        (
            "> 确定性证据草稿，待人工审核。支持关系表示已有来源支持该主张，"
            "不代表独立核实。模型推断和假设不会自动升级为事实。"
            if legacy
            else "> 研究草稿，待人工审核。以下按想法类型整理，并附原文引用。"
        ),
        "",
        f"- 类型：{project['kind']}",
        f"- 受众：{_escape(project['target_audience']) or '待明确'}",
        f"- 目标：{_escape(project['goal']) or '待明确'}",
        f"- 版本：{snapshot['version']}；生成时间：{snapshot['created_at']}",
        "",
        "## 提纲",
        "",
    ]
    lines.extend(
        f"{index}. {heading}" for index, heading in enumerate(TEMPLATES[project["kind"]], 1)
    )
    lines.extend(["", "## 主张与证据", ""])
    citations: list[dict[str, Any]] = []
    labels = {
        "supported": "有支持来源，关系待复核" if legacy else "有来源支持",
        "contested": "存在质疑，待比较" if legacy else "存在不同观点",
        "hypothesis": "待验证假设",
        "needs_verification": "待验证" if legacy else "待讨论",
    }
    kind_labels = {
        "source_assertion": "资料观点",
        "model_inference": "推测",
        "hypothesis": "假设",
        "question": "问题",
    }
    origin_labels = {"human": "手动记录", "host_model": "AI 提出", "deterministic": "本地整理"}
    for claim in snapshot["claims"]:
        refs = []
        for evidence in claim["evidence"]:
            citations.append(evidence)
            refs.append(f"[^e{len(citations)}]")
        lines.extend(
            [
                f"- **{labels[claim['classification']]}**：{_escape(claim['statement'])} "
                + " ".join(refs),
                (
                    f"  - 类型：{claim['claim_kind']}；来源：{claim['origin']}；"
                    f"主张 ID：{_escape(claim['claim_id'])}。"
                    if legacy
                    else f"  - {kind_labels[claim['claim_kind']]} · "
                    f"{origin_labels.get(claim['origin'], claim['origin'])}"
                ),
            ]
        )
        if not refs:
            lines.append(
                "  - 缺少原文证据；不得作为已确认结论。" if legacy else "  - 待补充原文引用。"
            )
    lines.extend(["", "## 后续复核与行动" if legacy else "## 下一步", ""])
    for claim in snapshot["claims"]:
        verb = {
            "supported": "核对引用片段是否足以支持该主张，并补充适用范围",
            "contested": "比较支持与质疑来源的方法、范围和条件",
            "hypothesis": "设计可证伪的检验并补充证据",
            "needs_verification": "寻找直接证据，或保持为待验证问题",
        }[claim["classification"]]
        lines.append(f"- [ ] {verb}：{_escape(claim['statement'])}")
    lines.extend(
        [
            (
                "- [ ] 由作者补充方法、样本、评价指标及执行安排；本草稿未生成实验结果。"
                if legacy
                else "- [ ] 补充研究方法、样本、评价指标和执行安排。"
            ),
            "- [ ] 审核所有主张和引用后再对外使用。"
            if legacy
            else "- [ ] 核对论点与引用，完成正文。",
            "",
            "## 来源与定位",
            "",
        ]
    )
    relation_note = "（语义关系未独立核验）" if legacy else ""
    for index, citation in enumerate(citations, 1):
        # These paths contain only validated CAS identifiers; no source-controlled URL is linked.
        relative = "../../../" + citation["blob_path"]
        location = _escape(_location(citation))
        quote = _escape(citation["quote"][:320])
        suffix = "（节选）" if len(citation["quote"]) > 320 else ""
        lines.append(
            f"[^e{index}]: {_escape(citation['title'])}；{location}；"
            f"version={_escape(citation['version_id'])}；block={_escape(citation['block_id'])}；"
            f"块内字符 {citation['start_char']}–{citation['end_char']}；"
            f"关系={citation['relation']}{relation_note}；"
            f"[保存的原件]({relative})。原文：“{quote}”{suffix}。"
            f"来源标识：{_escape(citation['canonical_uri'])}"
        )
    if not citations:
        lines.append("暂无可引用原文；全部内容保持为待验证。" if legacy else "待补充原文引用。")
    return "\n".join(lines) + "\n"


def _project(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    validate_text(project_id, "project_id", 100)
    row = connection.execute("SELECT * FROM output_projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise RuminateError("project_not_found", "Output project does not exist")
    return cast(sqlite3.Row, row)


def _project_result(row: sqlite3.Row) -> dict[str, Any]:
    return {"project_id": row["id"], **{key: row[key] for key in row.keys() if key != "id"}}


def _revision_result(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "revision_id": row["id"],
        "project_id": row["project_id"],
        "version": row["version"],
        "created_at": row["created_at"],
        "provider": row["provider"],
        "prompt_version": row["prompt_version"],
        "review_status": row["review_status"],
    }


class OutputService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(config)

    def create_project(
        self,
        topic_id: str,
        title: str,
        *,
        kind: str = "research_note",
        target_audience: str = "",
        goal: str = "",
        request_id: str,
    ) -> dict[str, Any]:
        validate_text(title, "title", 300)
        validate_text(target_audience, "target_audience", 500, empty=True)
        validate_text(goal, "goal", 2000, empty=True)
        if not isinstance(kind, str) or kind not in TEMPLATES:
            raise RuminateError("invalid_template", "Unknown output template")
        payload = {
            "topic_id": topic_id,
            "title": title,
            "kind": kind,
            "target_audience": target_audience,
            "goal": goal,
        }
        with self.store.write() as connection:

            def perform() -> dict[str, Any]:
                topic = resolve_topic(connection, topic_id)
                project_id = new_id("out")
                connection.execute(
                    "INSERT INTO output_projects"
                    "(id,topic_id,title,kind,target_audience,goal,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (project_id, topic["id"], title, kind, target_audience, goal, timestamp()),
                )
                audit(connection, "output_project_created", project_id, {"topic_id": topic["id"]})
                return _project_result(_project(connection, project_id))

            return mutate(connection, "create_output_project", request_id, payload, perform)

    def list_projects(
        self,
        *,
        topic_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        validate_limit(limit)
        if status is not None and (
            not isinstance(status, str) or status not in {"active", "archived"}
        ):
            raise RuminateError("invalid_status", "Unknown output project status")
        with self.store.read() as connection:
            clauses, values = [], []
            if topic_id is not None:
                clauses.append("topic_id=?")
                values.append(resolve_topic(connection, topic_id)["id"])
            if status is not None:
                clauses.append("status=?")
                values.append(status)
            query = "SELECT * FROM output_projects"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            rows = connection.execute(
                query + " ORDER BY created_at DESC,id LIMIT ?", [*values, limit]
            )
            return [_project_result(row) for row in rows]

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.store.read() as connection:
            project = _project_result(_project(connection, project_id))
            rows = connection.execute(
                "SELECT * FROM output_revisions WHERE project_id=? ORDER BY version DESC LIMIT 50",
                (project_id,),
            )
            project["revisions"] = [_revision_result(row) for row in rows]
            project["revision_count"] = connection.execute(
                "SELECT COUNT(*) FROM output_revisions WHERE project_id=?", (project_id,)
            ).fetchone()[0]
            return project

    def _claims(
        self,
        connection: sqlite3.Connection,
        project: sqlite3.Row,
        claim_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        if claim_ids is None:
            rows = connection.execute(
                "SELECT * FROM claims WHERE (topic_id=? OR id IN "
                "(SELECT claim_id FROM claim_topics WHERE topic_id=?)) AND status='candidate' "
                "ORDER BY created_at,id "
                "LIMIT 51",
                (project["topic_id"], project["topic_id"]),
            ).fetchall()
            if len(rows) > 50:
                raise RuminateError("output_budget", "Select at most 50 claims explicitly")
        else:
            rows = []
            for claim_id in claim_ids:
                row = connection.execute(
                    "SELECT * FROM claims WHERE id=? AND (topic_id=? OR id IN "
                    "(SELECT claim_id FROM claim_topics WHERE topic_id=?)) AND status='candidate'",
                    (claim_id, project["topic_id"], project["topic_id"]),
                ).fetchone()
                if row is None:
                    raise RuminateError("claim_not_found", "Claim is unavailable in this topic")
                rows.append(row)
        if not rows:
            raise RuminateError("no_claims", "Add claims to this topic before drafting")
        claims = []
        citation_count = 0
        for row in rows:
            evidence_rows = connection.execute(
                "SELECT e.*,b.text AS block_text,b.page,b.section,b.locator_json,"
                "b.version_id AS actual_version,v.source_id,v.title,v.sha256,v.blob_path,"
                "s.canonical_uri FROM claim_evidence e JOIN blocks b ON b.id=e.block_id "
                "JOIN source_versions v ON v.id=b.version_id JOIN sources s ON s.id=v.source_id "
                "WHERE e.claim_id=? ORDER BY e.created_at,e.id LIMIT 201",
                (row["id"],),
            ).fetchall()
            citation_count += len(evidence_rows)
            if citation_count > MAX_CITATIONS:
                raise RuminateError(
                    "output_budget", "Draft has too many citations; select fewer claims"
                )
            evidence = [self._evidence(item) for item in evidence_rows]
            claim = {
                "claim_id": row["id"],
                "statement": row["statement"],
                "claim_kind": row["claim_kind"],
                "origin": row["origin"],
                "status": row["status"],
                "evidence": evidence,
            }
            if row["topic_id"] != project["topic_id"]:
                assignment = connection.execute(
                    "SELECT suggestion_id FROM claim_topics WHERE claim_id=? AND topic_id=?",
                    (row["id"], project["topic_id"]),
                ).fetchone()
                claim["topic_assignment"] = {"suggestion_id": assignment[0],
                                             "topic_id": project["topic_id"]}
            claim["classification"] = _classification(claim)
            claims.append(claim)
        return claims

    @staticmethod
    def _evidence(row: sqlite3.Row) -> dict[str, Any]:
        start, end = row["start_char"], row["end_char"]
        if (
            row["version_id"] != row["actual_version"]
            or not 0 <= start < end <= len(row["block_text"])
            or row["block_text"][start:end] != row["quote"]
        ):
            raise RuminateError("citation_invalid", "Evidence does not match its saved block")
        digest = row["sha256"]
        if (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or row["blob_path"] != f"blobs/{digest[:2]}/{digest}"
        ):
            raise RuminateError("citation_invalid", "Source blob reference is invalid")
        return {
            "evidence_id": row["id"],
            "source_id": row["source_id"],
            "version_id": row["version_id"],
            "block_id": row["block_id"],
            "source_sha256": digest,
            "blob_path": row["blob_path"],
            "title": row["title"],
            "canonical_uri": row["canonical_uri"],
            "page": row["page"],
            "section": row["section"],
            "locator": json.loads(row["locator_json"]),
            "start_char": start,
            "end_char": end,
            "quote": row["quote"],
            "relation": row["relation"],
            "verifier_status": row["verifier_status"],
            "semantic_verification": "not_verified",
        }

    def create_draft(
        self,
        project_id: str,
        *,
        claim_ids: list[str] | None = None,
        request_id: str,
    ) -> dict[str, Any]:
        if claim_ids is not None:
            if not isinstance(claim_ids, list) or not 1 <= len(claim_ids) <= 50:
                raise RuminateError("invalid_claims", "Select between 1 and 50 distinct claims")
            for claim_id in claim_ids:
                validate_text(claim_id, "claim_id", 100)
            if len(set(claim_ids)) != len(claim_ids):
                raise RuminateError("invalid_claims", "Duplicate claim selections are not allowed")
        with self.store.write() as connection:

            def perform() -> dict[str, Any]:
                project = _project(connection, project_id)
                if project["status"] != "active":
                    raise RuminateError("project_archived", "Cannot draft an archived project")
                claims = self._claims(connection, project, claim_ids)
                version = connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM output_revisions WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                created_at = timestamp()
                snapshot = {
                    "format_version": OUTPUT_FORMAT_VERSION,
                    "project": _project_result(project),
                    "version": version,
                    "created_at": created_at,
                    "claims": claims,
                }
                encoded = json_dump(snapshot)
                if len(encoded) > MAX_SNAPSHOT_CHARS:
                    raise RuminateError(
                        "output_budget", "Selected evidence exceeds the draft budget"
                    )
                markdown = _render(snapshot)
                self._validate_snapshot(connection, snapshot, markdown, verify_blobs=True)
                revision_id = new_id("rev")
                connection.execute(
                    "INSERT INTO output_revisions(id,project_id,version,markdown,snapshot_json,"
                    "created_at,provider,prompt_version) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        revision_id,
                        project_id,
                        version,
                        markdown,
                        encoded,
                        created_at,
                        "none",
                        PROMPT_VERSION,
                    ),
                )
                audit(
                    connection,
                    "output_draft_created",
                    revision_id,
                    {"project_id": project_id, "version": version, "claim_count": len(claims)},
                )
                return {
                    "revision_id": revision_id,
                    "project_id": project_id,
                    "version": version,
                    "created_at": created_at,
                    "provider": "none",
                    "prompt_version": PROMPT_VERSION,
                    "review_status": "needs_review",
                    "claim_count": len(claims),
                    "citation_count": sum(len(claim["evidence"]) for claim in claims),
                }

            return mutate(
                connection,
                "create_output_draft",
                request_id,
                {"project_id": project_id, "claim_ids": claim_ids},
                perform,
            )

    def _validate_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: dict[str, Any],
        markdown: str,
        *,
        verify_blobs: bool,
    ) -> None:
        try:
            if (
                type(snapshot["format_version"]) is not int
                or snapshot["format_version"] not in PROMPT_VERSIONS
                or not 1 <= len(snapshot["claims"]) <= 50
            ):
                raise ValueError("Invalid snapshot")
            if len(json_dump(snapshot)) > MAX_SNAPSHOT_CHARS:
                raise ValueError("Snapshot budget exceeded")
            if sum(len(claim["evidence"]) for claim in snapshot["claims"]) > MAX_CITATIONS:
                raise ValueError("Citation budget exceeded")
            if _render(snapshot) != markdown:
                raise ValueError("Draft text does not match snapshot")
            verified = set()
            for claim in snapshot["claims"]:
                saved_claim = connection.execute(
                    "SELECT topic_id,claim_kind,origin FROM claims WHERE id=?", (claim["claim_id"],)
                ).fetchone()
                if (
                    saved_claim is None
                    or any(claim[key] != saved_claim[key] for key in ("claim_kind", "origin"))
                ):
                    raise ValueError("Claim provenance mismatch")
                if saved_claim["topic_id"] != snapshot["project"]["topic_id"]:
                    assignment = claim.get("topic_assignment", {})
                    if not isinstance(assignment, dict):
                        raise ValueError("Invalid topic assignment")
                    if (assignment.get("topic_id") != snapshot["project"]["topic_id"]
                            or not connection.execute(
                                "SELECT 1 FROM topic_suggestions WHERE id=? "
                                "AND target_kind='claim' "
                                "AND target_id=? AND topic_id=? "
                                "AND status IN ('applied','reverted')",
                                (assignment.get("suggestion_id"), claim["claim_id"],
                                 snapshot["project"]["topic_id"]),
                            ).fetchone()):
                        raise ValueError("Claim topic assignment mismatch")
                if _classification(claim) != claim["classification"]:
                    raise ValueError("Invalid classification")
                for citation in claim["evidence"]:
                    link = connection.execute(
                        "SELECT * FROM claim_evidence WHERE id=? AND claim_id=?",
                        (citation["evidence_id"], claim["claim_id"]),
                    ).fetchone()
                    if (
                        link is None
                        or any(
                            citation[key] != link[key]
                            for key in (
                                "block_id",
                                "version_id",
                                "relation",
                                "start_char",
                                "end_char",
                                "quote",
                                "verifier_status",
                            )
                        )
                        or citation["semantic_verification"] != "not_verified"
                    ):
                        raise ValueError("Evidence link mismatch")
                    block = connection.execute(
                        "SELECT b.*,v.source_id,v.sha256,v.blob_path,v.title,s.canonical_uri "
                        "FROM blocks b JOIN source_versions v ON v.id=b.version_id "
                        "JOIN sources s ON s.id=v.source_id WHERE b.id=?",
                        (citation["block_id"],),
                    ).fetchone()
                    if block is None:
                        raise ValueError("Missing saved block")
                    checks = {
                        "version_id": block["version_id"],
                        "source_id": block["source_id"],
                        "source_sha256": block["sha256"],
                        "blob_path": block["blob_path"],
                        "title": block["title"],
                        "canonical_uri": block["canonical_uri"],
                        "page": block["page"],
                        "section": block["section"],
                        "locator": json.loads(block["locator_json"]),
                    }
                    if any(citation[key] != value for key, value in checks.items()):
                        raise ValueError("Citation metadata mismatch")
                    start, end = citation["start_char"], citation["end_char"]
                    if (
                        type(start) is not int
                        or type(end) is not int
                        or not 0 <= start < end <= len(block["text"])
                        or block["text"][start:end] != citation["quote"]
                    ):
                        raise ValueError("Citation span mismatch")
                    if verify_blobs and block["sha256"] not in verified:
                        BlobStore(self.config).read(block["sha256"], block["blob_path"])
                        verified.add(block["sha256"])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RuminateError(
                "citation_invalid", "Saved draft failed citation validation"
            ) from exc

    def get_revision(self, project_id: str, *, revision: int | None = None) -> dict[str, Any]:
        if revision is not None and (type(revision) is not int or not 1 <= revision < 2**63):
            raise RuminateError("invalid_revision", "Revision must be a positive integer")
        with self.store.read() as connection:
            _project(connection, project_id)
            if revision is None:
                row = connection.execute(
                    "SELECT * FROM output_revisions WHERE project_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM output_revisions WHERE project_id=? AND version=?",
                    (project_id, revision),
                ).fetchone()
            if row is None:
                raise RuminateError("revision_not_found", "Output revision does not exist")
            try:
                snapshot = json.loads(row["snapshot_json"])
                if (
                    snapshot["project"]["project_id"] != row["project_id"]
                    or snapshot["version"] != row["version"]
                    or snapshot["created_at"] != row["created_at"]
                    or row["provider"] != "none"
                    or row["prompt_version"] != PROMPT_VERSIONS.get(snapshot["format_version"])
                ):
                    raise ValueError("Revision metadata mismatch")
                self._validate_snapshot(connection, snapshot, row["markdown"], verify_blobs=False)
            except (KeyError, ValueError, TypeError) as exc:
                raise RuminateError("citation_invalid", "Saved snapshot is invalid") from exc
            return {
                **_revision_result(row),
                "markdown": row["markdown"],
                "snapshot": snapshot,
                "citation_audit": "exact_stored_spans",
                "semantic_verification": "not_verified",
            }

    def export(self, project_id: str, *, revision: int | None = None) -> dict[str, Any]:
        result = self.get_revision(project_id, revision=revision)
        # Only server-generated IDs can become path components, even if a database was altered.
        if not re.fullmatch(r"out_[0-9a-f]{32}", project_id) or not re.fullmatch(
            r"rev_[0-9a-f]{32}", result["revision_id"]
        ):
            raise RuminateError("unsafe_output_path", "Output identifiers are invalid")
        with self.store.read() as connection:
            self._validate_snapshot(
                connection, result["snapshot"], result["markdown"], verify_blobs=True
            )
        sidecar = {key: value for key, value in result.items() if key != "markdown"}
        files = {
            "draft.md": result["markdown"].encode("utf-8"),
            "evidence.json": (json_dump(sidecar) + "\n").encode("utf-8"),
        }
        root = self.config.data_dir / "outputs"
        with directory_fd(root) as output_fd:
            try:
                os.mkdir(project_id, 0o700, dir_fd=output_fd)
            except FileExistsError:
                pass
        folder = root / project_id
        final = folder / result["revision_id"]
        with directory_fd(folder) as parent:
            try:
                self._check_export(final, files)
            except FileNotFoundError:
                self._write_export(parent, folder, result["revision_id"], files)
        return {
            **{key: value for key, value in result.items() if key not in {"markdown", "snapshot"}},
            "markdown_path": str(final / "draft.md"),
            "sidecar_path": str(final / "evidence.json"),
        }

    @staticmethod
    def _check_export(directory: Path, files: dict[str, bytes]) -> None:
        with directory_fd(directory) as descriptor:
            if set(os.listdir(descriptor)) != set(files):
                raise RuminateError("output_exists", "Existing export contains unexpected files")
            for name, content in files.items():
                if read_at(descriptor, name, 2_000_000) != content:
                    raise RuminateError(
                        "output_exists", "Existing export differs; refusing overwrite"
                    )

    @staticmethod
    def _write_export(parent: int, folder: Path, revision_id: str, files: dict[str, bytes]) -> None:
        temporary = "." + new_id("tmp")
        os.mkdir(temporary, 0o700, dir_fd=parent)
        descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        published = False
        try:
            for name, content in files.items():
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=descriptor,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.fsync(descriptor)
            try:
                os.rename(temporary, revision_id, src_dir_fd=parent, dst_dir_fd=parent)
            except OSError:
                # Concurrent identical exports are safe only after checking every final byte.
                OutputService._check_export(folder / revision_id, files)
            else:
                published = True
                os.fsync(parent)
        finally:
            if not published:
                for name in files:
                    try:
                        os.unlink(name, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
            os.close(descriptor)
            try:
                os.rmdir(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass

    def impact(self, project_id: str) -> dict[str, Any]:
        revision = self.get_revision(project_id)
        old_versions: list[dict[str, Any]] = []
        new_evidence: list[dict[str, Any]] = []
        changed_claims: list[str] = []
        new_evidence_count = 0
        with self.store.read() as connection:
            for claim in revision["snapshot"]["claims"]:
                current = connection.execute(
                    "SELECT * FROM claims WHERE id=?", (claim["claim_id"],)
                ).fetchone()
                if (
                    current is None
                    or current["statement"] != claim["statement"]
                    or current["status"] != claim["status"]
                ):
                    changed_claims.append(claim["claim_id"])
                saved_ids = {item["evidence_id"] for item in claim["evidence"]}
                where = "claim_id=?"
                parameters = [claim["claim_id"]]
                if saved_ids:
                    where += " AND id NOT IN (" + ",".join("?" for _ in saved_ids) + ")"
                    parameters.extend(sorted(saved_ids))
                new_evidence_count += connection.execute(
                    "SELECT COUNT(*) FROM claim_evidence WHERE " + where,
                    parameters,
                ).fetchone()[0]
                remaining = 200 - len(new_evidence)
                if remaining:
                    rows = connection.execute(
                        "SELECT id,relation,block_id,version_id FROM claim_evidence WHERE "
                        + where
                        + " ORDER BY created_at,id LIMIT ?",
                        [*parameters, remaining],
                    )
                    for item in rows:
                        new_evidence.append({"claim_id": claim["claim_id"], **dict(item)})
                for citation in claim["evidence"]:
                    current_version = connection.execute(
                        "SELECT current_version_id FROM sources WHERE id=?",
                        (citation["source_id"],),
                    ).fetchone()
                    if current_version and current_version[0] != citation["version_id"]:
                        entry = {
                            "source_id": citation["source_id"],
                            "cited_version_id": citation["version_id"],
                            "current_version_id": current_version[0],
                        }
                        if entry not in old_versions:
                            old_versions.append(entry)
        return {
            "project_id": project_id,
            "revision": revision["version"],
            "superseded_source_versions": old_versions,
            "new_evidence": new_evidence,
            "new_evidence_count": new_evidence_count,
            "changed_claims": changed_claims,
            "new_evidence_truncated": new_evidence_count > len(new_evidence),
            "requires_review": bool(old_versions or new_evidence_count or changed_claims),
            "revision_modified": False,
        }
