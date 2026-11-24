"""Opt-in MCP workflows; application services own storage and authorization."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import StrictBool

from ruminate.application import Application
from ruminate.errors import RuminateError


def _call(function: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return function(*args, **kwargs)
    except RuminateError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from None


def register_workflow_tools(server: MCPServer, app: Application) -> None:
    read = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )

    @server.tool(title="List research topics", annotations=read)
    def list_topics(limit: int = 20) -> dict[str, Any]:
        """Find topic IDs and names to use in capture, research and output tools."""
        return _call(lambda: {"topics": app.knowledge.list_topics(limit=limit)})

    @server.tool(title="List captured sources", annotations=read)
    def list_sources(
        topic_id: str | None = None, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        """Browse captured documents, including sources with little searchable native text."""
        return _call(
            lambda: {"sources": app.list_sources(topic=topic_id, limit=limit, offset=offset)}
        )

    @server.tool(title="Read an output revision", annotations=read)
    def get_output_revision(project_id: str, revision: int | None = None) -> dict[str, Any]:
        """Read a preserved Markdown draft with its citation audit and evidence snapshot."""
        return _call(app.outputs.get_revision, project_id, revision=revision)

    @server.tool(title="Read capture progress", annotations=read)
    def get_capture_job(job_id: str) -> dict[str, Any]:
        """Read capture progress or a parsing/access error without retrying outside access."""
        return _call(app.get_job, job_id)

    @server.tool(title="Create a research topic", annotations=write)
    def create_topic(
        slug: str, name: str, request_id: str, description: str = ""
    ) -> dict[str, Any]:
        """Create a topic requested by the user; reuse request_id for retries."""
        return _call(
            app.knowledge.create_topic,
            slug=slug,
            name=name,
            description=description,
            request_id=request_id,
        )

    @server.tool(title="Edit a research topic", annotations=write)
    def update_topic(
        topic_id: str, request_id: str, name: str | None = None, description: str | None = None,
    ) -> dict[str, Any]:
        """Edit a topic's display name or scope, preserving its ID and all linked research."""
        return _call(app.knowledge.update_topic, topic_id, name=name,
                     description=description, request_id=request_id)

    @server.tool(title="Organize a captured source", annotations=write)
    def update_source(
        source_id: str, request_id: str, title: str | None = None,
        topic_ids: list[str] | None = None, reset_title: StrictBool = False,
    ) -> dict[str, Any]:
        """Edit a source label or its topic list when the user asks to organize it.

        Omitted fields are preserved. topic_ids replaces the full classification; [] clears
        it. reset_title restores the current original title. Source versions, evidence and
        historical drafts stay intact. Reuse request_id when retrying this write.
        """
        return _call(app.management.update_source, source_id, title=title, topic_ids=topic_ids,
                     reset_title=reset_title, request_id=request_id)

    @server.tool(
        title="Capture a web source",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    def ingest_url(
        url: str,
        request_id: str,
        network_allowed: StrictBool = False,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a user-requested URL and its text to the local store.

        Set network_allowed only for a URL the user asked to import. The server's network
        policy still applies. A repeated request_id returns the same job without fetching again.
        """
        return _call(
            app.ingest_url,
            url,
            topic=topic_id,
            network_allowed=network_allowed,
            request_id=request_id,
        )

    @server.tool(title="Capture an allowed local file", annotations=write)
    def ingest_local_file(
        path: str, request_id: str, topic_id: str | None = None
    ) -> dict[str, Any]:
        """Import a user-selected file within the server's configured allowed roots.

        For a file outside configured roots, use the workspace file picker.
        A repeated request_id returns the original job.
        """
        return _call(app.ingest_file, Path(path), topic=topic_id, request_id=request_id)

    @server.tool(title="Record a research claim", annotations=write)
    def record_claim(
        topic_id: str,
        statement: str,
        claim_kind: Literal["source_assertion", "model_inference", "hypothesis", "question"],
        request_id: str,
        block_id: str | None = None,
        relation: Literal["supports", "challenges", "mentions", "unclear"] = "supports",
        start_char: int = 0,
        end_char: int | None = None,
    ) -> dict[str, Any]:
        """Save a claim or question, optionally with an exact block span in one transaction.

        MCP-authored claims retain host_model origin. Source assertions describe the source;
        hypotheses and questions stay distinct. This tool does not create user beliefs.
        """
        if block_id is not None:
            return _call(
                app.knowledge.create_claim_with_evidence,
                topic_id=topic_id,
                statement=statement,
                claim_kind=claim_kind,
                block_id=block_id,
                relation=relation,
                start_char=start_char,
                end_char=end_char,
                origin="host_model",
                request_id=request_id,
            )
        return _call(
            app.knowledge.create_claim,
            topic_id=topic_id,
            statement=statement,
            claim_kind=claim_kind,
            origin="host_model",
            request_id=request_id,
        )

    @server.tool(title="Link an exact evidence span", annotations=write)
    def link_evidence(
        claim_id: str,
        block_id: str,
        relation: Literal["supports", "challenges", "mentions", "unclear"],
        request_id: str,
        start_char: int = 0,
        end_char: int | None = None,
    ) -> dict[str, Any]:
        """Link the selected native-text span to a claim; the block fixes the source version."""
        return _call(
            app.knowledge.link_evidence,
            claim_id=claim_id,
            block_id=block_id,
            relation=relation,
            start_char=start_char,
            end_char=end_char,
            request_id=request_id,
        )

    @server.tool(title="Present review cards", annotations=write)
    def present_review_queue(
        request_id: str, topic_id: str | None = None, limit: int = 3
    ) -> dict[str, Any]:
        """Select cards to show now and record their exposure within the daily allowance.

        Use list_review_queue for a preview. Reuse request_id if a response is interrupted.
        """
        return _call(app.reviews.present, topic_id=topic_id, limit=limit, request_id=request_id)

    @server.tool(title="Save review feedback", annotations=write)
    def record_review_feedback(
        review_id: str,
        action: Literal[
            "useful",
            "not_useful",
            "already_known",
            "needs_more_evidence",
            "postpone",
            "dismiss",
            "discuss",
        ],
        request_id: str,
        note: str = "",
        postpone_days: int = 7,
    ) -> dict[str, Any]:
        """Record the user's response to a card, including a chosen postponement."""
        return _call(
            app.reviews.feedback,
            review_id,
            action,
            note=note,
            postpone_days=postpone_days,
            request_id=request_id,
        )

    @server.tool(title="Create an output project", annotations=write)
    def create_output_project(
        topic_id: str,
        title: str,
        request_id: str,
        kind: Literal[
            "research_note", "literature_review", "research_proposal", "product_spec", "article"
        ] = "research_note",
        target_audience: str = "",
        goal: str = "",
    ) -> dict[str, Any]:
        """Start a local writing project for the user's research topic."""
        return _call(
            app.outputs.create_project,
            topic_id=topic_id,
            title=title,
            kind=kind,
            target_audience=target_audience,
            goal=goal,
            request_id=request_id,
        )

    @server.tool(title="Create a cited output draft", annotations=write)
    def create_output_draft(
        project_id: str, request_id: str, claim_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Create an immutable local outline from selected claims and exact citations.

        Read the saved draft with get_output_revision; export it from the workspace or CLI.
        """
        return _call(
            app.outputs.create_draft, project_id, claim_ids=claim_ids, request_id=request_id
        )

    @server.tool(title="Save a visual reading note", annotations=write)
    def record_visual_observation(
        source_id: str,
        version_id: str,
        page: int,
        kind: Literal["visual_transcription", "visual_interpretation"],
        text: str,
        host: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Save the host's page transcription or interpretation separately from native text.

        First view that stored page with get_source_page_image. Use the returned source/version
        and page; the note retains its model origin and can be reviewed alongside the image.
        """
        return _call(
            app.add_visual_observation,
            source_id=source_id,
            version_id=version_id,
            page=page,
            kind=kind,
            text=text,
            host=host,
            request_id=request_id,
        )

    @server.tool(title="Read thinking notes and connections", annotations=read)
    def get_thinking(claim_id: str, offset: int = 0) -> dict[str, Any]:
        """Read the latest guide, human/model notes and explicitly reasoned backlinks."""
        return _call(app.thinking.context, claim_id, offset=offset)

    @server.tool(title="Save a reflection guide", annotations=write)
    def save_reflection_guide(
        claim_id: str,
        lens: Literal["connection", "counterexample", "boundary", "tradeoff", "revisit"],
        spark: str, prompts: list[str], evidence_ids: list[str],
        links: list[dict[str, str]], request_id: str,
    ) -> dict[str, Any]:
        """After reading evidence, explain a specific tension or connection, then ask 1–3
        progressive questions. Use evidence IDs on this claim. Links need claim_id and reason.
        This appends a host-model guide; it never changes a claim or confirms a user belief.
        """
        return _call(app.thinking.save_guide, claim_id=claim_id, lens=lens, spark=spark,
                     prompts=prompts, evidence_ids=evidence_ids, links=links, request_id=request_id)

    @server.tool(title="Save a thinking note", annotations=write)
    def save_reflection_note(
        claim_id: str, text: str, request_id: str,
        origin: Literal["human", "host_model"] = "host_model",
    ) -> dict[str, Any]:
        """Save a note. Use human only for the user's actual words; model writing stays
        host_model. A note is a reflection, not a confirmed user belief.
        """
        return _call(app.thinking.save_note, claim_id=claim_id, text=text,
                     origin=origin, request_id=request_id)

    @server.tool(title="Suggest an additional topic", annotations=write)
    def suggest_topic(
        target_kind: Literal["source", "claim"], target_id: str, topic_id: str,
        rationale: str, request_id: str, version_id: str | None = None,
        auto_apply: StrictBool = False,
    ) -> dict[str, Any]:
        """After reading the content, recommend an existing topic with a concrete reason.
        Sources require the current version_id. auto_apply adds a reversible classification
        when automatic organization is authorized. Manual/primary topics stay intact.
        Prior rejected suggestions are not recreated for the same content.
        """
        return _call(app.organization.suggest, target_kind=target_kind, target_id=target_id,
                     topic_id=topic_id, rationale=rationale, version_id=version_id,
                     auto_apply=auto_apply, request_id=request_id)

    @server.tool(title="Browse topic suggestions", annotations=read)
    def list_topic_suggestions(
        status: str = "active", limit: int = 25, offset: int = 0,
    ) -> dict[str, Any]:
        """Read model classification reasons and status, with pagination."""
        return _call(app.organization.list_suggestions, status=status, limit=limit, offset=offset)

    @server.tool(title="Apply or undo a topic suggestion", annotations=write)
    def decide_topic_suggestion(
        suggestion_id: str, action: Literal["apply", "dismiss", "revert"], request_id: str,
    ) -> dict[str, Any]:
        """Apply/dismiss a pending recommendation, or undo an applied addition.
        Undo preserves subsequent manual choices and the original topic.
        """
        return _call(app.organization.decide, suggestion_id, action, request_id=request_id)
