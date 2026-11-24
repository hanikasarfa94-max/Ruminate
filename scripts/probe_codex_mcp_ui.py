"""Read Ruminate's MCP Apps resource through the installed Codex app-server protocol.

This launches an isolated app-server with a temporary store, or verifies the installed
project plugin with --installed-plugin. The latter reads its catalog and UI resource only.
It does not connect to the desktop app's private IPC, create chats, or change configuration.
The probe verifies transport/metadata forwarding; rendering must be observed in the desktop.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruminate import __version__
from ruminate.application import Application
from ruminate.config import Config

ROOT = Path(__file__).resolve().parents[1]
URI = "ui://ruminate/workspace-v0.7.0.html"
MIME = "text/html;profile=mcp-app"

# The proxy records only the initialization capabilities and forwards the actual server bytes.
PROXY = """import json,subprocess,sys,threading
from pathlib import Path
p=subprocess.Popen([sys.executable,"-m","ruminate","--data-dir",sys.argv[2],"mcp"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
def forward():
 for line in sys.stdin.buffer:
  try:
   msg=json.loads(line)
   if msg.get("method")=="initialize":
    params=msg.get("params",{})
    Path(sys.argv[1]).write_text(json.dumps({"capabilities":params.get("capabilities"),"clientInfo":params.get("clientInfo")}))
  except ValueError: pass
  p.stdin.write(line); p.stdin.flush()
 p.stdin.close()
threading.Thread(target=forward,daemon=True).start()
for line in p.stdout:
 sys.stdout.buffer.write(line); sys.stdout.buffer.flush()
p.wait()
"""


async def run(
    *, installed_plugin: bool = False, codex: str | None = None,
    marketplace_path: Path | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "BLOCKED",
        "scope": "Codex app-server stdio metadata/resource transport; no desktop rendering claim",
        "desktop_render_observed": False,
        "existing_thread_accessed": False,
        "user_configuration_changed": False,
        "mode": "installed_plugin" if installed_plugin else "direct_mcp",
        "working_directory": "temporary directory outside the project",
    }
    codex = codex or shutil.which("codex")
    if codex is None:
        report["reason"] = "codex not available on PATH"
        return report
    with tempfile.TemporaryDirectory(prefix="ruminate-app-server-ui-") as directory:
        workspace = Path(directory)
        data = workspace / "data"
        Application(Config(data, "off")).initialize()
        proxy = workspace / "stdio_proxy.py"
        proxy.write_text(PROXY)
        initialization = workspace / "mcp-initialize.json"
        args = [
            codex,
            "app-server",
            "--stdio",
            "-c",
            "mcp_servers.ruminate.command=" + json.dumps(sys.executable),
            "-c",
            "mcp_servers.ruminate.args=" + json.dumps([str(proxy), str(initialization), str(data)]),
            "-c",
            "mcp_servers.ruminate.cwd=" + json.dumps(str(ROOT)),
            "-c",
            "mcp_servers.ruminate.startup_timeout_sec=20",
            "-c",
            'mcp_servers.ruminate.env={RUMINATE_NETWORK_MODE="off",RUMINATE_MCP_WRITES="1"}',
        ]
        if installed_plugin:
            args = [codex, "app-server", "--stdio"]
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=4 * 1024 * 1024,
        )
        assert process.stdin and process.stdout

        async def send(method: str, params: Any, request_id: int | None = None) -> Any:
            message = {"method": method, "params": params}
            if request_id is not None:
                message["id"] = request_id
            assert process.stdin and process.stdout
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
            if request_id is None:
                return None
            async with asyncio.timeout(40):
                while line := await process.stdout.readline():
                    try:
                        response = json.loads(line)
                    except ValueError:
                        continue
                    if response.get("id") == request_id:
                        if "error" in response:
                            raise ValueError(json.dumps(response["error"]))
                        return response.get("result")
            raise ValueError("App-server exited before responding")

        try:
            result = await send(
                "initialize",
                {
                    "clientInfo": {"name": "ruminate_ui_probe", "version": __version__},
                    "capabilities": {
                        "experimentalApi": True,
                        "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": [MIME]}},
                    },
                },
                1,
            )
            report["app_server_user_agent"] = result.get("userAgent")
            await send("initialized", {})
            if installed_plugin:
                details = await send("plugin/read", {
                    "marketplacePath": str(
                        marketplace_path or ROOT / ".agents/plugins/marketplace.json"
                    ),
                    "pluginName": "ruminate",
                }, 10)
                plugin = details["plugin"]
                report["plugin"] = {
                    "id": plugin["summary"]["id"],
                    "installed": plugin["summary"]["installed"],
                    "enabled": plugin["summary"]["enabled"],
                    "declared_mcp_servers": plugin["mcpServers"],
                }
            statuses = await send(
                "mcpServerStatus/list", {"serverName": "ruminate", "detail": "full"}, 2
            )
            server = next(item for item in statuses["data"] if item["name"] == "ruminate")
            if installed_plugin:
                report["mcp_server_owner"] = server.get("pluginId")
                report["plugin_entrypoint_associated"] = (
                    report["plugin"]["installed"] is True
                    and report["plugin"]["enabled"] is True
                    and server.get("pluginId") == report["plugin"]["id"]
                    and server["name"] in report["plugin"]["declared_mcp_servers"]
                )
                if not report["plugin_entrypoint_associated"]:
                    raise ValueError(
                        "The workspace server is not associated with the enabled plugin"
                    )
            tools = server.get("tools", {})
            ui = tools.get("open_workspace", {}).get("_meta", {}).get("ui", {})
            entrypoints = tools.get("open_workspace", {}).get("_meta", {}).get("openai/ui", {})
            report["tool_count"] = len(tools)
            read_actions = (
                tools.get("ui_read", {}).get("inputSchema", {})
                .get("properties", {}).get("action", {}).get("enum", [])
            )
            report["management_read_supported"] = "management" in read_actions
            report["belief_read_supported"] = "belief" in read_actions
            report["open_workspace_ui"] = ui
            report["openai_ui_entrypoints"] = entrypoints.get("entrypoints", [])
            if installed_plugin and {"type": "global"} not in report["openai_ui_entrypoints"]:
                raise ValueError("Installed plugin is missing its global workspace entrypoint")
            report["transport_origin"] = server.get("httpOrigin")
            resource = await send("mcpServer/resource/read", {"server": "ruminate", "uri": URI}, 3)
            contents = resource["contents"]
            item = next(item for item in contents if item.get("uri") == URI)
            html = item.get("text", "")
            report["resource"] = {
                "uri": item.get("uri"),
                "mime_type": item.get("mimeType"),
                "bytes": len(html.encode()),
                "sha256": hashlib.sha256(html.encode()).hexdigest(),
                "meta": item.get("_meta"),
                "contains_bridge": "ui/initialize" in html,
                "contains_management_page": "thought-filters" in html and "/api/management" in html,
                "contains_localhost": "127.0.0.1:" in html or "localhost:" in html,
            }
            if initialization.is_file():
                report["forwarded_client_initialize"] = json.loads(initialization.read_text())
            await send("config/mcpServer/reload", None, 4)
            report["isolated_server_reload_accepted"] = True
            if ui.get("resourceUri") == URI and item.get("mimeType") == MIME and html:
                report["status"] = "SUPPORTED"
            else:
                report["reason"] = (
                    "MCP UI descriptor or resource did not match the expected contract"
                )
        except (ValueError, KeyError, StopIteration, TimeoutError) as exc:
            report["reason"] = str(exc)[:1200] or type(exc).__name__
        finally:
            if process.stdin:
                process.stdin.close()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--installed-plugin", action="store_true")
    parser.add_argument("--codex", help="Codex executable, including the desktop-bundled CLI")
    parser.add_argument("--marketplace-path", type=Path, help="Installed marketplace.json path")
    args = parser.parse_args()
    result = asyncio.run(run(installed_plugin=args.installed_plugin, codex=args.codex,
                             marketplace_path=args.marketplace_path))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    raise SystemExit(0 if result["status"] == "SUPPORTED" else 1)
