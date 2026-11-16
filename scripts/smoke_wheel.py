"""Install a built wheel in a fresh environment outside the source checkout.

Dependency installation may access the configured package registry. The installed
application uses disposable local storage to verify health, PDF/Markdown capture,
Chinese retrieval, stored-page rendering, P2 research and backup/restore without
network access or model credentials. All user positions and visual notes are
explicit synthetic installation inputs, not actual user beliefs or model output.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

P2_SMOKE = dedent("""\
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import ruminate
from ruminate.application import Application
from ruminate.config import Config

inputs = json.load(sys.stdin)
workspace = Path(inputs["workspace"])
if not Path(ruminate.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise RuntimeError("P2 smoke accidentally imported Ruminate outside the installed environment")
if os.environ.get("OPENAI_API_KEY"):
    raise RuntimeError("Installation smoke must not require model credentials")
config = Config(workspace / "data", network_mode="off", allowed_roots=(workspace,))
app = Application(config)
topic = app.knowledge.create_topic(
    slug="wheel-research", name="Synthetic installation research", request_id="wheel-topic"
)
native = [app.get_source(inputs["captures"][name]["source_id"])
          for name in ("single_column.pdf", "notes.md")]
selected = [native[0]["blocks"][0], native[0]["blocks"][-1], native[1]["blocks"][1]]
claims = []
for index, block in enumerate(selected):
    quote = block["text"][:1000]
    claim = app.knowledge.create_claim(
        topic_id=topic["topic_id"], statement=quote, claim_kind="source_assertion",
        origin="human", request_id=f"wheel-claim-{index}"
    )
    app.knowledge.link_evidence(
        claim_id=claim["claim_id"], block_id=block["block_id"], relation="supports",
        start_char=0, end_char=len(quote), request_id=f"wheel-evidence-{index}"
    )
    claims.append(claim["claim_id"])
hypothesis = app.knowledge.create_claim(
    topic_id=topic["topic_id"], statement="Synthetic untested research hypothesis.",
    claim_kind="hypothesis", origin="host_model", request_id="wheel-hypothesis"
)
candidate = app.knowledge.create_belief(
    topic_id=topic["topic_id"], text="Synthetic unconfirmed belief candidate.",
    provenance="Installation test input", origin="host_model", request_id="wheel-candidate"
)
confirmed = app.knowledge.create_belief(
    topic_id=topic["topic_id"], text="Synthetic prior position supplied for this test.",
    provenance="Explicit synthetic confirmation in installation smoke; not a real user belief",
    confirmed=True, origin="human", request_id="wheel-belief"
)
app.knowledge.link_belief(
    belief_id=confirmed["belief_id"], claim_id=claims[0], relation="challenges",
    justification="Explicit test relation; no semantic contradiction classifier is asserted.",
    request_id="wheel-belief-link"
)
project = app.outputs.create_project(
    topic["topic_id"], "Installed research proposal", kind="research_proposal",
    target_audience="Installation reviewer", goal="Verify durable citations",
    request_id="wheel-project"
)
dry = app.reviews.tick(topic["topic_id"], dry_run=True)
if app.reviews.list_queue(topic["topic_id"]):
    raise RuntimeError("Dry run persisted review candidates")
tick = app.reviews.tick(topic["topic_id"])
cards = app.reviews.list_queue(topic["topic_id"])
conflicts = [card for card in cards if card["reason"] == "possible_conflict"]
if not conflicts or not conflicts[0]["challenges"] or tick["notifications_sent"] != 0:
    raise RuntimeError("Installed wheel did not create an evidence-backed review candidate")
shown = app.reviews.present(topic["topic_id"], request_id="wheel-review-show")
if not 1 <= shown["shown"] <= 3:
    raise RuntimeError("Installed review exposure count is invalid")
card = conflicts[0]
feedback = app.reviews.feedback(card["id"], "postpone", request_id="wheel-review-postpone")
if card["id"] in {item["id"] for item in app.reviews.list_queue(topic["topic_id"])}:
    raise RuntimeError("Postponed review reappeared immediately")
created = app.outputs.create_draft(
    project["project_id"], claim_ids=[*claims, hypothesis["claim_id"]], request_id="wheel-draft"
)
draft = app.outputs.get_revision(project["project_id"])
exported = app.outputs.export(project["project_id"])
citations = [evidence for claim in draft["snapshot"]["claims"] for evidence in claim["evidence"]]
if len(citations) != 3 or len({citation["source_id"] for citation in citations}) != 2:
    raise RuntimeError("Installed draft did not preserve all multi-source citations")
for citation in citations:
    source = app.get_source(citation["source_id"], version_id=citation["version_id"])
    block = next(block for block in source["blocks"] if block["block_id"] == citation["block_id"])
    if block["text"][citation["start_char"]:citation["end_char"]] != citation["quote"]:
        raise RuntimeError("Installed draft citation does not match its persisted source")
if not Path(exported["markdown_path"]).is_file() or not Path(exported["sidecar_path"]).is_file():
    raise RuntimeError("Installed wheel did not export Markdown and evidence sidecar")
before_claims = len(app.knowledge.list_claims(topic["topic_id"]))
enriched = asyncio.run(app.enrichment.start(native[0]["source_id"], request_id="wheel-no-llm"))
if (enriched["status"] != "enriched" or enriched["provider"] != "no_llm"
        or enriched["result"]["status"] != "unverified"
        or len(app.knowledge.list_claims(topic["topic_id"])) != before_claims):
    raise RuntimeError("No-LLM proposals were not kept separate from curated knowledge")
# Advance a cited source after drafting, then ensure backup preserves the old revision.
(workspace / "notes.md").write_text("# Updated installation note\\n\\nNew material after drafting.")
updated = app.ingest_file(workspace / "notes.md")
if updated["version_id"] == native[1]["version_id"]:
    raise RuntimeError("Source update did not create a new version")
archive = workspace / "research-backup.zip"
backup_result = app.backups.export_backup(archive)
restored = Application(Config(workspace / "restored", network_mode="off"))
restore_result = restored.backups.import_backup(archive)
restored_draft = restored.outputs.get_revision(project["project_id"])
if restored.health_check()["status"] != "ok" or restored_draft["markdown"] != draft["markdown"]:
    raise RuntimeError("Restored installed store lost the original output revision")
for citation in restored_draft["snapshot"]["claims"]:
    for evidence in citation["evidence"]:
        source = restored.get_source(evidence["source_id"], version_id=evidence["version_id"])
        block = next(item for item in source["blocks"]
                     if item["block_id"] == evidence["block_id"])
        if (block["text"][evidence["start_char"]:evidence["end_char"]] != evidence["quote"]
                or block["locator"] != evidence["locator"]):
            raise RuntimeError("Restored citation could not be replayed exactly")
restored.reviews.tick(topic["topic_id"])
if card["id"] in {item["id"] for item in restored.reviews.list_queue(topic["topic_id"])}:
    raise RuntimeError("Restored review forgot its postpone feedback")
context = restored.knowledge.topic_context(topic["topic_id"])
confirmed_ids = [belief["belief_id"] for belief in context["confirmed_beliefs"]]
candidate_ids = [belief["belief_id"] for belief in context["belief_candidates"]]
if confirmed_ids != [confirmed["belief_id"]] or candidate_ids != [candidate["belief_id"]]:
    raise RuntimeError("Restored store mixed confirmed and candidate beliefs")
restored_visual = restored.list_visual_observations(
    inputs["captures"]["vision_scan.pdf"]["source_id"]
)
if len(restored_visual) != 1 or restored_visual[0]["status"] != "unverified":
    raise RuntimeError("Restored store lost the separate visual observation")
print(json.dumps({
    "import_from_installed_environment": True,
    "synthetic_inputs_only": True,
    "claim_count": created["claim_count"],
    "citation_count": len(citations),
    "distinct_cited_sources": len({citation["source_id"] for citation in citations}),
    "citation_spans_verified": True,
    "unconfirmed_belief_remains_candidate": True,
    "possible_conflict": card["reason"],
    "conflict_relation_origin": "explicit synthetic relation, not semantic inference",
    "dry_run_would_create": dry["would_create"],
    "review_notifications_sent": tick["notifications_sent"],
    "review_feedback": feedback["status"],
    "postponed_review_absent_after_restore": True,
    "output_review_status": created["review_status"],
    "markdown_sha256": hashlib.sha256(draft["markdown"].encode()).hexdigest(),
    "no_llm_job_status": enriched["status"],
    "no_llm_proposals_unverified": True,
    "backup_source_blobs": backup_result["source_blobs"],
    "backup_output_revisions": backup_result["output_revisions"],
    "restore_status": restore_result["status"],
    "historical_citations_replayed_after_restore": True,
    "visual_observation_restored": True,
}))
""")

P3_SMOKE = dedent("""\
import asyncio
import base64
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import ruminate
from mcp import Client, StdioServerParameters
from ruminate.config import Config
from ruminate.web_server import create_server

workspace = Path(json.load(sys.stdin)["workspace"])
if not Path(ruminate.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise RuntimeError("GUI smoke imported source checkout")
server = create_server(Config(workspace / "gui-data", network_mode="off"), port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = server.url.rstrip("/")

def request(path, payload=None, token=None):
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "Origin": base}
        if token:
            headers["X-Ruminate-Token"] = token
    req = urllib.request.Request(base + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read()

try:
    html = request("/").decode()
    if ("Ruminate" not in html or not request("/app.js") or not request("/style.css")
            or not request("/transport.js")):
        raise RuntimeError("Installed GUI assets missing")
    token = json.loads(request("/api/session"))["token"]
    topic_payload = {"slug": "wheel-gui", "name": "Installed GUI smoke", "request_id": "gui-topic"}
    try:
        request("/api/topics", topic_payload)
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
    else:
        raise RuntimeError("GUI mutation accepted without session token")
    topic = json.loads(request("/api/topics", topic_payload, token))
    encoded_note = base64.b64encode(b"# GUI installation\\n\\nPersisted evidence.").decode()
    upload = {"filename": "gui-note.md", "topic": topic["topic_id"], "request_id": "gui-upload",
              "content_base64": encoded_note}
    first = json.loads(request("/api/ingest/file", upload, token))
    second = json.loads(request("/api/ingest/file", upload, token))
    if first["source_id"] != second["source_id"] or first["job_id"] != second["job_id"]:
        raise RuntimeError("Installed GUI upload retry duplicated records")
    source = json.loads(request("/api/sources/" + first["source_id"]))
    if not source["blocks"]:
        raise RuntimeError("Installed GUI source reader is empty")
    async def mcp_ui():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ruminate", "--data-dir", str(workspace / "gui-data"), "mcp"],
            env={"RUMINATE_MCP_WRITES": "1", "RUMINATE_NETWORK_MODE": "off"},
            cwd=str(workspace),
        )
        async with Client(parameters) as client:
            tool = next(item for item in (await client.list_tools()).tools
                        if item.name == "open_workspace")
            meta = tool.model_dump(by_alias=True)["_meta"]
            resource = await client.read_resource(meta["ui"]["resourceUri"])
            content = resource.contents[0]
            if (content.mime_type != "text/html;profile=mcp-app"
                    or 'window.RUMINATE_EMBEDDED = true' not in content.text
                    or '<script src=' in content.text):
                raise RuntimeError("Installed MCP UI is not a self-contained resource")
            result = await client.call_tool("open_workspace", {})
            if result.is_error or len(result.structured_content["sources"]) != 1:
                raise RuntimeError("Installed MCP UI did not read the uploaded source")
            return len(content.text.encode())
    resource_bytes = asyncio.run(mcp_ui())
    print(json.dumps({"packaged_assets_served": True, "session_token_required": True,
                      "upload_persisted": True, "upload_retry_same_job": True,
                      "readable_blocks": len(source["blocks"]), "external_network": False,
                      "installed_mcp_ui_resource_bytes": resource_bytes}))
finally:
    server.shutdown()
    server.server_close()
    thread.join(5)
""")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--uv", default=shutil.which("uv"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--install-timeout", type=float, default=300,
                        help="Seconds allowed for dependency installation (default: 300)")
    args = parser.parse_args()
    if not args.uv:
        parser.error("uv is not on PATH; pass --uv /absolute/path/to/uv")
    if args.install_timeout <= 0:
        parser.error("--install-timeout must be positive")
    wheel = args.wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="ruminate-wheel-") as directory:
        workspace = Path(directory)
        environment = workspace / "venv"
        subprocess.run(
            [args.uv, "venv", str(environment), "--python", sys.executable],
            check=True,
            timeout=30,
        )
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(
            [args.uv, "pip", "install", "--python", str(python), str(wheel)],
            check=True,
            timeout=args.install_timeout,
        )
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("RUMINATE_")
            and key not in {"PYTHONPATH", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
        }
        clean_env["RUMINATE_NETWORK_MODE"] = "off"
        run = subprocess.run(
            [str(python), "-m", "ruminate", "--data-dir", str(workspace / "data"), "init"],
            cwd=workspace,
            env=clean_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        health = json.loads(run.stdout)
        if health["status"] != "ok":
            raise RuntimeError("Installed wheel did not initialize successfully")
        results = []
        captures = {}
        fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
        for name in ("single_column.pdf", "notes.md", "vision_scan.pdf"):
            selected = workspace / name
            selected.write_bytes((fixtures / name).read_bytes())
            captured = subprocess.run(
                [
                    str(python),
                    "-m",
                    "ruminate",
                    "--data-dir",
                    str(workspace / "data"),
                    "ingest",
                    str(selected),
                ],
                cwd=workspace,
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            expected_status = "needs_ocr" if name == "vision_scan.pdf" else "indexed"
            expected_exit = 1 if expected_status == "needs_ocr" else 0
            if captured.returncode != expected_exit:
                raise RuntimeError(f"Installed wheel capture failed: {captured.stderr}")
            result = json.loads(captured.stdout)
            if result["status"] != expected_status:
                raise RuntimeError("Installed wheel could not capture a fixture")
            captures[name] = result
            results.append(
                {"fixture": name, "status": result["status"], "block_count": result["block_count"]}
            )
        searched = subprocess.run(
            [
                str(python),
                "-m",
                "ruminate",
                "--data-dir",
                str(workspace / "data"),
                "search",
                "证据",
            ],
            cwd=workspace,
            env=clean_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        hits = json.loads(searched.stdout)["results"]
        if not hits:
            raise RuntimeError("Installed wheel could not retrieve Chinese evidence")

        def cli(*arguments: str) -> dict:
            completed = subprocess.run(
                [
                    str(python),
                    "-m",
                    "ruminate",
                    "--data-dir",
                    str(workspace / "data"),
                    *arguments,
                ],
                cwd=workspace,
                env=clean_env,
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            )
            return json.loads(completed.stdout)

        scan = captures["vision_scan.pdf"]
        if scan["block_count"] != 0:
            raise RuntimeError("Vision fixture unexpectedly contains native text")
        # Removing the submitted file proves that rendering reads the immutable
        # captured bytes, not the original path or the checked-out fixture.
        (workspace / "vision_scan.pdf").unlink()
        output = workspace / "stored-page.png"
        rendered = cli(
            "sources",
            "page-image",
            scan["source_id"],
            "--version-id",
            scan["version_id"],
            "--page",
            "1",
            "--output",
            str(output),
        )
        decoded = subprocess.run(
            [
                str(python),
                "-c",
                "import json,sys; from PIL import Image; "
                "image=Image.open(sys.argv[1]); "
                "metadata={'format':image.format,'width':image.width,'height':image.height}; "
                "image.verify(); print(json.dumps(metadata))",
                str(output),
            ],
            cwd=workspace,
            env=clean_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        image = json.loads(decoded.stdout)
        if (
            image["format"] != "PNG"
            or image["width"] != rendered["width"]
            or image["height"] != rendered["height"]
            or rendered["image_sha256"] != hashlib.sha256(output.read_bytes()).hexdigest()
            or rendered["version_id"] != scan["version_id"]
            or rendered["page"] != 1
        ):
            raise RuntimeError("Installed wheel rendered invalid image or citation metadata")
        observation_text = "Wheelvisualcanary: synthetic visual note, not an actual model result."
        text_file = workspace / "visual-note.txt"
        text_file.write_text(observation_text, encoding="utf-8")
        observed = cli(
            "observations",
            "add",
            scan["source_id"],
            "--version-id",
            scan["version_id"],
            "--page",
            "1",
            "--kind",
            "visual_interpretation",
            "--text-file",
            str(text_file),
            "--host",
            "installation-smoke (synthetic)",
            "--request-id",
            "wheel-visual-1",
        )
        listed = cli(
            "observations",
            "list",
            scan["source_id"],
            "--version-id",
            scan["version_id"],
        )["observations"]
        if (
            observed["status"] != "unverified"
            or len(listed) != 1
            or listed[0]["observation_id"] != observed["observation_id"]
            or listed[0]["status"] != "unverified"
            or listed[0]["text"] != observation_text
            or listed[0]["source_sha256"] != rendered["source_sha256"]
            or cli("search", "Wheelvisualcanary")["results"]
        ):
            raise RuntimeError("Installed wheel did not preserve a separate unverified visual note")
        cognition = subprocess.run(
            [str(python), "-I", "-c", P2_SMOKE],
            input=json.dumps({"workspace": str(workspace), "captures": captures}),
            cwd=workspace,
            env=clean_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if cognition.returncode:
            raise RuntimeError(f"Installed P2 workflow failed: {cognition.stderr[-4000:]}")
        p2_workflow = json.loads(cognition.stdout)
        gui = subprocess.run(
            [str(python), "-I", "-c", P3_SMOKE],
            input=json.dumps({"workspace": str(workspace)}),
            cwd=workspace,
            env=clean_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if gui.returncode:
            raise RuntimeError(f"Installed GUI workflow failed: {gui.stderr[-4000:]}")
        report = {
            "artifact": wheel.name,
            "install": "clean virtual environment; registry dependencies downloaded as needed",
            "working_directory": "temporary directory outside source checkout",
            "health_check": health,
            "imports": results,
            "chinese_search_hits": len(hits),
            "visual_workflow": {
                "source_status": scan["status"],
                "native_blocks": scan["block_count"],
                "rendered_after_original_removed": True,
                "page": rendered["page"],
                "image": image,
                "image_sha256_verified": True,
                "renderer": rendered["renderer"],
                "observation_kind": observed["kind"],
                "observation_status": observed["status"],
                "listed_observations": len(listed),
                "observation_searches_as_native_evidence": False,
                "note_origin": "synthetic installation test input, not actual model output",
            },
            "p2_workflow": p2_workflow,
            "gui_workflow": json.loads(gui.stdout),
        }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
