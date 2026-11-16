"""Real app/CLI/MCP page-image bridge; model quality is checked by a separate host smoke."""

import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import ImageContent
from PIL import Image

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def make_app(tmp_path: Path) -> tuple[Application, dict]:
    app = Application(Config(tmp_path / "store", network_mode="off", allowed_roots=(FIXTURES,)))
    app.initialize()
    result = app.ingest_file(FIXTURES / "single_column.pdf")
    return app, result


def dump(app: Application) -> str:
    with connect(app.config.database_path, readonly=True) as connection:
        return "\n".join(connection.iterdump())


def test_mcp_page_image_is_actual_png_and_readonly(tmp_path: Path) -> None:
    app, source = make_app(tmp_path)
    before = dump(app)

    async def inspect() -> None:
        async with asyncio.timeout(30):
            async with Client(
                StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "ruminate", "--data-dir", str(app.config.data_dir), "mcp"],
                    cwd=str(ROOT),
                    env={"PYTHONDONTWRITEBYTECODE": "1", "RUMINATE_NETWORK_MODE": "off"},
                )
            ) as client:
                result = await client.call_tool(
                    "get_source_page_image",
                    {
                        "source_id": source["source_id"],
                        "version_id": source["version_id"],
                        "page": 1,
                    },
                )
                assert not result.is_error
                metadata = result.structured_content
                assert metadata is not None
                images = [item for item in result.content if isinstance(item, ImageContent)]
                assert len(images) == 1 and images[0].mime_type == "image/png"
                png = base64.b64decode(images[0].data, validate=True)
                with Image.open(io.BytesIO(png)) as image:
                    assert image.format == "PNG"
                    assert image.size == (metadata["width"], metadata["height"])
                assert metadata["image_sha256"] == hashlib.sha256(png).hexdigest()
                assert metadata["version_id"] == source["version_id"]
                assert metadata["locator"] == {"type": "pdf_page", "page": 1}
                invalid = await client.call_tool(
                    "get_source_page_image",
                    {
                        "source_id": source["source_id"],
                        "page": 99,
                    },
                )
                assert invalid.is_error
                observations = await client.call_tool(
                    "list_visual_observations",
                    {
                        "source_id": source["source_id"],
                    },
                )
                assert not observations.is_error
                assert observations.structured_content == {"observations": []}

    asyncio.run(inspect())
    assert dump(app) == before


def test_observation_cannot_replace_native_evidence_or_invent_page(tmp_path: Path) -> None:
    app, source = make_app(tmp_path)
    before = app.get_source(source["source_id"])
    args = dict(
        source_id=source["source_id"],
        version_id=source["version_id"],
        page=1,
        kind="visual_interpretation",
        text="Nebulapeach interpretation.",
        host="codex (caller supplied)",
        request_id="visual-1",
    )
    added = app.add_visual_observation(**args)
    assert added["status"] == "unverified"
    assert app.add_visual_observation(**args)["observation_id"] == added["observation_id"]
    assert app.get_source(source["source_id"]) == before
    assert app.search_sources("Nebulapeach") == []
    assert len(app.list_visual_observations(source["source_id"])) == 1
    with pytest.raises(RuminateError):
        app.add_visual_observation(**{**args, "page": 99, "request_id": "impossible"})
    assert len(app.list_visual_observations(source["source_id"])) == 1


def test_cli_exports_new_image_and_saves_utf8_observation(tmp_path: Path) -> None:
    app, source = make_app(tmp_path)
    env = {**os.environ, "RUMINATE_NETWORK_MODE": "off", "RUMINATE_ALLOWED_ROOTS": ""}

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ruminate", "--data-dir", str(app.config.data_dir), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    output = tmp_path / "page.png"
    image_args = (
        "sources",
        "page-image",
        source["source_id"],
        "--version-id",
        source["version_id"],
        "--page",
        "1",
        "--output",
        str(output),
    )
    rendered = cli(*image_args)
    assert rendered.returncode == 0, rendered.stderr
    before = output.read_bytes()
    assert before.startswith(b"\x89PNG")
    assert cli(*image_args).returncode == 2
    assert output.read_bytes() == before
    cyclic_parent = tmp_path / "cyclic"
    cyclic_parent.symlink_to(cyclic_parent)
    cyclic = cli(*image_args[:-1], str(cyclic_parent / "page.png"))
    assert cyclic.returncode == 2
    assert json.loads(cyclic.stderr)["error"]["code"] == "storage_error"
    text_file = tmp_path / "observation.txt"
    text_file.write_text("待核对的视觉转写。", encoding="utf-8")
    saved = cli(
        "observations",
        "add",
        source["source_id"],
        "--version-id",
        source["version_id"],
        "--page",
        "1",
        "--kind",
        "visual_transcription",
        "--text-file",
        str(text_file),
        "--host",
        "codex",
        "--request-id",
        "cli-note-1",
    )
    assert saved.returncode == 0, saved.stderr
    assert json.loads(saved.stdout)["status"] == "unverified"
    listed = cli("observations", "list", source["source_id"])
    assert json.loads(listed.stdout)["observations"][0]["text"] == "待核对的视觉转写。"


def test_page_access_checks_version_identity_and_blob_integrity(tmp_path: Path) -> None:
    app, source = make_app(tmp_path)
    note = app.ingest_file(FIXTURES / "notes.md")
    with pytest.raises(RuminateError) as mismatch:
        app.get_source_page_image(source["source_id"], version_id=note["version_id"])
    assert mismatch.value.code == "version_not_found"
    with pytest.raises(RuminateError) as not_pdf:
        app.get_source_page_image(note["source_id"])
    assert not_pdf.value.code == "not_pdf"
    details = app.get_source(source["source_id"])
    (app.config.data_dir / details["blob_path"]).write_bytes(b"changed source")
    with pytest.raises(RuminateError) as corrupt:
        app.get_source_page_image(source["source_id"])
    assert corrupt.value.code == "blob_corrupt"
