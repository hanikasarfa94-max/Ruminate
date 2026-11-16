# Contributing to Ruminate

Bug reports, documentation improvements and focused pull requests are welcome.
For a larger change, open an issue describing the problem and proposed behavior
before investing in an implementation. English and Chinese are both welcome.

## Development setup

Use Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) and Node.js 22 or newer
for the JavaScript transport tests. The UI itself has no Node build step.

```bash
git clone https://github.com/hanikasarfa94-max/Ruminate.git
cd Ruminate
uv sync --locked
uv run python scripts/smoke_capture.py
uv run python scripts/smoke_p2.py
```

These demos use synthetic fixtures and disposable storage. Keep personal research
outside the checkout. See [the usage guide](docs/USAGE.md) for data directories,
configuration and CLI examples, and [compatibility](docs/COMPATIBILITY.md) for
tested platforms.

## Checks

Run these before submitting an application change:

```bash
uv run pytest
uv run ruff check .
uv run mypy
node --test scripts/test_ui_transport.cjs
uv build
```

After staging, run `uv run python scripts/check_public_tree.py` to check the exact
Git index for private paths, local data and common credential patterns. It reports
file names and rule names without printing matched secrets.

For UI changes, also run the [browser workflow](docs/TESTING.md). Real Codex smoke
tests use an existing host login and are separate from the offline suite.

## Design rules

- Put business logic in the application layer shared by CLI, MCP and UI.
- Preserve source versions, original text, exact evidence spans and output revisions.
- Keep source assertions, model inferences, hypotheses and confirmed user beliefs distinct.
- Keep parsing, retrieval and storage usable without model credentials.
- Add migrations for schema changes; do not rewrite existing migrations.
- Describe host support with actual test results and their scope.

See [AGENTS.md](AGENTS.md), [architecture](docs/ARCHITECTURE.md) and
[roadmap](docs/ROADMAP.md) for more context.

## Pull requests

Explain the problem, resulting behavior and how you verified it. Include focused
tests for new behavior or regression fixes. Update documentation when commands,
configuration, data formats or compatibility change. AI-assisted contributions
are welcome; the contributor is responsible for reviewing the code and checking
the results.

Use self-authored fixtures or material you can redistribute. Remove private
documents, credentials, local configuration and personal paths from examples,
screenshots and logs. Report vulnerabilities through [SECURITY.md](SECURITY.md).

Contributions are submitted under the project's [Apache License 2.0](LICENSE).
Respect other contributors and follow the [code of conduct](CODE_OF_CONDUCT.md).
