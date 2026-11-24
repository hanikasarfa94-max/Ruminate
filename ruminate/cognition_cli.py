"""CLI argument adaptation for the shared cognition services."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from ruminate.application import Application

COMMANDS = {"topics", "claims", "beliefs", "review", "tick", "outputs", "enrich", "thinking"}


def _key(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request-id", required=True, help="Stable key for retrying this write")


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    thinking = sub.add_parser("thinking", help="Reflection guides, notes and activity")
    thinking_commands = thinking.add_subparsers(dest="action", required=True)
    get = thinking_commands.add_parser("get")
    get.add_argument("claim_id")
    get.add_argument("--offset", type=int, default=0)
    activity = thinking_commands.add_parser("activity")
    activity.add_argument("--timezone", default="Asia/Shanghai")
    activity.add_argument("--day")
    note = thinking_commands.add_parser("note")
    note.add_argument("claim_id")
    note.add_argument("--text", required=True)
    note.add_argument("--origin", choices=["human", "host_model"], default="host_model")
    _key(note)
    guide = thinking_commands.add_parser("guide")
    guide.add_argument("claim_id")
    guide.add_argument("--lens", required=True,
                       choices=["connection", "counterexample", "boundary", "tradeoff", "revisit"])
    guide.add_argument("--spark", required=True)
    guide.add_argument("--prompt", action="append", required=True)
    guide.add_argument("--evidence-id", action="append", required=True)
    guide.add_argument("--links", default="[]", help="JSON list of claim_id/reason objects")
    _key(guide)

    topics = sub.add_parser("topics", help="Organize local research context")
    topic_commands = topics.add_subparsers(dest="action", required=True)
    create = topic_commands.add_parser("create")
    create.add_argument("slug")
    create.add_argument("--name", required=True)
    create.add_argument("--description", default="")
    _key(create)
    topic_commands.add_parser("list").add_argument("--limit", type=int, default=20)
    edit = topic_commands.add_parser("edit", help="Edit a topic name or research scope")
    edit.add_argument("topic")
    edit.add_argument("--name")
    edit.add_argument("--description")
    _key(edit)
    context = topic_commands.add_parser("context")
    context.add_argument("topic")
    context.add_argument("--max-claims", type=int, default=20)

    suggestions = topic_commands.add_parser("suggestions")
    suggestions.add_argument("--status", default="active")
    suggestions.add_argument("--offset", type=int, default=0)
    suggest = topic_commands.add_parser("suggest")
    suggest.add_argument("target_kind", choices=["source", "claim"])
    suggest.add_argument("target_id")
    suggest.add_argument("--topic", required=True)
    suggest.add_argument("--rationale", required=True)
    suggest.add_argument("--version-id")
    suggest.add_argument("--auto-apply", action="store_true")
    _key(suggest)
    decide = topic_commands.add_parser("decide")
    decide.add_argument("suggestion_id")
    decide.add_argument("decision", choices=["apply", "dismiss", "revert"])
    _key(decide)

    claims = sub.add_parser("claims", help="Keep claims separate from evidence and beliefs")
    claim_commands = claims.add_subparsers(dest="action", required=True)
    create = claim_commands.add_parser("add")
    create.add_argument("--topic", required=True)
    create.add_argument("--statement", required=True)
    create.add_argument(
        "--kind",
        required=True,
        choices=["source_assertion", "model_inference", "hypothesis", "question"],
    )
    create.add_argument(
        "--origin", choices=["human", "host_model", "deterministic"], default="human"
    )
    _key(create)
    claim_commands.add_parser("get").add_argument("claim_id")
    listing = claim_commands.add_parser("list")
    listing.add_argument("--topic", required=True)
    listing.add_argument("--limit", type=int, default=20)
    link = claim_commands.add_parser("link-evidence")
    link.add_argument("claim_id")
    link.add_argument("block_id")
    link.add_argument(
        "--relation", required=True, choices=["supports", "challenges", "mentions", "unclear"]
    )
    link.add_argument("--start", type=int, default=0)
    link.add_argument("--end", type=int)
    _key(link)

    beliefs = sub.add_parser(
        "beliefs", help="Record explicit human statements or unconfirmed candidates"
    )
    belief_commands = beliefs.add_subparsers(dest="action", required=True)
    create = belief_commands.add_parser("add")
    create.add_argument("--topic", required=True)
    create.add_argument("--text", required=True)
    create.add_argument("--provenance", required=True)
    create.add_argument("--origin", choices=["human", "host_model"], default="human")
    create.add_argument(
        "--confirm", action="store_true", help="Explicit user confirmation of this belief"
    )
    _key(create)
    link = belief_commands.add_parser("link")
    link.add_argument("belief_id")
    link.add_argument("claim_id")
    link.add_argument(
        "--relation", required=True, choices=["supports", "challenges", "mentions", "unclear"]
    )
    link.add_argument("--justification", required=True)
    _key(link)

    tick = sub.add_parser(
        "tick", help="Generate evidence-grounded review candidates; no notifications"
    )
    tick.add_argument("--topic")
    tick.add_argument("--dry-run", action="store_true")
    review = sub.add_parser("review", help="Read, present and give feedback on review cards")
    review_commands = review.add_subparsers(dest="action", required=True)
    for action in ("list", "present"):
        command = review_commands.add_parser(action)
        command.add_argument("--topic")
        command.add_argument("--limit", type=int, default=3)
        if action == "present":
            _key(command)
    feedback = review_commands.add_parser("feedback")
    feedback.add_argument("review_id")
    feedback.add_argument(
        "feedback",
        choices=[
            "useful",
            "not_useful",
            "already_known",
            "needs_more_evidence",
            "postpone",
            "dismiss",
            "discuss",
        ],
    )
    feedback.add_argument("--note", default="")
    feedback.add_argument("--postpone-days", type=int, default=7)
    _key(feedback)

    outputs = sub.add_parser("outputs", help="Create and export evidence-backed research drafts")
    output_commands = outputs.add_subparsers(dest="action", required=True)
    create = output_commands.add_parser("create")
    create.add_argument("--topic", required=True)
    create.add_argument("--title", required=True)
    create.add_argument(
        "--type",
        default="research_note",
        choices=[
            "research_note",
            "literature_review",
            "research_proposal",
            "product_spec",
            "article",
        ],
    )
    create.add_argument("--audience", default="")
    create.add_argument("--goal", default="")
    _key(create)
    listing = output_commands.add_parser("list")
    listing.add_argument("--topic")
    listing.add_argument("--status", choices=["active", "archived"])
    listing.add_argument("--limit", type=int, default=20)
    for action in ("get", "draft", "revision", "export", "impact"):
        command = output_commands.add_parser(action)
        command.add_argument("project_id")
        if action in {"revision", "export"}:
            command.add_argument("--revision", type=int)
        if action == "export":
            command.add_argument("--format", choices=["md"], default="md")
        if action == "draft":
            command.add_argument("--claim", action="append", dest="claim_ids")
            _key(command)
    enrich = sub.add_parser(
        "enrich", help="Generate unverified proposals with the local NoLLM provider"
    )
    enrich.add_argument("source_id")
    enrich.add_argument("--version-id")
    _key(enrich)


def dispatch(app: Application, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "thinking":
        if args.action == "get":
            return app.thinking.context(args.claim_id, offset=args.offset)
        if args.action == "activity":
            return app.thinking.activity(timezone=args.timezone, day=args.day)
        if args.action == "note":
            return app.thinking.save_note(claim_id=args.claim_id, text=args.text,
                                          origin=args.origin, request_id=args.request_id)
        try:
            links = json.loads(args.links)
        except (ValueError, TypeError) as exc:
            from ruminate.errors import RuminateError

            raise RuminateError("invalid_input", "links must be JSON") from exc
        return app.thinking.save_guide(claim_id=args.claim_id, lens=args.lens, spark=args.spark,
                                       prompts=args.prompt, evidence_ids=args.evidence_id,
                                       links=links, request_id=args.request_id)
    if args.command == "topics":
        if args.action == "suggestions":
            return app.organization.list_suggestions(status=args.status, offset=args.offset)
        if args.action == "suggest":
            return app.organization.suggest(target_kind=args.target_kind, target_id=args.target_id,
                topic_id=args.topic, rationale=args.rationale, version_id=args.version_id,
                auto_apply=args.auto_apply, request_id=args.request_id)
        if args.action == "decide":
            return app.organization.decide(args.suggestion_id, args.decision,
                                            request_id=args.request_id)
        if args.action == "create":
            return app.knowledge.create_topic(
                slug=args.slug,
                name=args.name,
                description=args.description,
                request_id=args.request_id,
            )
        if args.action == "list":
            return {"topics": app.knowledge.list_topics(args.limit)}
        if args.action == "edit":
            return app.knowledge.update_topic(
                args.topic, name=args.name, description=args.description,
                request_id=args.request_id,
            )
        return app.knowledge.topic_context(args.topic, max_claims=args.max_claims)
    if args.command == "claims":
        if args.action == "add":
            return app.knowledge.create_claim(
                topic_id=args.topic,
                statement=args.statement,
                claim_kind=args.kind,
                origin=args.origin,
                request_id=args.request_id,
            )
        if args.action == "get":
            return app.knowledge.get_claim(args.claim_id)
        if args.action == "list":
            return {"claims": app.knowledge.list_claims(args.topic, limit=args.limit)}
        return app.knowledge.link_evidence(
            claim_id=args.claim_id,
            block_id=args.block_id,
            relation=args.relation,
            start_char=args.start,
            end_char=args.end,
            request_id=args.request_id,
        )
    if args.command == "beliefs":
        if args.action == "add":
            return app.knowledge.create_belief(
                topic_id=args.topic,
                text=args.text,
                provenance=args.provenance,
                confirmed=args.confirm,
                origin=args.origin,
                request_id=args.request_id,
            )
        return app.knowledge.link_belief(
            belief_id=args.belief_id,
            claim_id=args.claim_id,
            relation=args.relation,
            justification=args.justification,
            request_id=args.request_id,
        )
    if args.command == "tick":
        return app.reviews.tick(topic_id=args.topic, dry_run=args.dry_run)
    if args.command == "review":
        if args.action == "list":
            return {"cards": app.reviews.list_queue(topic_id=args.topic, limit=args.limit)}
        if args.action == "present":
            return app.reviews.present(
                topic_id=args.topic, limit=args.limit, request_id=args.request_id
            )
        return app.reviews.feedback(
            args.review_id,
            args.feedback,
            note=args.note,
            postpone_days=args.postpone_days,
            request_id=args.request_id,
        )
    if args.command == "outputs":
        if args.action == "create":
            return app.outputs.create_project(
                topic_id=args.topic,
                title=args.title,
                kind=args.type,
                target_audience=args.audience,
                goal=args.goal,
                request_id=args.request_id,
            )
        if args.action == "list":
            return {
                "projects": app.outputs.list_projects(
                    topic_id=args.topic, status=args.status, limit=args.limit
                )
            }
        if args.action == "get":
            return app.outputs.get_project(args.project_id)
        if args.action == "draft":
            return app.outputs.create_draft(
                args.project_id, claim_ids=args.claim_ids, request_id=args.request_id
            )
        if args.action == "revision":
            return app.outputs.get_revision(args.project_id, revision=args.revision)
        if args.action == "impact":
            return app.outputs.impact(args.project_id)
        return app.outputs.export(args.project_id, revision=args.revision)
    return asyncio.run(
        app.enrichment.start(args.source_id, version_id=args.version_id, request_id=args.request_id)
    )
