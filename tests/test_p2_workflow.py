"""Application/CLI/MCP acceptance for the evidence-to-research workflow."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from ruminate.application import Application
from ruminate.cli import main
from ruminate.config import Config
from ruminate.persistence.database import connect

ROOT = Path(__file__).resolve().parents[1]


def prepare(tmp_path: Path) -> tuple[Application, dict]:
    note = tmp_path / "source.md"
    note.write_text("# Synthetic evaluation\n\nSuccess needs verifiable persisted state.")
    app = Application(Config(tmp_path / "data", "off", (note,)))
    app.initialize()
    imported = app.ingest_file(note, topic="research")
    block = app.get_source(imported["source_id"])["blocks"][1]
    claim = app.knowledge.create_claim(
        topic_id="research",
        statement="State needs verification.",
        claim_kind="source_assertion",
        request_id="claim",
    )
    app.knowledge.link_evidence(
        claim_id=claim["claim_id"],
        block_id=block["block_id"],
        relation="supports",
        request_id="evidence",
    )
    belief = app.knowledge.create_belief(
        topic_id="research",
        text="An agent's own success is enough.",
        provenance="Self-authored test actor, not the real user",
        confirmed=True,
        request_id="belief",
    )
    app.knowledge.link_belief(
        belief_id=belief["belief_id"],
        claim_id=claim["claim_id"],
        relation="challenges",
        justification="Synthetic opposing claim",
        request_id="relation",
    )
    project = app.outputs.create_project(
        topic_id="research", title="Synthetic research note", request_id="project"
    )
    app.reviews.tick()
    return app, {**imported, **project, "claim_id": claim["claim_id"]}


def test_cli_commands_expose_shared_workflow(tmp_path: Path, capsys) -> None:
    app, ids = prepare(tmp_path)

    def cli(*args: str) -> dict:
        assert main(["--data-dir", str(app.config.data_dir), *args]) == 0
        return json.loads(capsys.readouterr().out)

    assert cli("topics", "context", "research")["confirmed_beliefs"]
    assert cli("claims", "get", ids["claim_id"])["evidence"]
    preview = cli("review", "list")
    assert preview["cards"][0]["uncertainty"] == "requires_user_review"
    draft = cli("outputs", "draft", ids["project_id"], "--request-id", "revision")
    assert draft["citation_count"] == 1
    exported = cli("outputs", "export", ids["project_id"], "--format", "md")
    assert "[^e1]" in Path(exported["markdown_path"]).read_text()
    shown = cli("review", "present", "--request-id", "exposure")
    assert shown["shown"] == 1
    cli("review", "feedback", shown["cards"][0]["id"], "postpone", "--request-id", "later")
    assert cli("review", "list")["cards"] == []
    enriched = cli("enrich", ids["source_id"], "--request-id", "local-questions")
    assert enriched["status"] == "enriched"
    assert cli("jobs", "get", enriched["job_id"])["result"]["status"] == "unverified"
    assert cli("jobs", "retry", enriched["job_id"])["attempts"] == 1


def test_cognition_mcp_reads_are_real_protocol_and_do_not_record_exposure(tmp_path: Path) -> None:
    app, ids = prepare(tmp_path)

    def dump() -> str:
        with connect(app.config.database_path, readonly=True) as connection:
            return "\n".join(connection.iterdump())

    before = dump()

    async def inspect() -> None:
        async with asyncio.timeout(20):
            async with Client(
                StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "ruminate", "--data-dir", str(app.config.data_dir), "mcp"],
                    cwd=str(ROOT),
                    env={"RUMINATE_NETWORK_MODE": "off"},
                )
            ) as client:
                context = await client.call_tool("get_topic_context", {"topic_id": "research"})
                assert not context.is_error
                assert context.structured_content["confirmed_beliefs"]
                queue = await client.call_tool("list_review_queue", {"topic_id": "research"})
                assert queue.structured_content["exposure_recorded"] is False
                assert queue.structured_content["cards"][0]["reason"] == "possible_conflict"
                projects = await client.call_tool("list_output_projects", {"topic_id": "research"})
                assert projects.structured_content["projects"][0]["project_id"] == ids["project_id"]
                claim = await client.call_tool("get_claim", {"claim_id": ids["claim_id"]})
                assert claim.structured_content["evidence"][0]["version_id"] == ids["version_id"]
                invalid = await client.call_tool("list_review_queue", {"limit": 100})
                assert invalid.is_error

    asyncio.run(inspect())
    assert dump() == before


def test_cli_rejects_inferred_user_confirmation_and_keeps_store_valid(tmp_path: Path) -> None:
    app, _ = prepare(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruminate",
            "--data-dir",
            str(app.config.data_dir),
            "beliefs",
            "add",
            "--topic",
            "research",
            "--text",
            "Inferred position",
            "--origin",
            "host_model",
            "--provenance",
            "Model inference",
            "--confirm",
            "--request-id",
            "forbidden-confirmation",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]
    assert app.health_check()["status"] == "ok"
    assert len(app.knowledge.topic_context("research")["confirmed_beliefs"]) == 1


def test_cli_backup_restores_cognition_into_new_store(tmp_path: Path, capsys) -> None:
    app, ids = prepare(tmp_path)
    app.outputs.create_draft(ids["project_id"], request_id="revision")
    archive = tmp_path / "workspace.zip"
    assert main(["--data-dir", str(app.config.data_dir), "export-backup", str(archive)]) == 0
    capsys.readouterr()
    target = tmp_path / "restored"
    assert main(["--data-dir", str(target), "import-backup", str(archive)]) == 0
    capsys.readouterr()
    restored = Application(Config(target, "off"))
    assert restored.knowledge.get_claim(ids["claim_id"])["evidence"]
    assert restored.outputs.get_revision(ids["project_id"])["markdown"]
    assert main(["--data-dir", str(target), "import-backup", str(archive)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]
