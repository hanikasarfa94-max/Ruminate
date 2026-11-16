"""Loopback web adapter for the same local workflows used by CLI and MCP."""

from __future__ import annotations

import binascii
import json
import re
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

from ruminate.application import Application
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.workspace import ReadAction, WorkspaceAdapter, WorkspaceImage, WriteAction

_STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "transport.js": "text/javascript; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
_MESSAGES = {
    "invalid_input": "请检查填写的内容。",
    "invalid_json": "提交内容没有读完整，请重试。",
    "invalid_span": "请选择 1 到 1000 字的原文作为引用。",
    "no_claims": "先在这个主题下记下几个观点，再生成提纲。",
    "topic_exists": "这个主题名称已被使用。",
    "topic_not_found": "没有找到这个主题。",
    "source_not_found": "没有找到这份资料。",
    "version_not_found": "没有找到这个资料版本。",
    "block_not_found": "没有找到这段原文。",
    "claim_not_found": "没有找到这个观点。",
    "project_not_found": "没有找到这个输出项目。",
    "revision_not_found": "还没有生成提纲。",
    "review_not_found": "没有找到这张反刍卡片。",
    "review_closed": "这张卡片已经处理过了。",
    "idempotency_conflict": "这次提交与之前的内容不同，请重新提交。",
    "invalid_limit": "一次查看的数量超出了范围。",
    "invalid_range": "请检查选择的位置。",
    "invalid_revision": "请选择有效的输出版本。",
    "invalid_page": "这个页码不在资料中。",
    "not_pdf": "这份资料没有 PDF 页面。",
    "network_denied": "当前资料库关闭了联网导入。",
    "network_disabled": "当前资料库关闭了联网导入。",
    "output_budget": "这个主题内容较多，请先选择要使用的观点。",
}


class RuminateHTTPServer(ThreadingHTTPServer):
    """One local session; no document paths are accepted from the browser."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, config: Config, *, port: int = 0) -> None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise RuminateError("invalid_port", "Port must be between 0 and 65535")
        self.app = Application(config)
        self.app.initialize()
        self.workspace = WorkspaceAdapter(self.app)
        self.token = secrets.token_urlsafe(32)
        self.max_body_bytes = 4 * ((config.max_source_bytes + 2) // 3) + 16_384
        super().__init__(("127.0.0.1", port), _Handler)
        self.url = f"http://127.0.0.1:{self.server_port}"
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}


class _Handler(BaseHTTPRequestHandler):
    server: RuminateHTTPServer
    server_version = "Ruminate"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format: str, *args: Any) -> None:
        # Source titles, searches and private file names never enter access logs.
        pass

    def do_GET(self) -> None:
        self._dispatch(write=False)

    def do_HEAD(self) -> None:
        self._dispatch(write=False, head=True)

    def do_POST(self) -> None:
        self._dispatch(write=True)

    def do_OPTIONS(self) -> None:
        self._error(405, "method_not_allowed", "这个接口不支持该操作。")

    def _dispatch(self, *, write: bool, head: bool = False) -> None:
        try:
            self._check_origin(write=write)
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or len(self.path) > 8192:
                raise RuminateError("invalid_input", "Invalid request target")
            query_values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
            if any(len(values) != 1 for values in query_values.values()):
                raise RuminateError("invalid_input", "Repeated query parameter")
            query = {key: values[0] for key, values in query_values.items()}
            path = parsed.path
            if write:
                result = self._post(path, self._body())
                self._json(result)
            elif path.startswith("/api/"):
                self._get(path, query, head=head)
            else:
                self._static(path, head=head)
        except RuminateError as exc:
            status = 404 if exc.code.endswith("_not_found") else 400
            if exc.code in {"invalid_host", "invalid_origin", "invalid_token"}:
                status = 403
            elif exc.code == "payload_too_large":
                status = 413
            elif exc.code == "idempotency_conflict":
                status = 409
            self._error(status, exc.code, _MESSAGES.get(exc.code, exc.message), head=head)
        except (ValueError, UnicodeError, binascii.Error, RecursionError):
            self._error(400, "invalid_input", "请检查提交的内容。", head=head)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except (OSError, sqlite3.Error):
            self._error(500, "storage_error", "暂时无法读写资料库，请重试。", head=head)

    def _check_origin(self, *, write: bool) -> None:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            raise RuminateError("invalid_host", "请通过启动时显示的本地地址打开 Ruminate。")
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0] not in self.server.allowed_origins):
            raise RuminateError("invalid_origin", "请在 Ruminate 页面中完成操作。")
        if self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
            raise RuminateError("invalid_origin", "请在 Ruminate 页面中完成操作。")
        if write:
            values = self.headers.get_all("X-Ruminate-Token", [])
            if len(values) != 1 or not secrets.compare_digest(
                values[0].encode("utf-8"), self.server.token.encode("ascii")
            ):
                raise RuminateError("invalid_token", "页面连接已更新，请刷新后重试。")

    def _body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise RuminateError("invalid_input", "Chunked bodies are not supported")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise RuminateError("invalid_input", "Content-Length is required")
        length = int(lengths[0])
        if length > self.server.max_body_bytes:
            raise RuminateError("payload_too_large", "文件太大，请选择 20 MB 以内的文件。")
        if self.headers.get_content_type() != "application/json":
            raise RuminateError("invalid_input", "Expected application/json")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RuminateError("invalid_json", "Incomplete request body")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise RuminateError("invalid_json", "Expected a JSON object")
        return result

    def _get(self, path: str, query: dict[str, str], *, head: bool) -> None:
        if path == "/api/session":
            self._json({"token": self.server.token, "origin": self.server.url}, head=head)
            return
        direct: dict[str, ReadAction] = {
            "/api/management": "management",
            "/api/state": "state",
            "/api/topics": "topics",
            "/api/sources": "sources",
            "/api/reviews": "reviews",
            "/api/projects": "projects",
        }
        patterns: tuple[tuple[str, ReadAction, str], ...] = (
            (r"/api/beliefs/([a-z0-9_]+)", "belief", "belief_id"),
            (r"/api/sources/([a-z0-9_]+)/page", "source_page", "source_id"),
            (r"/api/sources/([a-z0-9_]+)/observations", "observations", "source_id"),
            (r"/api/sources/([a-z0-9_]+)", "source", "source_id"),
            (r"/api/topics/([^/]+)/context", "topic_context", "topic_id"),
            (r"/api/claims/([a-z0-9_]+)", "claim", "claim_id"),
            (r"/api/projects/([a-z0-9_]+)/(?:revision|download)", "revision", "project_id"),
            (r"/api/projects/([a-z0-9_]+)/impact", "impact", "project_id"),
            (r"/api/projects/([a-z0-9_]+)", "project", "project_id"),
            (r"/api/jobs/([a-z0-9_]+)", "job", "job_id"),
        )
        action = direct.get(path)
        params: dict[str, Any] = dict(query)
        if action is None:
            for pattern, candidate, key in patterns:
                if match := re.fullmatch(pattern, path):
                    action = candidate
                    params[key] = match[1]
                    break
        if action is None:
            raise RuminateError("route_not_found", "没有找到这个页面。")
        result = self.server.workspace.read(action, params)
        if isinstance(result, WorkspaceImage):
            self._send(
                result.png,
                "image/png",
                head=head,
                extra={
                    "X-Ruminate-Page-Count": str(result.metadata["page_count"]),
                    "X-Ruminate-Page": str(result.metadata["page"]),
                },
            )
        elif path.endswith("/download"):
            title = str(result["snapshot"]["project"]["title"])
            filename = re.sub(r"[\x00-\x1f\x7f/\\]", "-", title)[:100] + ".md"
            self._send(
                result["markdown"].encode("utf-8"),
                "text/markdown; charset=utf-8",
                head=head,
                extra={
                    "Content-Disposition": (
                        "attachment; filename=research.md; filename*=UTF-8''" + quote(filename)
                    )
                },
            )
        else:
            self._json(result, head=head)

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        direct: dict[str, WriteAction] = {
            "/api/topics": "create_topic",
            "/api/ingest/url": "capture_url",
            "/api/ingest/file": "upload_file",
            "/api/claims": "record_claim",
            "/api/reviews/generate": "generate_reviews",
            "/api/reviews/present": "present_reviews",
            "/api/projects": "create_project",
        }
        action = direct.get(path)
        params = {"request_id": uuid4().hex, **data}
        if action is None:
            if match := re.fullmatch(r"/api/reviews/([a-z0-9_]+)/feedback", path):
                action = "review_feedback"
                params["review_id"] = match[1]
            elif match := re.fullmatch(r"/api/projects/([a-z0-9_]+)/draft", path):
                action = "create_draft"
                params["project_id"] = match[1]
            elif match := re.fullmatch(r"/api/sources/([a-z0-9_]+)/edit", path):
                action = "update_source"
                params["source_id"] = match[1]
            elif match := re.fullmatch(r"/api/topics/([a-z0-9_-]+)/edit", path):
                action = "update_topic"
                params["topic_id"] = match[1]
        if action is None:
            raise RuminateError("route_not_found", "没有找到这个操作。")
        return self.server.workspace.write(action, params)

    def _static(self, path: str, *, head: bool) -> None:
        name = "index.html" if path == "/" else path.removeprefix("/")
        if name not in _STATIC_TYPES:
            self._error(404, "route_not_found", "没有找到这个页面。", head=head)
            return
        asset = files("ruminate").joinpath("ui", name)
        if not asset.is_file():
            self._error(404, "asset_not_found", "界面文件尚未安装。", head=head)
            return
        self._send(asset.read_bytes(), _STATIC_TYPES[name], head=head)

    def _json(self, value: Any, *, head: bool = False) -> None:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", head=head)

    def _error(self, status: int, code: str, message: str, *, head: bool = False) -> None:
        body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)
        self._send(
            body.encode("utf-8"), "application/json; charset=utf-8", status=status, head=head
        )

    def _send(
        self,
        body: bytes,
        content_type: str,
        *,
        status: int = 200,
        head: bool = False,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Connection", "close")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        if not head:
            self.wfile.write(body)


def create_server(config: Config, *, port: int = 0) -> RuminateHTTPServer:
    return RuminateHTTPServer(config, port=port)


def serve(config: Config, *, port: int = 8765) -> None:
    """Run until interrupted; the CLI owns only this process, not a daemon install."""
    with create_server(config, port=port) as server:
        print(server.url, flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
