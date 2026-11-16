"""Local MCP Apps protocol test host, backed by a real Ruminate stdio server.

This is a development harness, not the Codex desktop UI. No model or external
network is used. The iframe has an opaque origin and communicates only through
MCP Apps postMessage; the parent relays tool calls over an actual MCP connection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from ruminate.application import Application
from ruminate.config import Config

HOST = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Ruminate MCP Apps protocol harness</title>
<style>body{margin:0;background:#e8ece7;font:13px system-ui}header{padding:12px 20px;
display:flex;justify-content:space-between}iframe{width:100%;height:calc(100vh - 48px);
border:0;background:white}#status{color:#244c3e}</style>
<header><span>MCP Apps 协议测试宿主 · 真实 stdio · 非 Codex 界面</span>
<span id="status">等待组件握手</span></header>
<iframe title="Ruminate MCP UI" sandbox="allow-scripts allow-forms allow-downloads"
src="/widget"></iframe>
<script>
const frame = document.querySelector('iframe');
const events = [];
const initial = INITIAL_RESULT;
const token = SESSION_TOKEN;
const send = message => frame.contentWindow.postMessage(message, '*');
window.addEventListener('message', async event => {
  if (event.source !== frame.contentWindow || event.data?.jsonrpc !== '2.0') return;
  const {id, method, params = {}} = event.data;
  if (!method) return;
  events.push({method, params});
  document.querySelector('#status').textContent = method;
  const answer = result => { if (id !== undefined) send({jsonrpc:'2.0', id, result}); };
  const fail = message => { if (id !== undefined)
    send({jsonrpc:'2.0', id, error:{code:-32603, message}}); };
  try {
    if (method === 'ui/initialize') {
      answer({protocolVersion:'2026-01-26',hostInfo:{name:'Ruminate protocol harness',version:'1'},
        hostCapabilities:{serverTools:{},serverResources:{},message:{text:{}},
          updateModelContext:{text:{},structuredContent:{}},openLinks:{},
          experimental:{"openai/modelContext":{},"openai/message":{}}},
        hostContext:{theme:'light',displayMode:'inline',availableDisplayModes:['inline'],
          locale:'zh-CN',platform:'web',containerDimensions:{width:innerWidth,height:800}}});
    } else if (method === 'ui/notifications/initialized') {
      send({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{arguments:{}}});
      send({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:initial});
    } else if (method === 'tools/call') {
      const response = await fetch('/call', {method:'POST',
        headers:{'Content-Type':'application/json','X-Harness-Token':token},
        body:JSON.stringify(params)});
      const result = await response.json();
      if (!response.ok) throw Error(result.error || 'MCP call failed');
      answer(result);
    } else if (method === 'ui/message' || method === 'ui/update-model-context') {
      await fetch('/event', {method:'POST',
        headers:{'Content-Type':'application/json','X-Harness-Token':token},
        body:JSON.stringify({method,params})});
      answer({});
    } else if (method === 'ui/notifications/size-changed') {
      // The test viewport stays fixed so responsive assertions are reproducible.
    } else if (method === 'ui/open-link') {
      answer({isError:true});
    } else if (id !== undefined) {
      send({jsonrpc:'2.0',id,error:{code:-32601,message:'Unsupported test-host method'}});
    }
  } catch(error) { fail(String(error.message || error)); }
});
</script></html>"""


class Harness(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(
        self, *, port: int, loop: asyncio.AbstractEventLoop, client: Client,
        widget: str, initial: dict[str, Any],
    ) -> None:
        self.loop = loop
        self.client = client
        self.token = secrets.token_urlsafe(32)
        self.widget = widget.encode()
        self.host_html = HOST.replace(
            'INITIAL_RESULT', json.dumps(initial, ensure_ascii=True).replace('<', r'\u003c')
        ).replace('SESSION_TOKEN', json.dumps(self.token)).encode()
        self.events: list[dict[str, Any]] = []
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: Harness

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send(self, data: bytes, *, status: int = 200, mime: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def valid(self, write: bool = False) -> bool:
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            return False
        if self.headers.get("Origin") not in {None, f"http://{expected}"}:
            return False
        return not write or secrets.compare_digest(
            self.headers.get("X-Harness-Token", "").encode(), self.server.token.encode()
        )

    def do_GET(self) -> None:
        if not self.valid():
            self.send(b'{"error":"origin"}', status=403)
        elif self.path == "/":
            self.send(self.server.host_html, mime="text/html; charset=utf-8")
        elif self.path == "/widget":
            self.send(self.server.widget, mime="text/html; charset=utf-8")
        elif self.path == "/events":
            self.send(json.dumps(self.server.events).encode())
        else:
            self.send(b'{}', status=404)

    def do_POST(self) -> None:
        if not self.valid(write=True):
            self.send(b'{"error":"origin or token"}', status=403)
            return
        try:
            self.connection.settimeout(15)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 29 * 1024 * 1024:
                raise ValueError("Invalid body size")
            data = json.loads(self.rfile.read(length))
            if self.path == "/event":
                self.server.events.append(data)
                self.send(b'{}')
            elif self.path == "/call" and data.get("name") in {
                "ui_read", "ui_write", "record_claim"
            }:
                future = asyncio.run_coroutine_threadsafe(
                    self.server.client.call_tool(data["name"], data.get("arguments", {})),
                    self.server.loop,
                )
                result = future.result(timeout=30)
                self.send(result.model_dump_json(by_alias=True).encode())
            else:
                self.send(b'{"error":"unsupported method"}', status=400)
        except Exception as exc:
            self.send(json.dumps({"error": str(exc)}).encode(), status=400)


async def run(data_dir: Path, port: int) -> None:
    Application(Config(data_dir, network_mode="off")).initialize()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ruminate", "--data-dir", str(data_dir.resolve()), "mcp"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"RUMINATE_MCP_WRITES": "1", "RUMINATE_NETWORK_MODE": "off"},
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        descriptor = next(item for item in tools.tools if item.name == "open_workspace")
        meta = descriptor.model_dump(by_alias=True)["_meta"]
        result = await client.read_resource(meta["ui"]["resourceUri"])
        widget = result.contents[0].text
        initial = await client.call_tool("open_workspace", {})
        if initial.is_error:
            raise RuntimeError("open_workspace failed")
        loop = asyncio.get_running_loop()
        server = Harness(
            port=port, loop=loop, client=client, widget=widget,
            initial=initial.model_dump(by_alias=True),
        )
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        task = asyncio.create_task(asyncio.to_thread(server.serve_forever, poll_interval=0.2))
        try:
            await task
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.data_dir, args.port))
    except KeyboardInterrupt:
        pass
