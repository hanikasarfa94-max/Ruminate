# Ruminate

**Turn saved sources into questions worth revisiting and writing with traceable citations.**

[简体中文](README.md) · [Contributing](CONTRIBUTING.md) · [Roadmap](docs/ROADMAP.md)

Ruminate is a local-first research workspace for Codex and ChatGPT, connected
through MCP and skills. It stores sources, evidence, ideas and output revisions
on your computer, while the existing AI host handles reading and reasoning.
The core needs no model API key or cloud database.

Source version **0.5.2** is in early development. Install from source; there is no
PyPI release or public marketplace distribution yet. Actual host verification is
recorded in [compatibility](docs/COMPATIBILITY.md).

## Workflow

1. Import PDF, Markdown, text, static HTML or an explicitly requested public URL.
2. Search preserved text and link ideas to exact source versions and spans.
3. Revisit questions, record feedback and continue discussing them with the host.
4. Build a cited outline, retain revisions and export Markdown.

The embedded workspace opens on **Ideas**, followed by **Sources** and **Outputs**.
Host-generated review suggestions appear under Ideas → Pending review. Scheduling
is configured in the user's own host; installing Ruminate does not create a task.

## Install

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and SQLite with FTS5.
macOS arm64 has been tested. Linux validation is tracked in the compatibility
report. File ingestion currently requires POSIX and is unavailable on Windows.

```bash
git clone https://github.com/hanikasarfa94-max/Ruminate.git
cd Ruminate
uv sync --locked
uv run ruminate init
uv run ruminate doctor
uv run ruminate ingest tests/fixtures/notes.md --topic demo
uv run ruminate search 'evidence' --topic demo
```

Run `uv run python scripts/smoke_p2.py` for a complete synthetic evidence-to-output
demo in a disposable store.

## Codex plugin

Prepare and apply the project registration, using your own data directory:

```bash
uv run python scripts/register_project_plugin.py --plugin-mcp --data-dir /absolute/path/to/private-data
uv run python scripts/register_project_plugin.py --apply
cd /tmp
codex plugin marketplace add /absolute/path/to/Ruminate --json
codex plugin add ruminate@ruminate-local --json
```

Reopen the plugin page, or ask Codex to open the Ruminate workspace. It is an
embedded MCP App and does not require a separately running web server.
See the [installation guide](docs/CODEX_MCP_UI.md) for updates and troubleshooting.
For a local browser preview, run `uv run ruminate ui`.

## Data and privacy

Storage defaults outside the checkout: `~/Library/Application Support/Ruminate/`
on macOS, and `$XDG_DATA_HOME/ruminate/` or `~/.local/share/ruminate/` on Linux.
Override it with `RUMINATE_DATA_DIR` or `--data-dir`.

Parsing, search, evidence management and template drafts run locally. URL capture
requires explicit network permission. Snippets and page images passed to the AI
host become part of that host conversation. See [security](SECURITY.md) and the
[usage guide](docs/USAGE.md) for boundaries and backup commands.

## Development

```bash
uv run pytest
uv run ruff check .
uv run mypy
node --test scripts/test_ui_transport.cjs
uv build
```

Node.js 22+ is needed for protocol tests, not to run the workspace.
See [CONTRIBUTING.md](CONTRIBUTING.md), [testing](docs/TESTING.md),
[architecture](docs/ARCHITECTURE.md), [known limitations](docs/KNOWN_LIMITATIONS.md)
and [CHANGELOG.md](CHANGELOG.md). Most detailed documentation is currently Chinese.
Issues and pull requests in either language are welcome.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) and
[third-party notices](THIRD_PARTY_NOTICES.md). Imported documents and user research
retain their own rights. Ruminate is an independent project, not affiliated with
OpenAI; product names describe interoperability.
