"""Offline P2 acceptance using self-authored documents and synthetic user statements."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ruminate.application import Application
from ruminate.config import Config
from ruminate.persistence.database import connect


def seed_cognition(workspace: Path) -> tuple[Application, dict[str, Any]]:
    """Reusable disposable setup; confirmations are fixture data, not the real user's beliefs."""
    inputs = workspace / "synthetic-inputs"
    inputs.mkdir()
    files = [inputs / "early-pilot.md", inputs / "later-pilot.md", inputs / "protocol.pdf"]
    files[0].write_text(
        "# Synthetic early pilot\n\n"
        "Synthetic pilot A: all sampled tasks required human verification.\n",
        encoding="utf-8",
    )
    files[1].write_text(
        "# Synthetic later pilot\n\n"
        "Topazmarigold synthetic pilot B: some sampled tasks completed "
        "without human verification.\n",
        encoding="utf-8",
    )
    fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    files[2].write_bytes((fixtures / "single_column.pdf").read_bytes())
    app = Application(Config(workspace / "data", "off", (inputs,)))
    app.initialize()
    topic = app.knowledge.create_topic(
        slug="synthetic-p2", name="Synthetic P2 research", request_id="seed-topic"
    )
    claims, imports = [], []
    for index, path in enumerate(files):
        imported = app.ingest_file(path, topic=topic["topic_id"])
        if imported["status"] != "indexed":
            raise RuntimeError("Synthetic document import failed")
        imports.append(imported)
        source = app.get_source(imported["source_id"])
        block = source["blocks"][-1]
        claim = app.knowledge.create_claim(
            topic_id=topic["topic_id"],
            statement=block["text"],
            claim_kind="source_assertion",
            origin="human",
            request_id=f"seed-claim-{index}",
        )
        app.knowledge.link_evidence(
            claim_id=claim["claim_id"],
            block_id=block["block_id"],
            relation="supports",
            request_id=f"seed-evidence-{index}",
        )
        claims.append(claim["claim_id"])
    belief = app.knowledge.create_belief(
        topic_id=topic["topic_id"],
        text="Every task always requires human verification.",
        provenance="Explicit statement in synthetic acceptance fixture; not a real user belief.",
        confirmed=True,
        origin="human",
        request_id="seed-belief",
    )
    for index, relation in ((0, "supports"), (1, "challenges")):
        app.knowledge.link_belief(
            belief_id=belief["belief_id"],
            claim_id=claims[index],
            relation=relation,
            justification="Synthetic fixture supplied this relation; human review is necessary.",
            request_id=f"seed-belief-link-{index}",
        )
    candidate = app.knowledge.create_belief(
        topic_id=topic["topic_id"],
        text="A host might infer a preference for automation.",
        provenance="Synthetic model conjecture, deliberately not confirmed.",
        origin="host_model",
        confirmed=False,
        request_id="seed-candidate",
    )
    inference = app.knowledge.create_claim(
        topic_id=topic["topic_id"],
        statement="A hypothetical mechanism could explain this difference.",
        claim_kind="model_inference",
        origin="host_model",
        request_id="seed-inference",
    )
    project = app.outputs.create_project(
        topic["topic_id"],
        "Synthetic verification research outline",
        kind="research_proposal",
        target_audience="Acceptance-test reviewer",
        goal="Compare preserved synthetic evidence.",
        request_id="seed-project",
    )
    return app, {
        "topic_id": topic["topic_id"],
        "claim_ids": claims,
        "inference_id": inference["claim_id"],
        "belief_id": belief["belief_id"],
        "candidate_id": candidate["belief_id"],
        "project_id": project["project_id"],
        "imports": imports,
    }


def logical_dump(app: Application) -> str:
    with connect(app.config.database_path, readonly=True) as connection:
        return "\n".join(connection.iterdump())


def demonstrate() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ruminate-p2-smoke-") as temporary:
        app, seeded = seed_cognition(Path(temporary))
        now = datetime.now(UTC)
        before = logical_dump(app)
        preview = app.reviews.tick(seeded["topic_id"], dry_run=True, now=now)
        if logical_dump(app) != before or preview["created"] != 0:
            raise RuntimeError("Dry-run persisted changes")
        generated = app.reviews.tick(seeded["topic_id"], now=now)
        before_list = logical_dump(app)
        cards = app.reviews.list_queue(seeded["topic_id"], now=now)
        if logical_dump(app) != before_list:
            raise RuntimeError("Queue preview persisted changes")
        conflict = next((card for card in cards if card["reason"] == "possible_conflict"), None)
        if not conflict or not conflict["supports"] or not conflict["challenges"]:
            raise RuntimeError("Expected both sides of the grounded possible conflict")
        if conflict["old_belief_id"] != seeded["belief_id"]:
            raise RuntimeError("Conflict did not reference the explicit fixture belief")
        shown = app.reviews.present(seeded["topic_id"], request_id="smoke-show", now=now)
        if conflict["id"] not in {card["id"] for card in shown["cards"]} or shown["shown"] > 3:
            raise RuntimeError("Presentation did not respect the queue contract")
        app.reviews.feedback(
            conflict["id"], "postpone", postpone_days=7, request_id="smoke-postpone", now=now
        )
        app.reviews.tick(seeded["topic_id"], now=now + timedelta(seconds=1))
        repeated = app.reviews.list_queue(seeded["topic_id"], now=now + timedelta(seconds=2))
        if conflict["id"] in {card["id"] for card in repeated}:
            raise RuntimeError("Postponed candidate immediately reappeared")
        context = app.knowledge.topic_context(seeded["topic_id"])
        if {item["belief_id"] for item in context["confirmed_beliefs"]} != {seeded["belief_id"]}:
            raise RuntimeError("An unconfirmed candidate became a user belief")
        if {item["belief_id"] for item in context["belief_candidates"]} != {seeded["candidate_id"]}:
            raise RuntimeError("Model conjecture did not remain separate")
        created = app.outputs.create_draft(
            seeded["project_id"], claim_ids=seeded["claim_ids"], request_id="smoke-draft"
        )
        revision = app.outputs.get_revision(seeded["project_id"])
        citations = [item for claim in revision["snapshot"]["claims"] for item in claim["evidence"]]
        if len(citations) < 3 or len({item["source_id"] for item in citations}) != 3:
            raise RuntimeError("Draft did not retain all three sources")
        for citation in citations:
            source = app.get_source(citation["source_id"], version_id=citation["version_id"])
            block = next(
                item for item in source["blocks"] if item["block_id"] == citation["block_id"]
            )
            if (
                block["text"][citation["start_char"] : citation["end_char"]] != citation["quote"]
                or block["locator"] != citation["locator"]
            ):
                raise RuntimeError("A citation did not reproduce its persisted original")
        exported = app.outputs.export(seeded["project_id"])
        markdown = Path(exported["markdown_path"]).read_bytes()
        if markdown.decode("utf-8") != revision["markdown"]:
            raise RuntimeError("Export differs from the stored revision")
        sidecar = json.loads(Path(exported["sidecar_path"]).read_text())
        if sidecar["snapshot"] != revision["snapshot"]:
            raise RuntimeError("Export sidecar lost the evidence snapshot")
        reopened = Application(Config(app.config.data_dir, "off"))
        if reopened.outputs.get_revision(seeded["project_id"]) != revision:
            raise RuntimeError("Revision did not survive reopening the store")
        if conflict["id"] in {
            card["id"] for card in reopened.reviews.list_queue(seeded["topic_id"], now=now)
        }:
            raise RuntimeError("Postponement did not survive reopening")
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "status": "SUPPORTED",
            "version": app.health_check()["version"],
            "schema_version": app.health_check()["schema_version"],
            "mode": "offline; no model credentials or model calls",
            "fixtures": "two self-authored Markdown documents and one self-authored PDF",
            "belief_scope": "synthetic explicit statement; not a real user belief",
            "imported_sources": len(seeded["imports"]),
            "claims_with_exact_evidence": len(seeded["claim_ids"]),
            "dry_run_changed_records": False,
            "queue_preview_changed_records": False,
            "review_candidates_created": generated["created"],
            "possible_conflict": {
                "both_sides_present": True,
                "explicit_belief_link": True,
                "semantic_status": "requires_user_review",
            },
            "shown_cards": shown["shown"],
            "postponed_candidate_immediately_repeated": False,
            "confirmed_beliefs": len(context["confirmed_beliefs"]),
            "unconfirmed_candidates": len(context["belief_candidates"]),
            "output": {
                "kind": "research_proposal",
                "revision": created["version"],
                "citations": len(citations),
                "distinct_sources": 3,
                "citations_reproduced": len(citations),
                "sidecar_matches": True,
                "markdown_sha256": hashlib.sha256(markdown).hexdigest(),
                "provider": created["provider"],
                "review_status": created["review_status"],
            },
            "reopen_preserves_revision_and_feedback": True,
            "temporary_store_removed_after_run": True,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = demonstrate()
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
