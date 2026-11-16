"""MCP Apps resources and app-only bridge over an actual stdio MCP process."""

import asyncio
import base64
import json
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from pypdf import PdfWriter

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.mcp_ui import UI_MIME_TYPE, UI_URI, workspace_html
from ruminate.persistence.database import connect
from ruminate.workspace import WorkspaceAdapter

ROOT = Path(__file__).resolve().parents[1]


def parameters(data_dir: Path, *, workflow: bool = True) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "ruminate", "--data-dir", str(data_dir), "mcp"],
        cwd=str(ROOT),
        env={
            "RUMINATE_MCP_WRITES": "1" if workflow else "0",
            "RUMINATE_NETWORK_MODE": "off",
        },
    )


def logical_snapshot(app: Application) -> str:
    with connect(app.config.database_path, readonly=True) as connection:
        return "\n".join(connection.iterdump())


def test_stdio_app_discovery_resource_and_readonly_workspace(tmp_path: Path) -> None:
    app = Application(Config(tmp_path / "data", network_mode="off"))
    app.initialize()
    app.ingest_upload("source.md", b"# Stored source\n\nAn exact source block.", request_id="seed")
    before = logical_snapshot(app)

    async def run() -> None:
        async with asyncio.timeout(30):
            async with Client(parameters(app.config.data_dir)) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert len(tools) == 26
                assert tools["open_workspace"].meta["ui"] == {
                    "resourceUri": UI_URI,
                    "visibility": ["model", "app"],
                }
                assert tools["open_workspace"].meta["openai/ui"] == {
                    "entrypoints": [{"type": "global"}, {"type": "thread"}]
                }
                assert not tools["open_workspace"].input_schema.get("required")
                for name in ("ui_read", "ui_write"):
                    assert tools[name].meta == {"ui": {"visibility": ["app"]}}
                    assert "resourceUri" not in tools[name].meta["ui"]
                assert tools["open_workspace"].annotations.read_only_hint is True
                assert tools["ui_read"].annotations.read_only_hint is True
                assert tools["ui_read"].annotations.open_world_hint is False
                assert tools["ui_write"].annotations.read_only_hint is False
                assert tools["ui_write"].annotations.open_world_hint is True
                assert tools["ui_write"].annotations.idempotent_hint is True
                assert (
                    "upload_file" in tools["ui_write"].input_schema["properties"]["action"]["enum"]
                )
                resources = (await client.list_resources()).resources
                resource = next(item for item in resources if str(item.uri) == UI_URI)
                assert resource.mime_type == UI_MIME_TYPE
                document = (await client.read_resource(UI_URI)).contents[0]
                assert document.mime_type == UI_MIME_TYPE
                assert document.meta["ui"]["csp"]["connectDomains"] == []
                assert document.meta["ui"]["csp"]["resourceDomains"] == []
                assert '<html lang="zh-CN">' in document.text
                assert "window.RUMINATE_EMBEDDED = true" in document.text
                assert "<script src=" not in document.text
                assert 'rel="stylesheet"' not in document.text
                assert "ui/initialize" in document.text and "tools/call" in document.text
                opened = await client.call_tool("open_workspace", {})
                assert not opened.is_error
                assert opened.structured_content == WorkspaceAdapter(app).read("state", {})
                state = await client.call_tool("ui_read", {"action": "state"})
                assert state.structured_content["data"] == opened.structured_content
                managed = await client.call_tool(
                    "ui_read", {"action": "management", "params": {"view": "sources"}}
                )
                assert not managed.is_error
                assert managed.structured_content["data"]["total"] == 1
                assert managed.structured_content["data"]["items"][0]["title"] == "Stored source"
                source_id = opened.structured_content["sources"][0]["source_id"]
                source = await client.call_tool(
                    "ui_read",
                    {
                        "action": "source",
                        "params": {"source_id": source_id},
                    },
                )
                assert (
                    source.structured_content["data"]["blocks"][1]["text"]
                    == "An exact source block."
                )

    asyncio.run(run())
    assert logical_snapshot(app) == before


def test_stdio_app_forms_use_shared_durable_workflows(tmp_path: Path) -> None:
    app = Application(Config(tmp_path / "data", network_mode="off"))
    app.initialize()

    async def run() -> None:
        async with asyncio.timeout(30):
            async with Client(parameters(app.config.data_dir)) as client:

                async def write(action: str, params: dict) -> dict:
                    result = await client.call_tool(
                        "ui_write", {"action": action, "params": params}
                    )
                    assert not result.is_error, result
                    return result.structured_content["data"]

                async def read(action: str, **params: Any) -> Any:
                    result = await client.call_tool("ui_read", {"action": action, "params": params})
                    assert not result.is_error, result
                    return result.structured_content["data"]

                topic = await write(
                    "create_topic", {"name": "Embedded research", "request_id": "topic"}
                )
                uploaded = {
                    "filename": "研究.md",
                    "topic": topic["topic_id"],
                    "request_id": "upload",
                    "content_base64": base64.b64encode(
                        b"# Source\n\nEvidence needs a location."
                    ).decode(),
                }
                source = await write("upload_file", uploaded)
                assert source["status"] == "indexed"
                assert (await write("upload_file", uploaded))["job_id"] == source["job_id"]
                block = (await read("source", source_id=source["source_id"]))["blocks"][1]
                claim = await write(
                    "record_claim",
                    {
                        "topic_id": topic["topic_id"],
                        "statement": "Evidence needs a location.",
                        "claim_kind": "source_assertion",
                        "block_id": block["block_id"],
                        "request_id": "claim",
                    },
                )
                assert claim["evidence"][0]["quote"] == block["text"]
                assert claim["origin"] == "human"
                project = await write(
                    "create_project",
                    {
                        "topic_id": topic["topic_id"],
                        "title": "Embedded outline",
                        "request_id": "project",
                    },
                )
                draft = await write(
                    "create_draft",
                    {
                        "project_id": project["project_id"],
                        "request_id": "draft",
                    },
                )
                assert draft["citation_count"] == 1
                revision = await read("revision", project_id=project["project_id"])
                assert (
                    revision["snapshot"]["claims"][0]["evidence"][0]["version_id"]
                    == source["version_id"]
                )
                assert (
                    revision["markdown"]
                    == app.outputs.get_revision(project["project_id"])["markdown"]
                )
                await write(
                    "generate_reviews", {"topic_id": topic["topic_id"], "request_id": "generate"}
                )
                pending = await read("management", view="thoughts", status="pending")
                assert pending["total"] == 1
                assert pending["items"][0]["claim_id"] == claim["claim_id"]
                cards = await write(
                    "present_reviews", {"topic_id": topic["topic_id"], "request_id": "show"}
                )
                assert cards["cards"]
                card = cards["cards"][0]
                feedback = await write(
                    "review_feedback",
                    {
                        "review_id": card["id"],
                        "action": "postpone",
                        "request_id": "feedback",
                    },
                )
                assert feedback["status"] == "postponed"
                assert all(item["id"] != card["id"] for item in await read("reviews"))
                assert (await read("management", view="thoughts", status="pending"))["total"] == 0
                assert (await read("management", view="thoughts", status="later"))["total"] == 1

    asyncio.run(run())


def test_stdio_pdf_page_uses_image_content_not_a_localhost_url(tmp_path: Path) -> None:
    app = Application(Config(tmp_path / "data", network_mode="off"))
    app.initialize()
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=120, height=160)
    writer.add_blank_page(width=120, height=160)
    writer.write(output)
    source = app.ingest_upload("scan.pdf", output.getvalue(), request_id="pdf")

    async def run() -> None:
        async with asyncio.timeout(30):
            async with Client(parameters(app.config.data_dir)) as client:
                result = await client.call_tool(
                    "ui_read",
                    {
                        "action": "source_page",
                        "params": {"source_id": source["source_id"], "page": 2},
                    },
                )
                assert not result.is_error
                metadata = result.structured_content["data"]
                assert metadata["page"] == metadata["page_count"] == 2
                image = next(item for item in result.content if item.type == "image")
                assert image.mime_type == "image/png"
                assert base64.b64decode(image.data).startswith(b"\x89PNG\r\n\x1a\n")
                assert "http://127.0.0.1" not in json.dumps(metadata)

    asyncio.run(run())


def test_app_operations_are_whitelisted_and_uploads_remain_bounded(tmp_path: Path) -> None:
    app = Application(Config(tmp_path / "data", network_mode="off", max_source_bytes=10))
    app.initialize()
    workspace = WorkspaceAdapter(app)
    for action, params in (
        ("state", {"path": "/etc/passwd"}),
        ("upload_file", {"filename": "x.md", "request_id": "x", "content_base64": "YQ=="}),
    ):
        with pytest.raises(RuminateError):
            workspace.read(action, params)  # type: ignore[arg-type]
    with pytest.raises(RuminateError, match="大小限制"):
        workspace.write(
            "upload_file",
            {
                "filename": "x.md",
                "request_id": "x",
                "content_base64": "YQ==" * 100,
            },
        )
    with pytest.raises(RuminateError):
        workspace.write("create_topic", {"name": "Missing request key"})
    assert app.list_sources() == app.knowledge.list_topics() == []


def test_stdio_invalid_app_action_cannot_mutate_or_read_arbitrary_paths(tmp_path: Path) -> None:
    app = Application(Config(tmp_path / "data", network_mode="off"))
    app.initialize()
    before = logical_snapshot(app)

    async def run() -> None:
        async with asyncio.timeout(30):
            async with Client(parameters(app.config.data_dir)) as client:
                for tool, args in (
                    ("ui_read", {"action": "upload_file", "params": {}}),
                    ("ui_read", {"action": "state", "params": {"path": "/etc/passwd"}}),
                    (
                        "ui_write",
                        {"action": "create_topic", "params": {"name": "No idempotency key"}},
                    ),
                    (
                        "ui_write",
                        {
                            "action": "upload_file",
                            "params": {
                                "filename": "../secret.md",
                                "content_base64": "YQ==",
                                "request_id": "bad",
                            },
                        },
                    ),
                ):
                    assert (await client.call_tool(tool, args)).is_error

    asyncio.run(run())
    assert logical_snapshot(app) == before


def test_default_readonly_server_does_not_expose_app_writes(tmp_path: Path) -> None:
    async def run() -> None:
        async with asyncio.timeout(30):
            async with Client(parameters(tmp_path / "absent", workflow=False)) as client:
                assert len((await client.list_tools()).tools) == 9
                assert (await client.list_resources()).resources == []
                assert (
                    await client.call_tool(
                        "ui_write",
                        {
                            "action": "create_topic",
                            "params": {"name": "No", "request_id": "no"},
                        },
                    )
                ).is_error

    asyncio.run(run())
    assert not (tmp_path / "absent").exists()


def test_ui_bundle_is_self_contained_and_contains_no_store_data() -> None:
    html = workspace_html()
    assert len(html.encode()) < 1024 * 1024
    assert "<script src=" not in html and 'rel="stylesheet"' not in html
    assert "RUMINATE_EMBEDDED = true" in html
    assert ".local/acceptance" not in html and "ruminate.sqlite3" not in html
