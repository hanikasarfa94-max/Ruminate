# Changelog

This log describes user-visible changes. Versions before the public repository
were local development milestones. The current source version is 0.5.2; this file
does not imply a PyPI package or a GitHub release has been published.

## Unreleased

- Reject cyclic configured paths consistently on Python 3.13 and newer, while
  allowing new data directories that have not yet been created.
- Apache 2.0 license, contributor and security documentation, bilingual README,
  issue and pull request templates, CI and a public-tree check.
- Concise interface and Markdown draft wording. Existing format-1 output
  revisions remain readable and reproducible; new drafts use format 2.

## 0.5.2 — 2026-11-16

- Ruminate `r.` artwork for plugin and MCP icons, with light and dark variants.

## 0.5.1 — 2026-11-16

- Ideas open first in the unified workspace.
- Consistent topic controls, keyboard navigation and file selection.
- Current reading context can follow the selection into the host conversation.
- Discussion actions prepare an editable prompt when supported by the host.

## 0.5.0 — 2026-11-16

- Unified Ideas, Sources and Outputs workspace with search, filters and pagination.
- Model-generated questions join Ideas → Pending review with exact source references.
- Review feedback, citations, PDF pages, output revisions and Markdown export.

## Earlier development milestones

- **0.4.x:** MCP Apps resource, host bridge, plugin packaging and four workflow skills.
- **0.3.x:** topics, claims, evidence, review scheduling, cited drafts and backup/restore.
- **0.2.x:** PDF/HTML/Markdown/TXT import, immutable source versions, full-text search
  and on-demand page images for host vision.
- **0.1.x:** local application skeleton, SQLite storage, CLI and stdio MCP.
