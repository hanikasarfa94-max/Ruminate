"""Real stdio writes and shared durable capture semantics; no remote model required."""

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import connect

ROOT = Path(__file__).resolve().parents[1]


def parameters(data: Path, allowed: Path | None = None) -> StdioServerParameters:
    args = ["-m", "ruminate", "--data-dir", str(data)]
    if allowed is not None:
        args += ["--allow-root", str(allowed)]
    return StdioServerParameters(
        command=sys.executable,
        args=[*args, "mcp"],
        cwd=str(ROOT),
        env={"RUMINATE_MCP_WRITES": "1", "RUMINATE_NETWORK_MODE": "off"},
    )


def test_stdio_write_workflow_and_durable_replay(tmp_path: Path) -> None:
    source = tmp_path / "sample.md"
    source.write_text("# Research\n\nA workflow must preserve evidence.", encoding="utf-8")
    app = Application(Config(tmp_path / "data", "off", (source,)))
    app.initialize()
    saved = {}

    async def run() -> None:
        async with asyncio.timeout(30):
            async with Client(parameters(app.config.data_dir, source)) as client:
                listing = await client.list_tools()
                assert len(listing.tools) == 28
                by_name = {tool.name: tool for tool in listing.tools}
                for name in (
                    "create_topic",
                    "update_topic",
                    "update_source",
                    "ingest_url",
                    "ingest_local_file",
                    "record_claim",
                    "link_evidence",
                    "present_review_queue",
                    "record_review_feedback",
                    "create_output_project",
                    "create_output_draft",
                    "record_visual_observation",
                ):
                    assert by_name[name].annotations.read_only_hint is False
                    assert by_name[name].annotations.idempotent_hint is True
                assert by_name["ingest_url"].annotations.open_world_hint is True
                assert "create_belief" not in by_name

                async def call(name: str, args: dict) -> dict:
                    result = await client.call_tool(name, args)
                    assert not result.is_error, result
                    assert result.structured_content is not None
                    return result.structured_content

                topic = await call(
                    "create_topic", {"slug": "research", "name": "Research", "request_id": "topic"}
                )
                imported = await call(
                    "ingest_local_file",
                    {"path": str(source), "topic_id": topic["topic_id"], "request_id": "capture"},
                )
                assert imported["status"] == "indexed"
                source.unlink()
                assert (
                    await call(
                        "ingest_local_file",
                        {
                            "path": str(source),
                            "topic_id": topic["topic_id"],
                            "request_id": "capture",
                        },
                    )
                    == imported
                )
                document = await call("get_source", {"source_id": imported["source_id"]})
                block = document["blocks"][1]
                claim_input = {
                    "topic_id": topic["topic_id"],
                    "statement": "Evidence should be preserved.",
                    "claim_kind": "source_assertion",
                    "block_id": block["block_id"],
                    "start_char": 0,
                    "end_char": len(block["text"]),
                    "request_id": "claim",
                }
                claim = await call("record_claim", claim_input)
                assert claim["origin"] == "host_model"
                assert claim["evidence"][0]["quote"] == block["text"]
                assert await call("record_claim", claim_input) == claim
                project = await call(
                    "create_output_project",
                    {
                        "topic_id": topic["topic_id"],
                        "title": "Evidence workflow",
                        "request_id": "out",
                    },
                )
                draft_input = {"project_id": project["project_id"], "request_id": "draft"}
                draft = await call("create_output_draft", draft_input)
                assert draft["citation_count"] == 1
                revision = await call("get_output_revision", {"project_id": project["project_id"]})
                assert revision["snapshot"]["claims"][0]["evidence"][0]["quote"] == block["text"]
                assert "[^e1]" in revision["markdown"]
                context = await call("get_topic_context", {"topic_id": topic["topic_id"]})
                assert context["confirmed_beliefs"] == []
                assert context["belief_candidates"] == []
                app.reviews.tick()
                presented = await call("present_review_queue", {"request_id": "present"})
                assert presented["shown"] == 1
                assert await call("present_review_queue", {"request_id": "present"}) == presented
                feedback_input = {
                    "review_id": presented["cards"][0]["id"],
                    "action": "postpone",
                    "request_id": "feedback",
                }
                feedback = await call("record_review_feedback", feedback_input)
                assert await call("record_review_feedback", feedback_input) == feedback
                assert (await call("list_review_queue", {}))["cards"] == []
                renamed = await call("update_topic", {
                    "topic_id": topic["topic_id"], "name": "Updated research",
                    "request_id": "rename-topic",
                })
                assert renamed["topic_id"] == topic["topic_id"]
                await call("update_source", {
                    "source_id": imported["source_id"], "title": "Named source",
                    "topic_ids": [], "request_id": "edit-source",
                })
                edited = await call("get_source", {"source_id": imported["source_id"]})
                assert edited["display_title"] == "Named source"
                assert edited["topics"] == []
                assert edited["blocks"] == document["blocks"]
                assert (
                    await call("get_output_revision", {"project_id": project["project_id"]})
                    == revision
                )
                saved.update(draft_input=draft_input, draft=draft)
            async with Client(parameters(app.config.data_dir)) as restarted:
                replay = await restarted.call_tool("create_output_draft", saved["draft_input"])
                assert not replay.is_error
                assert replay.structured_content == saved["draft"]

    asyncio.run(run())
    with connect(app.config.database_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM output_revisions").fetchone()[0] == 1


def test_stdio_writes_retain_file_network_and_input_guards(tmp_path: Path) -> None:
    source = tmp_path / "private.md"
    source.write_text("Do not import without a file grant.")
    app = Application(Config(tmp_path / "data", "off"))
    app.initialize()

    async def run() -> None:
        async with asyncio.timeout(20):
            async with Client(parameters(app.config.data_dir)) as client:
                denied = await client.call_tool(
                    "ingest_local_file", {"path": str(source), "request_id": "denied-file"}
                )
                assert denied.structured_content["status"] == "needs_attention"
                assert denied.structured_content["source_id"] is None
                blocked = await client.call_tool(
                    "ingest_url",
                    {
                        "url": "https://example.com/paper",
                        "request_id": "denied-url",
                        "network_allowed": True,
                    },
                )
                assert blocked.structured_content["status"] == "needs_attention"
                assert blocked.structured_content["error_code"] == "network_disabled"
                invalid_bool = await client.call_tool(
                    "ingest_url",
                    {
                        "url": "https://example.com/paper",
                        "request_id": "bad-bool",
                        "network_allowed": "true",
                    },
                )
                assert invalid_bool.is_error
                conflict = await client.call_tool(
                    "ingest_url",
                    {
                        "url": "https://example.com/other",
                        "request_id": "denied-url",
                        "network_allowed": True,
                    },
                )
                assert conflict.is_error
                assert "idempotency_conflict" in str(conflict.content)
                missing_key = await client.call_tool(
                    "create_topic", {"slug": "missing", "name": "Missing"}
                )
                assert missing_key.is_error
                bogus = await client.call_tool(
                    "record_claim",
                    {
                        "topic_id": "missing",
                        "statement": "A claim",
                        "claim_kind": "fact",
                        "request_id": "bad-kind",
                    },
                )
                assert bogus.is_error

    asyncio.run(run())
    assert app.list_sources() == []
    assert app.knowledge.list_topics() == []


def test_capture_idempotency_handles_concurrent_requests_and_explicit_retry(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Evidence\n\nPreserve this paragraph.")
    app = Application(Config(tmp_path / "data", "off", (source,)))
    app.initialize()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: app.ingest_file(source, request_id="same"), range(4)))
    assert len({result["job_id"] for result in results}) == 1
    assert app.ingest_file(source, request_id="same")["status"] == "indexed"
    source.write_bytes(b"\x00\x00\x00")
    broken = app.ingest_file(source, request_id="broken")
    assert broken["status"] == "needs_attention"
    assert app.ingest_file(source, request_id="broken")["attempts"] == 1
    retried = app.retry_job(broken["job_id"])
    assert retried["status"] == "needs_attention"
    assert app.get_job(broken["job_id"])["attempts"] == 2


def test_uploaded_bytes_use_durable_snapshot_and_no_local_path(tmp_path: Path) -> None:
    app = Application(Config(tmp_path / "data", "off"))
    app.initialize()
    body = "# 上传笔记\n\n原文引用应能回到保存的资料。".encode()
    first = app.ingest_upload("笔记.md", body, topic="research", request_id="upload")
    assert first["status"] == "indexed"
    restarted = Application(app.config)
    assert restarted.ingest_upload("笔记.md", body, topic="research", request_id="upload") == first
    source = app.get_source(first["source_id"])
    assert source["canonical_uri"].startswith("upload://")
    assert "原文引用" in source["blocks"][1]["text"]
    with pytest.raises(RuminateError, match="Request id"):
        app.ingest_upload("笔记.md", b"Changed text", topic="research", request_id="upload")
    with connect(app.config.database_path, readonly=True) as connection:
        snapshot = json.loads(
            connection.execute("SELECT snapshot_json FROM job_inputs").fetchone()[0]
        )
        assert snapshot["sha256"] == source["sha256"]
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "filename", ["../secret.md", "/etc/passwd", "a\\b.md", "bad\nname.md", "."]
)
def test_upload_rejects_directory_names(tmp_path: Path, filename: str) -> None:
    app = Application(Config(tmp_path / "data", "off"))
    app.initialize()
    with pytest.raises(RuminateError):
        app.ingest_upload(filename, b"# A note", request_id="bad-name")
    assert app.list_jobs() == []


def test_upload_rejects_oversize_without_writing(tmp_path: Path) -> None:
    app = Application(Config(tmp_path / "data", "off", max_source_bytes=10))
    app.initialize()
    with pytest.raises(RuminateError, match="size limit"):
        app.ingest_upload("large.md", b"x" * 11, request_id="too-large")
    assert app.list_jobs() == []


def test_uploaded_snapshot_and_request_replay_survive_backup_restore(tmp_path: Path) -> None:
    app = Application(Config(tmp_path / "data", "off"))
    app.initialize()
    body = b"# An uploaded note\n\nRetain the original bytes and the capture request."
    first = app.ingest_upload("note.md", body, request_id="upload")
    archive = tmp_path / "backup.zip"
    app.backups.export_backup(archive)
    restored = Application(Config(tmp_path / "restored", "off"))
    restored.backups.import_backup(archive)
    assert restored.ingest_upload("note.md", body, request_id="upload") == first
    assert restored.get_source(first["source_id"])["blocks"][1]["text"].startswith("Retain")
    assert len(restored.list_jobs()) == 1
