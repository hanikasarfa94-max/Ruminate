"""Exercise packaged skills and opt-in writes in an ephemeral real Codex session."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smoke_codex import sanitize

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import connect

ROOT = Path(__file__).resolve().parents[1]


def run(timeout: float) -> dict[str, Any]:
    report: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "BLOCKED",
        "workflow": "packaged-skills-and-local-mcp-writes",
        "client": "codex-cli",
        "configuration": "ephemeral CLI overrides and temporary project skills",
        "fixture": "self-authored note and research question; no user documents",
    }
    codex = shutil.which("codex")
    if codex is None:
        report["reason"] = "codex executable not found on PATH"
        return report
    report["client_version"] = subprocess.run(
        [codex, "--version"], capture_output=True, text=True, timeout=10, check=True
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="ruminate-p3-codex-") as directory:
        workspace = Path(directory)
        skills = workspace / ".agents" / "skills"
        shutil.copytree(ROOT / "plugins" / "ruminate" / "skills", skills)
        source = workspace / "synthetic-research.md"
        source.write_text(
            "# Synthetic workflow study\n\n"
            "The Orchidquartz pilot checked saved state for every completed task.\n\n"
            "This self-authored fixture describes no real-world study.\n",
            encoding="utf-8",
        )
        app = Application(Config(workspace / "data", "off", (source,)))
        app.initialize()
        topic = app.knowledge.create_topic(
            slug="synthetic", name="Synthetic workflow research", request_id="seed-topic"
        )
        app.knowledge.create_claim(
            topic_id=topic["topic_id"],
            statement="How should a task's saved state be checked?",
            claim_kind="question",
            origin="deterministic",
            request_id="seed-question",
        )
        app.reviews.tick(topic["topic_id"])
        prompt = (
            "This is a synthetic integration exercise. Use the project skills $capture, "
            "$research, $compose and $ruminate. Read their SKILL.md instructions first. "
            "Import the file " + str(source) + " into existing topic synthetic, inspect its "
            "native text, and save one source_assertion with an exact evidence span about "
            "the Orchidquartz pilot. Create a project titled 'Synthetic cited outline', "
            "generate a local draft using that source_assertion and read the resulting revision. "
            "Then present one existing review card. For this test I explicitly choose "
            "'postpone for 7 days' for that card; record that feedback. These local writes are "
            "requested. Do not create beliefs or invent actual research results. All reads and "
            "writes of application data must use Ruminate MCP; shell is only for reading these "
            "four skill files. No external network or other tools. Report the created project "
            "and the evidence location. Stop after this one capture/claim/draft/feedback flow."
        )
        server_args = [
            "-m",
            "ruminate",
            "--data-dir",
            str(app.config.data_dir),
            "--allow-root",
            str(source),
            "mcp",
        ]
        command = [
            codex,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--cd",
            str(workspace),
            "--json",
            "--color",
            "never",
            "-c",
            "mcp_servers.ruminate.command=" + json.dumps(sys.executable),
            "-c",
            "mcp_servers.ruminate.args=" + json.dumps(server_args),
            "-c",
            "mcp_servers.ruminate.required=true",
            "-c",
            'mcp_servers.ruminate.env={RUMINATE_NETWORK_MODE="off",RUMINATE_MCP_WRITES="1"}',
            prompt,
        ]
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, _stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, _stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, _stderr = process.communicate(timeout=5)
        events = []
        for line in stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        calls = []
        commands = []
        response = []
        for event in events:
            if event.get("type") != "item.completed":
                continue
            item = event.get("item", {})
            if item.get("type") == "mcp_tool_call":
                calls.append(
                    {
                        "server": item.get("server"),
                        "tool": item.get("tool"),
                        "status": item.get("status"),
                    }
                )
            elif item.get("type") == "command_execution":
                commands.append(item.get("command", ""))
            elif item.get("type") == "agent_message":
                response.append(str(item.get("text", "")))
        skill_reads = sorted(
            name
            for name in ("capture", "research", "compose", "ruminate")
            if any(f"{name}/SKILL.md" in command for command in commands)
        )
        required = {
            "ingest_local_file",
            "get_source",
            "record_claim",
            "create_output_project",
            "create_output_draft",
            "get_output_revision",
            "present_review_queue",
            "record_review_feedback",
        }
        completed = {call["tool"] for call in calls if call["status"] == "completed"}
        projects = app.outputs.list_projects(topic_id=topic["topic_id"])
        checks: dict[str, Any] = {
            "required_tools_completed": sorted(required & completed),
            "skill_files_read": skill_reads,
            "project_count": len(projects),
        }
        with connect(app.config.database_path, readonly=True) as connection:
            checks["beliefs_created"] = connection.execute(
                "SELECT COUNT(*) FROM user_beliefs"
            ).fetchone()[0]
            checks["feedback_recorded"] = connection.execute(
                "SELECT COUNT(*) FROM review_events WHERE action='feedback' AND feedback='postpone'"
            ).fetchone()[0]
        citation_matches = False
        if projects:
            try:
                revision = app.outputs.get_revision(projects[0]["project_id"])
                cited = [
                    ref for claim in revision["snapshot"]["claims"] for ref in claim["evidence"]
                ]
                citation_matches = bool(cited) and all(
                    ref["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
                    for ref in cited
                )
                checks["citation_count"] = len(cited)
            except (KeyError, RuminateError):
                citation_matches = False
        checks["citations_match_imported_fixture"] = citation_matches
        report.update(
            tool_calls=calls,
            exit_code=process.returncode,
            timed_out=timed_out,
            checks=checks,
            synthetic_response="\n".join(response).replace(directory, "<temporary-workspace>")[
                :6000
            ],
        )
        if process.returncode != 0:
            report["runtime_error"] = sanitize(_stderr, directory)
        if (
            process.returncode == 0
            and not timed_out
            and required <= completed
            and len(skill_reads) == 4
            and citation_matches
            and checks["beliefs_created"] == 0
            and checks["feedback_recorded"] == 1
        ):
            report["status"] = "SUPPORTED"
        else:
            report["reason"] = "One or more workflow checks did not complete; inspect checks"
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.timeout)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if result["status"] == "SUPPORTED" else 1)
