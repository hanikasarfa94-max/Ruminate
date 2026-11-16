"""Exercise the installer with a real Codex CLI and a disposable profile/store.

Downloads dependencies. No login, model call or existing desktop profile is used.
Pass --github after publishing to exercise the tagged GitHub archive too.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--uv", default=shutil.which("uv"))
    parser.add_argument("--github", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.uv:
        parser.error("Pass --uv or install uv first")
    with tempfile.TemporaryDirectory(prefix="ruminate-installer-check-") as directory:
        outside = Path(directory).resolve()
        profile, root, data = outside / "profile", outside / "plugin files", outside / "research"
        profile.mkdir()
        config = profile / "config.toml"
        config.write_text('# Existing user setting\nmodel = "gpt-6"\n')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("RUMINATE_") and key not in {"PYTHONPATH", "UV_OFFLINE"}}
        env["CODEX_HOME"] = str(profile)

        def execute(command: list[str], timeout: int = 180) -> Any:
            result = subprocess.run(command, cwd=outside, env=env, capture_output=True,
                                    text=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout)[-4000:])
            return result.stdout

        command = ["bash", str(ROOT / "install.sh"), "--install-dir", str(root),
                   "--codex", args.codex, "--uv", args.uv]
        if not args.github:
            command += ["--source", str(ROOT)]
        print("Installing into a temporary Codex profile…", flush=True)
        execute([*command, "--data-dir", str(data)], timeout=900)
        first = json.loads((root / "installation.json").read_text())
        note = outside / "sample.md"
        note.write_text("# Saved research\n\nEvidence remains available after a plugin update.\n")
        cli = [str(Path(first["runtime"]) / ".venv/bin/python"), "-m", "ruminate",
               "--data-dir", str(data)]
        captured = json.loads(execute([*cli, "ingest", str(note), "--topic", "install-check"]))
        assert captured["status"] == "indexed"
        mcp = root / "marketplace/plugins/ruminate/.mcp.json"
        wiring = json.loads(mcp.read_text())
        wiring["mcpServers"]["ruminate"]["env"]["RUMINATE_NETWORK_MODE"] = "off"
        mcp.write_text(json.dumps(wiring))
        print("Reinstalling and verifying saved research and permissions…", flush=True)
        execute(command, timeout=900)  # Deliberately omit --data-dir on the second run.
        second = json.loads((root / "installation.json").read_text())
        assert first["data_dir"] == second["data_dir"] == str(data)
        assert first["verification"]["checked_at"] != second["verification"]["checked_at"]
        assert second["verification"]["plugin_entrypoint_associated"]
        assert json.loads(mcp.read_text())["mcpServers"]["ruminate"]["env"] == {
            "RUMINATE_MCP_WRITES": "1", "RUMINATE_NETWORK_MODE": "off",
        }
        assert '# Existing user setting\nmodel = "gpt-6"' in config.read_text()
        execute([args.codex, "plugin", "remove", "ruminate@ruminate", "--json"])
        execute([args.codex, "plugin", "marketplace", "remove", "ruminate", "--json"])
        assert "Evidence remains available" in execute([*cli, "search", "evidence"])
        report = {
            "status": "passed", "version": second["version"], "github_archive": args.github,
            "fresh_install": True, "repeat_install": True, "data_and_permissions_preserved": True,
            "unrelated_config_preserved": True, "uninstall_keeps_data": True,
            "tool_count": second["verification"]["tool_count"],
            "entrypoints": second["verification"]["openai_ui_entrypoints"],
            "desktop_render_observed": False,
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
