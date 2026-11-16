# Ruminate contributor instructions

Read `README.md`, `CONTRIBUTING.md`, `docs/ARCHITECTURE.md` and
`docs/ROADMAP.md` before making changes.

- Keep business logic in the application layer shared by CLI, MCP and UI.
- Preserve source versions, exact evidence locators and historical output revisions.
- Distinguish source assertions, model inferences, hypotheses and confirmed user beliefs.
- Default to local processing with no model credentials or automatic network calls.
- Keep user documents, databases, credentials, host sessions and local configuration out of Git.
- Use synthetic or explicitly redistributable fixtures; remove personal data from reports.
- Record actual test outcomes and host limitations. Documentation is not an account test.
- Run `uv run pytest`, `uv run ruff check .` and `uv run mypy` for application changes.
- Run `node --test scripts/test_ui_transport.cjs` for transport changes and the browser
  workflow in `docs/TESTING.md` for UI behavior changes.
- Add migrations rather than modifying existing migration files.
- Keep contributors' changes intact and avoid unrelated refactoring.
- Stage only reviewed files; run `scripts/check_public_tree.py` before publishing.
