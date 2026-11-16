"""MCP Apps resource and app-only bridge for the local Ruminate workspace."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from collections.abc import Callable
from importlib.resources import files
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from ruminate.application import Application
from ruminate.errors import RuminateError
from ruminate.workspace import ReadAction, WorkspaceAdapter, WorkspaceImage, WriteAction

UI_URI = "ui://ruminate/workspace-v0.6.0.html"
UI_MIME_TYPE = "text/html;profile=mcp-app"
UI_META: dict[str, Any] = {
    "ui": {
        "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []},
        "prefersBorder": False,
    }
}


def workspace_html() -> str:
    """Bundle shipped assets, never local user data or a localhost URL, into one resource."""
    assets = files("ruminate").joinpath("ui")
    document = assets.joinpath("index.html").read_text(encoding="utf-8")
    css = assets.joinpath("style.css").read_text(encoding="utf-8")
    scripts = [
        assets.joinpath(name).read_text(encoding="utf-8") for name in ("transport.js", "app.js")
    ]
    # These are fixed application assets. Source text enters later through tool results.
    document = re.sub(r'<link\s+rel="stylesheet"\s+href="/style\.css"\s*>', "", document)
    document = re.sub(r'<script\s+src="/(?:transport|app)\.js"[^>]*>\s*</script>', "", document)
    document = document.replace(
        "</head>", "<style>" + css.replace("</style", "<\\/style") + "</style></head>"
    )
    code = "<script>window.RUMINATE_EMBEDDED = true;</script>" + "".join(
        "<script>" + script.replace("</script", "<\\/script") + "</script>" for script in scripts
    )
    document = document.replace("</body>", code + "</body>")
    if len(document.encode("utf-8")) > 1024 * 1024:
        raise RuminateError("ui_bundle_too_large", "Workspace UI exceeds its resource budget")
    return document


def _result(value: Any) -> CallToolResult:
    if isinstance(value, WorkspaceImage):
        return CallToolResult(
            structured_content={"data": value.metadata},
            content=[
                TextContent(type="text", text=f"第 {value.metadata['page']} 页"),
                ImageContent(
                    type="image",
                    data=base64.b64encode(value.png).decode("ascii"),
                    mime_type="image/png",
                ),
            ],
        )
    return CallToolResult(
        structured_content={"data": value},
        content=[TextContent(type="text", text="工作区已更新。")],
    )


def _call(function: Callable[[], Any]) -> CallToolResult:
    try:
        return _result(function())
    except RuminateError as exc:
        error = exc.as_dict()
    except (OSError, sqlite3.Error):
        error = {"code": "storage_error", "message": "暂时无法读写资料库，请重试。"}
    return CallToolResult(
        is_error=True,
        structured_content={"error": error},
        content=[TextContent(type="text", text=json.dumps(error, ensure_ascii=False))],
    )


def register_ui_tools(server: MCPServer, app: Application) -> None:
    """Register only with explicit workflow mode; default read-only tools stay unchanged."""
    workspace = WorkspaceAdapter(app)
    readonly = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.resource(
        UI_URI,
        name="ruminate_workspace",
        title="Ruminate 工作区",
        description="Interactive sources, review cards, research and outputs.",
        mime_type=UI_MIME_TYPE,
        meta=UI_META,
    )
    def resource() -> str:
        return workspace_html()

    @server.tool(
        title="打开 Ruminate 工作区",
        annotations=readonly,
        meta={
            "ui": {"resourceUri": UI_URI, "visibility": ["model", "app"]},
            "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
            "openai/outputTemplate": UI_URI,
        },
    )
    def open_workspace() -> CallToolResult:
        """Open the interactive Ruminate workspace inside a compatible MCP Apps host.

        Shows local sources, review cards, research topics and cited outputs. This read-only
        render tool returns the same initial data for hosts without interactive UI support.
        """
        result = _call(lambda: workspace.read("state", {}))
        if result.is_error:
            return result
        assert result.structured_content is not None
        state = result.structured_content["data"]
        return CallToolResult(
            structured_content=state,
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"Ruminate 工作区：{len(state['sources'])} 份资料，"
                        f"{len(state['topics'])} 个主题，{len(state['projects'])} 个输出项目。"
                    ),
                )
            ],
        )

    @server.tool(
        title="Read Ruminate workspace data",
        annotations=readonly,
        meta={"ui": {"visibility": ["app"]}},
    )
    def ui_read(action: ReadAction, params: dict[str, Any] | None = None) -> CallToolResult:
        """App-only read operation. Select a named local view and its bounded query parameters.

        No HTTP requests, arbitrary paths, SQL or mutations are accepted. PDF pages return
        ImageContent plus page metadata. Other views return structuredContent.data.
        """
        return _call(lambda: workspace.read(action, params or {}))

    @server.tool(
        title="Save a Ruminate workspace action",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        meta={"ui": {"visibility": ["app"]}},
    )
    def ui_write(action: WriteAction, params: dict[str, Any]) -> CallToolResult:
        """Apply a user action from this app's forms; params must include a stable request_id.

        Named actions capture a submitted URL or uploaded bytes, record an entered thought,
        handle review cards, or create a cited draft. Only capture_url uses the network, under
        the configured policy. No arbitrary file paths, belief confirmations or deletions.
        """
        return _call(lambda: workspace.write(action, params))
