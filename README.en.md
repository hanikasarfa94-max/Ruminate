# Ruminate

**Turn saved sources into questions worth revisiting and writing with traceable citations.**

[简体中文](README.md) · [Contributing](CONTRIBUTING.md) · [Roadmap](docs/ROADMAP.md)

Ruminate is a local-first research workspace for Codex and ChatGPT, connected
through MCP and skills. It stores sources, evidence, ideas and output revisions
on your computer, while the existing AI host handles reading and reasoning.
The core needs no model API key or cloud database.

Version **0.5.3** is in early development. Install from GitHub into your personal
Codex plugin library; no PyPI or public directory release is available yet. Host verification is
recorded in [compatibility](docs/COMPATIBILITY.md).

## Workflow

1. Import PDF, Markdown, text, static HTML or an explicitly requested public URL.
2. Search preserved text and link ideas to exact source versions and spans.
3. Revisit questions, record feedback and continue discussing them with the host.
4. Build a cited outline, retain revisions and export Markdown.

The embedded workspace opens on **Ideas**, followed by **Sources** and **Outputs**.
Host-generated review suggestions appear under Ideas → Pending review. Scheduling
is configured in the user's own host; installing Ruminate does not create a task.

## Install in Codex

With a supported Codex desktop app installed, run:

```bash
curl -fsSL https://raw.githubusercontent.com/hanikasarfa94-max/Ruminate/main/install.sh | bash
```

The installer prepares Python and locked dependencies, registers the plugin and
checks its workspace through the actual Codex app-server. Reopen Codex, then choose
**Plugins → Ruminate → Open plugin**. On macOS it can use the desktop-bundled CLI.
No preinstalled Python, GitHub login or API key is required.

Run the same command to update; research data and scoped permissions are preserved.
macOS and Linux are supported; the plugin library requires a compatible desktop host.
For custom paths, removal and existing development installations, see the
[installation guide](docs/INSTALL.md). Use `bash install.sh --source "$PWD"` to
install from an existing checkout.

## Develop from source or use the CLI

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and SQLite with FTS5.
macOS arm64 has been tested; Linux Python 3.11 and 3.13 have passed CI. File ingestion currently requires POSIX and is unavailable on Windows.

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

To register a plugin that follows your development checkout, use the
[developer registration guide](docs/CODEX_MCP_UI.md).
The workspace is an embedded MCP App and needs no separate web server.
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
