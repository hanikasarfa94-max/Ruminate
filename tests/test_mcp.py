"""Protocol tests run an actual Ruminate child process; no MCP mocks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from ruminate.application import Application
from ruminate.config import Config
from ruminate.persistence.database import connect

ROOT = Path(__file__).resolve().parents[1]


def server_parameters(data_dir: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "ruminate", "--data-dir", str(data_dir), "mcp", "--transport", "stdio"],
        cwd=str(ROOT),
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )


def snapshot(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


async def inspect_health(data_dir: Path, initialized: bool) -> None:
    async with asyncio.timeout(20):
        async with Client(server_parameters(data_dir)) as client:
            assert client.server_info is not None
            assert client.server_info.name == "Ruminate"
            assert client.protocol_version
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {
                "health_check",
                "search_sources",
                "get_source",
                "get_source_page_image",
                "list_visual_observations",
                "get_claim",
                "list_review_queue",
                "get_topic_context",
                "list_output_projects",
            }
            health = next(tool for tool in listed.tools if tool.name == "health_check")
            assert health.annotations is not None
            assert health.annotations.read_only_hint is True
            assert health.annotations.open_world_hint is False
            assert health.input_schema["type"] == "object"
            result = await client.call_tool("health_check", {})
            assert not result.is_error
            assert result.structured_content is not None
            assert result.structured_content["initialized"] is initialized
            missing = await client.call_tool("not_a_real_tool", {})
            assert missing.is_error


def test_stdio_health_before_init_has_no_persistent_side_effects(tmp_path: Path) -> None:
    data_dir = tmp_path / "not-created"
    asyncio.run(inspect_health(data_dir, initialized=False))
    assert not data_dir.exists()


def test_stdio_health_after_init_is_readonly(tmp_path: Path) -> None:
    data_dir = tmp_path / "store"
    subprocess.run(
        [sys.executable, "-m", "ruminate", "--data-dir", str(data_dir), "init"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    before = snapshot(data_dir)
    assert before
    asyncio.run(inspect_health(data_dir, initialized=True))
    assert snapshot(data_dir) == before


async def inspect_wire(data_dir: Path) -> None:
    """Validate every stdout line, including shutdown, as a JSON-RPC envelope."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ruminate",
        "--data-dir",
        str(data_dir),
        "mcp",
        "--transport",
        "stdio",
        cwd=ROOT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    async def send(payload: dict[str, object]) -> None:
        assert process.stdin is not None
        process.stdin.write((json.dumps(payload) + "\n").encode())
        await process.stdin.drain()

    async def response(request_id: int) -> dict[str, object]:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            payload = json.loads(line)
            assert payload.get("jsonrpc") == "2.0", line
            if payload.get("id") == request_id:
                assert isinstance(payload, dict)
                return payload
        raise AssertionError("MCP server closed stdout before responding")

    try:
        async with asyncio.timeout(20):
            await send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "ruminate-wire-test", "version": "1"},
                    },
                }
            )
            assert "result" in await response(1)
            await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            assert "result" in await response(2)
            await send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "health_check", "arguments": {}},
                }
            )
            assert "result" in await response(3)
            process.stdin.close()
            while line := await process.stdout.readline():
                assert json.loads(line).get("jsonrpc") == "2.0", line
            assert await process.wait() == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def test_stdout_contains_only_jsonrpc_and_legacy_handshake_works(tmp_path: Path) -> None:
    asyncio.run(inspect_wire(tmp_path / "not-created"))
    assert not (tmp_path / "not-created").exists()


def test_stdio_search_and_historical_evidence(tmp_path: Path) -> None:
    source = tmp_path / "evidence.md"
    source.write_text("# Agent research\n\n认知反刍 requires traceable evidence.")
    app = Application(Config(tmp_path / "data", "off", (source,)))
    app.initialize()
    original = app.ingest_file(source)
    source.write_text("# Updated research\n\nA revised experiment with new evidence.")
    latest = app.ingest_file(source)

    def evidence_files() -> dict[str, str]:
        # SQLite read-only WAL connections may create coordination files.
        # Verify the actual store and blobs rather than requiring an absent cache.
        return {
            name: digest
            for name, digest in snapshot(app.config.data_dir).items()
            if name not in {"ruminate.sqlite3-wal", "ruminate.sqlite3-shm"}
        }

    def logical_dump() -> str:
        with connect(app.config.database_path, readonly=True) as connection:
            return "\n".join(connection.iterdump())

    before = evidence_files()
    before_records = logical_dump()

    async def inspect() -> None:
        async with asyncio.timeout(20):
            async with Client(server_parameters(app.config.data_dir)) as client:
                found = await client.call_tool("search_sources", {"query": "revised experiment"})
                assert not found.is_error
                assert found.structured_content is not None
                hits = found.structured_content["results"]
                assert hits[0]["version_id"] == latest["version_id"]
                assert hits[0]["block_id"] and hits[0]["locator"]
                historical = await client.call_tool(
                    "get_source",
                    {
                        "source_id": original["source_id"],
                        "version_id": original["version_id"],
                    },
                )
                assert not historical.is_error
                assert historical.structured_content is not None
                assert any(
                    "认知反刍" in block["text"] for block in historical.structured_content["blocks"]
                )
                invalid = await client.call_tool(
                    "search_sources", {"query": "evidence", "limit": 1000}
                )
                assert invalid.is_error
                assert any("invalid_limit" in str(part) for part in invalid.content)

    asyncio.run(inspect())
    assert evidence_files() == before
    assert logical_dump() == before_records
