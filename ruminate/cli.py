"""Small JSON CLI; stdout is reserved for protocol messages in MCP mode."""

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from ruminate import __version__
from ruminate.application import Application
from ruminate.cognition_cli import COMMANDS, dispatch, register
from ruminate.config import Config
from ruminate.errors import RuminateError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ruminate", description="Local evidence workspace")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-dir", type=Path, help="Override platform data directory")
    parser.add_argument(
        "--allow-root",
        type=Path,
        action="append",
        default=[],
        help="Authorize a file or directory for this invocation",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create storage and apply migrations")
    sub.add_parser("doctor", help="Check capabilities and schema without initializing")
    backup_export = sub.add_parser("export-backup", help="Write a new portable local backup")
    backup_export.add_argument("destination", type=Path)
    backup_import = sub.add_parser("import-backup", help="Restore into a new data directory only")
    backup_import.add_argument("archive", type=Path)
    mcp = sub.add_parser("mcp", help="Start the local MCP service")
    mcp.add_argument("--transport", choices=["stdio"], default="stdio")
    ui = sub.add_parser("ui", help="Open the local research workspace in your browser")
    ui.add_argument("--port", type=int, default=8765, help="Loopback port (0 chooses a free port)")
    ingest = sub.add_parser("ingest", help="Capture a selected file or explicitly authorized URL")
    ingest.add_argument("input")
    ingest.add_argument("--topic")
    ingest.add_argument(
        "--allow-network", action="store_true", help="Authorize this URL fetch and public redirects"
    )
    search = sub.add_parser("search", help="Search current source versions")
    search.add_argument("query")
    search.add_argument("--topic")
    search.add_argument("--limit", type=int, default=10)
    sources = sub.add_parser("sources", help="List or read preserved source versions")
    source_commands = sources.add_subparsers(dest="source_command", required=True)
    listing = source_commands.add_parser("list")
    listing.add_argument("--topic")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    reading = source_commands.add_parser("get")
    reading.add_argument("source_id")
    reading.add_argument("--version-id")
    reading.add_argument("--block-start", type=int, default=0)
    reading.add_argument("--block-limit", type=int, default=20)
    editing = source_commands.add_parser(
        "edit", help="Edit a source label and topic classification"
    )
    editing.add_argument("source_id")
    titles = editing.add_mutually_exclusive_group()
    titles.add_argument("--title")
    titles.add_argument("--reset-title", action="store_true")
    topics = editing.add_mutually_exclusive_group()
    topics.add_argument("--topic", action="append", dest="topic_ids",
                        help="Replace the full topic list; repeat for several topics")
    topics.add_argument("--clear-topics", action="store_true")
    editing.add_argument("--request-id", required=True)
    page_image = source_commands.add_parser(
        "page-image", help="Render one stored PDF page for Codex"
    )
    page_image.add_argument("source_id")
    page_image.add_argument("--version-id")
    page_image.add_argument("--page", type=int, default=1)
    page_image.add_argument(
        "--output", type=Path, required=True, help="New PNG file; never overwrite"
    )
    observations = sub.add_parser("observations", help="Store unverified host visual observations")
    observation_commands = observations.add_subparsers(dest="observation_command", required=True)
    observation_add = observation_commands.add_parser("add")
    observation_add.add_argument("source_id")
    observation_add.add_argument("--version-id", required=True)
    observation_add.add_argument("--page", type=int, required=True)
    observation_add.add_argument(
        "--kind", choices=["visual_transcription", "visual_interpretation"], required=True
    )
    observation_add.add_argument("--text-file", type=Path, required=True)
    observation_add.add_argument("--host", required=True, help="Caller-supplied host attribution")
    observation_add.add_argument(
        "--request-id", required=True, help="Idempotency key for this record"
    )
    observation_list = observation_commands.add_parser("list")
    observation_list.add_argument("source_id")
    observation_list.add_argument("--version-id")
    observation_list.add_argument("--page", type=int)
    observation_list.add_argument("--limit", type=int, default=20)
    jobs = sub.add_parser("jobs", help="Inspect or retry capture jobs")
    job_commands = jobs.add_subparsers(dest="job_command", required=True)
    job_listing = job_commands.add_parser("list")
    job_listing.add_argument("--limit", type=int, default=20)
    job_reading = job_commands.add_parser("get")
    job_reading.add_argument("job_id")
    retry = job_commands.add_parser("retry")
    retry.add_argument("job_id")
    retry.add_argument("--allow-network", action="store_true")
    register(sub)
    args = parser.parse_args(argv)
    try:
        config = Config.from_env(args.data_dir)
        if args.allow_root:
            config = replace(config, allowed_roots=(*config.allowed_roots, *args.allow_root))
        application = Application(config)
        if args.command == "mcp":
            from ruminate.mcp_server import run_server

            run_server(config)
            return 0
        if args.command == "ui":
            from ruminate.web_server import serve

            serve(config, port=args.port)
            return 0
        result: Any
        if args.command == "init":
            result = application.initialize()
        elif args.command == "export-backup":
            result = application.backups.export_backup(args.destination)
        elif args.command == "import-backup":
            result = application.backups.import_backup(args.archive)
        elif args.command in COMMANDS:
            result = dispatch(application, args)
        elif args.command == "doctor":
            result = application.health_check()
        elif args.command == "ingest":
            if "://" in args.input:
                result = application.ingest_url(
                    args.input, topic=args.topic, network_allowed=args.allow_network
                )
            else:
                # Typing a concrete local path in the CLI grants that file only, not its parent.
                target = Path(args.input).expanduser().absolute()
                application = Application(
                    replace(config, allowed_roots=(*config.allowed_roots, target))
                )
                result = application.ingest_file(target, topic=args.topic)
        elif args.command == "search":
            result = {
                "results": application.search_sources(
                    args.query, topic=args.topic, limit=args.limit
                )
            }
        elif args.command == "sources":
            if args.source_command == "list":
                result = {
                    "sources": application.list_sources(
                        topic=args.topic, limit=args.limit, offset=args.offset
                    )
                }
            elif args.source_command == "edit":
                result = application.management.update_source(
                    args.source_id, title=args.title,
                    topic_ids=[] if args.clear_topics else args.topic_ids,
                    reset_title=args.reset_title, request_id=args.request_id,
                )
            elif args.source_command == "page-image":
                from ruminate.ingestion.files import directory_fd

                result, png = application.get_source_page_image(
                    args.source_id, version_id=args.version_id, page=args.page
                )
                target = args.output.expanduser().absolute()
                # The explicit output is new, exclusive and not followed through a symlink.
                try:
                    resolved_parent = target.parent.resolve(strict=True)
                except RuntimeError:
                    raise RuminateError(
                        "storage_error", "Cannot resolve output directory"
                    ) from None
                with directory_fd(resolved_parent) as parent:
                    fd = os.open(
                        target.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(png)
                result["output"] = str(target)
            else:
                result = application.get_source(
                    args.source_id,
                    version_id=args.version_id,
                    block_start=args.block_start,
                    block_limit=args.block_limit,
                )
        elif args.command == "observations":
            if args.observation_command == "list":
                result = {
                    "observations": application.list_visual_observations(
                        args.source_id, version_id=args.version_id, page=args.page, limit=args.limit
                    )
                }
            else:
                from ruminate.ingestion.files import read_local_file

                target = args.text_file.expanduser().absolute()
                text_config = replace(config, allowed_roots=(*config.allowed_roots, target))
                content, _ = read_local_file(target, text_config)
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    raise RuminateError("invalid_text", "Observation file must be UTF-8") from None
                result = application.add_visual_observation(
                    source_id=args.source_id,
                    version_id=args.version_id,
                    page=args.page,
                    kind=args.kind,
                    text=text,
                    host=args.host,
                    request_id=args.request_id,
                )
        elif args.job_command == "list":
            result = {"jobs": application.list_jobs(args.limit)}
        elif args.job_command == "get":
            result = application.get_job(args.job_id)
        else:
            result = application.retry_job(args.job_id, network_allowed=args.allow_network)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return (
            1
            if result.get("status")
            in {"needs_attention", "needs_ocr", "needs_browser", "uninitialized"}
            else 0
        )
    except RuminateError as exc:
        error = exc.as_dict()
    except (OSError, sqlite3.Error):
        error = {
            "code": "storage_error",
            "message": "Cannot access local storage; check path and permissions",
        }
    print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
    return 2
