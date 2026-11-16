"""Exercise Ruminate through the official MCP client and a real stdio subprocess."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from mcp import Client, StdioServerParameters


async def smoke(data_dir: Path) -> dict[str, object]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ruminate", "--data-dir", str(data_dir), "mcp", "--transport", "stdio"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with asyncio.timeout(20):
        async with Client(parameters) as client:
            tools = await client.list_tools()
            result = await client.call_tool("health_check", {})
            if result.is_error:
                raise RuntimeError("Ruminate health_check returned an MCP tool error")
            return {
                "transport": "stdio",
                "client": "official-mcp-python-sdk",
                "sdk_version": version("mcp"),
                "protocol_version": client.protocol_version,
                "server": client.server_info.model_dump() if client.server_info else None,
                "tools": [tool.model_dump(by_alias=True) for tool in tools.tools],
                "health_check": result.structured_content,
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Read this store without initializing it")
    args = parser.parse_args()
    if args.data_dir is None:
        with tempfile.TemporaryDirectory(prefix="ruminate-mcp-smoke-") as directory:
            report = asyncio.run(smoke(Path(directory) / "data"))
    else:
        report = asyncio.run(smoke(args.data_dir.expanduser().resolve()))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
