"""Run a no-credentials CLI and MCP demonstration in disposable local storage."""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from smoke_mcp import smoke


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ruminate-local-smoke-") as directory:
        data_dir = Path(directory) / "data"
        commands = []
        for command in ("init", "init", "doctor"):
            result = subprocess.run(
                [sys.executable, "-m", "ruminate", "--data-dir", str(data_dir), command],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            payload = json.loads(result.stdout)
            if payload["status"] != "ok":
                raise RuntimeError("Local health check failed")
            commands.append({"command": command, "result": payload})
        report = {"commands": commands, "mcp": asyncio.run(smoke(data_dir))}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
