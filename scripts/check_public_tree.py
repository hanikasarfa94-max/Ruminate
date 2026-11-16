"""Check staged Git blobs for local data, private paths and common credential formats.

Uses only the Git index, prints no matched values, and makes no network requests.
This focused publication check complements manual review of documents and media.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath

PRIVATE_PARTS = {
    ".local", ".codex", ".agents", ".aws", ".ssh", ".venv", "node_modules",
    "data", "dist", "build", "Ruminate_Codex_Handoff_v1",
}
PRIVATE_NAMES = {".env", "auth.json", "credentials.json", "hosts.yml", ".DS_Store"}
PRIVATE_SUFFIXES = (
    ".sqlite", ".sqlite3", ".db", "-wal", "-shm", ".pem", ".key", ".p12", ".pfx",
    ".zip", ".tar.gz", ".pyc",
)
PUBLIC_EVIDENCE = {"README.md", "browser-workflow.json", "release-checks.json"}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "api_key": re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "personal_absolute_path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
}


def main() -> int:
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"]).split(b"\0")
    findings: list[tuple[str, str]] = []
    checked = 0
    for entry in entries:
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode().split()
        name = raw_name.decode("utf-8")
        path = PurePosixPath(name)
        checked += 1
        if mode != "100644" and mode != "100755":
            findings.append((name, "non_regular_file"))
        if stage != "0":
            findings.append((name, "unresolved_merge"))
        private = (
            bool(PRIVATE_PARTS.intersection(path.parts))
            or path.name in PRIVATE_NAMES
            or path.name.endswith(PRIVATE_SUFFIXES)
            or (path.name.startswith(".env.") and path.name != ".env.example")
            or name.startswith("docs/design/")
            or (name.startswith("docs/evidence/") and path.name not in PUBLIC_EVIDENCE)
            or (path.suffix == ".pdf" and not name.startswith("tests/fixtures/"))
        )
        if private:
            findings.append((name, "local_or_private_file"))
        blob = subprocess.check_output(["git", "cat-file", "blob", oid])
        text = blob.decode("utf-8", errors="ignore")
        for rule, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append((name, rule))
    for name, rule in findings:
        print(f"{name}: {rule}")
    print(f"Checked {checked} staged files; {len(findings)} finding(s).")
    return 1 if findings or not checked else 0


if __name__ == "__main__":
    raise SystemExit(main())
