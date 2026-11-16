"""Exercise real loopback HTTP against the shared evidence and review services."""

import base64
import http.client
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from pypdf import PdfWriter

from ruminate.config import Config
from ruminate.persistence.database import connect
from ruminate.web_server import RuminateHTTPServer, create_server


class Browser:
    def __init__(self, server: RuminateHTTPServer) -> None:
        self.server = server

    def request(
        self,
        path: str,
        *,
        data: dict | None = None,
        method: str | None = None,
        token: bool = True,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        method = method or ("POST" if data is not None else "GET")
        fields = {"Origin": self.server.url}
        body = raw
        if data is not None:
            body = json.dumps(data).encode()
            fields["Content-Type"] = "application/json"
        if token:
            fields["X-Ruminate-Token"] = self.server.token
        fields.update(headers or {})
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        try:
            client.request(method, path, body=body, headers=fields)
            response = client.getresponse()
            result = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            value = (
                json.loads(result)
                if result
                and response_headers.get("content-type", "").startswith("application/json")
                else result
            )
            return response.status, response_headers, value
        finally:
            client.close()

    def get(self, path: str) -> Any:
        status, _, result = self.request(path)
        assert status == 200, result
        return result

    def post(self, path: str, **data: Any) -> dict:
        status, _, result = self.request(path, data=data)
        assert status == 200, result
        return result


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[Browser]:
    with create_server(Config(tmp_path / "data", network_mode="off"), port=0) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        worker.start()
        try:
            yield Browser(server)
        finally:
            server.shutdown()
            worker.join(timeout=3)


def upload(browser: Browser, *, body: bytes | None = None, **changes: Any) -> dict:
    values = {
        "filename": "experiment.md",
        "content_base64": base64.b64encode(
            body or b"# Agent evaluation\n\nA trial needs a control group.\n"
        ).decode(),
        "request_id": "upload-experiment",
    }
    return browser.post("/api/ingest/file", **{**values, **changes})


def test_http_research_workflow_preserves_citations_and_review_feedback(browser: Browser) -> None:
    topic = browser.post("/api/topics", name="Agent research", request_id="topic-once")
    # An omitted slug remains stable when the same browser request is retried.
    assert browser.post("/api/topics", name="Agent research", request_id="topic-once") == topic
    captured = upload(browser, topic=topic["topic_id"])
    source = browser.get("/api/sources/" + captured["source_id"])
    block = next(b for b in source["blocks"] if "control group" in b["text"])
    statement = browser.post(
        "/api/claims",
        topic_id=topic["topic_id"],
        statement="The note calls for a control group.",
        claim_kind="source_assertion",
        block_id=block["block_id"],
        request_id="claim-once",
    )
    assert statement["origin"] == "human"
    assert statement["evidence"][0]["quote"] == block["text"]
    assert statement["evidence"][0]["version_id"] == source["version_id"]
    browser.post(
        "/api/claims",
        topic_id=topic["topic_id"],
        statement="Does this apply to agent workflows?",
        claim_kind="question",
        request_id="question-once",
    )
    assert browser.get("/api/topics/" + topic["topic_id"] + "/context")["supported"]
    project = browser.post(
        "/api/projects",
        topic_id=topic["topic_id"],
        title="Research outline",
        request_id="project",
    )
    draft = browser.post("/api/projects/" + project["project_id"] + "/draft", request_id="draft")
    revision = browser.get("/api/projects/" + project["project_id"] + "/revision")
    assert revision["revision_id"] == draft["revision_id"]
    assert revision["snapshot"]["claims"][0]["evidence"][0]["quote"] == block["text"]
    status, headers, markdown = browser.request(
        "/api/projects/" + project["project_id"] + "/download"
    )
    assert status == 200
    assert headers["content-disposition"].startswith("attachment;")
    assert markdown.decode() == revision["markdown"]
    assert browser.post("/api/reviews/generate", topic_id=topic["topic_id"])["created"] > 0
    presented = browser.post("/api/reviews/present", topic_id=topic["topic_id"], request_id="show")
    assert presented["shown"] > 0
    card = presented["cards"][0]
    feedback = browser.post(
        "/api/reviews/" + card["id"] + "/feedback",
        action="postpone",
        request_id="feedback",
    )
    assert feedback["status"] == "postponed"
    assert all(c["id"] != card["id"] for c in browser.get("/api/reviews"))
    assert browser.get("/api/sources?query=control")[0]["source_id"] == captured["source_id"]
    state = browser.get("/api/state")
    assert len(state["sources"]) == len(state["topics"]) == len(state["projects"]) == 1


def test_http_organization_edits_use_shared_services_and_require_token(browser: Browser) -> None:
    topic = browser.post("/api/topics", name="Research", request_id="topic")
    captured = upload(browser, topic=topic["topic_id"])
    path = "/api/sources/" + captured["source_id"]
    original = browser.get(path)
    browser.post(
        "/api/topics/" + topic["topic_id"] + "/edit",
        name="Updated research", request_id="rename-topic",
    )
    status, _, _ = browser.request(
        path + "/edit", data={"title": "Unauthorized", "request_id": "denied"}, token=False,
    )
    assert status == 403
    browser.post(path + "/edit", title="Named source", topic_ids=[], request_id="edit-source")
    edited = browser.get(path)
    assert edited["display_title"] == "Named source"
    assert edited["title"] == original["title"]
    assert edited["blocks"] == original["blocks"]
    assert edited["topics"] == []
    assert browser.get("/api/state")["topics"][0]["name"] == "Updated research"


def test_upload_request_is_durable_and_rejects_changed_payload(browser: Browser) -> None:
    first = upload(browser)
    second = upload(browser)
    assert first["job_id"] == second["job_id"]
    assert first["source_id"] == second["source_id"]
    with create_server(browser.server.app.config) as other_server:
        # A new server process/session reuses the application's durable import record.
        worker = Thread(target=other_server.serve_forever, kwargs={"poll_interval": 0.02})
        worker.start()
        try:
            assert upload(Browser(other_server))["job_id"] == first["job_id"]
        finally:
            other_server.shutdown()
            worker.join(timeout=3)
    status, _, failure = browser.request(
        "/api/ingest/file",
        data={
            "filename": "experiment.md",
            "content_base64": base64.b64encode(b"changed").decode(),
            "request_id": "upload-experiment",
        },
    )
    assert status == 409
    assert failure["error"]["code"] == "idempotency_conflict"
    assert len(browser.get("/api/sources")) == 1


def test_claim_and_evidence_save_is_atomic(browser: Browser) -> None:
    topic = browser.post("/api/topics", name="Atomic note")
    captured = upload(browser, topic=topic["topic_id"])
    block = browser.get("/api/sources/" + captured["source_id"])["blocks"][0]
    values = {
        "topic_id": topic["topic_id"],
        "statement": "A thought",
        "claim_kind": "hypothesis",
        "block_id": block["block_id"],
        "end_char": 1001,
        "request_id": "atomic-note",
    }
    status, _, failure = browser.request("/api/claims", data=values)
    assert status == 400 and failure["error"]["code"] == "invalid_span"
    assert browser.server.app.knowledge.list_claims(topic["topic_id"]) == []
    values["end_char"] = len(block["text"])
    with ThreadPoolExecutor(max_workers=3) as executor:
        replies = list(executor.map(lambda _: browser.post("/api/claims", **values), range(3)))
    assert replies[0] == replies[1] == replies[2]
    assert replies[0]["evidence_status"] == "hypothesis"
    with connect(browser.server.app.config.database_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0] == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.example:8765"},
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_other_sites_cannot_read_the_session_or_store(browser: Browser, headers: dict) -> None:
    assert browser.request("/api/session", headers=headers)[0] == 403
    assert browser.request("/api/state", headers=headers)[0] == 403


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Ruminate-Token": "wrong"},
        {"X-Ruminate-Token": "é"},
    ],
)
def test_browser_writes_require_the_current_session_token(browser: Browser, headers: dict) -> None:
    status, _, body = browser.request(
        "/api/topics", data={"name": "Blocked"}, token=False, headers=headers
    )
    assert status == 403 and body["error"]["code"] == "invalid_token"
    assert browser.get("/api/topics") == []


def test_session_headers_and_static_routes_do_not_expose_local_files(browser: Browser) -> None:
    status, headers, body = browser.request("/api/session")
    assert status == 200 and body["token"] == browser.server.token
    assert headers["cache-control"] == "no-store"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    assert "access-control-allow-origin" not in headers
    assert "script-src 'self'" in headers["content-security-policy"]
    for path in ("/.env", "/../pyproject.toml", "/%2e%2e/pyproject.toml", "/ruminate.sqlite3"):
        assert browser.request(path)[0] == 404
    assert browser.request("/api/state", method="HEAD")[2] == b""
    assert browser.request("/api/topics", method="OPTIONS")[0] == 405


@pytest.mark.parametrize("filename", ["../secret.md", "/tmp/secret.md", "dir\\note.md", "a\x00.md"])
def test_upload_does_not_accept_paths(browser: Browser, filename: str) -> None:
    status, _, _ = browser.request(
        "/api/ingest/file",
        data={
            "filename": filename,
            "content_base64": base64.b64encode(b"content").decode(),
        },
    )
    assert status == 400
    assert browser.get("/api/sources") == []


@pytest.mark.parametrize("raw", [b"[]", b"null", b"{", b"\xff"])
def test_malformed_json_does_not_mutate_state(browser: Browser, raw: bytes) -> None:
    status, _, _ = browser.request(
        "/api/topics", method="POST", raw=raw, headers={"Content-Type": "application/json"}
    )
    assert status == 400
    assert browser.get("/api/topics") == []


def test_request_size_is_rejected_before_reading_body(browser: Browser) -> None:
    status, _, body = browser.request(
        "/api/ingest/file",
        method="POST",
        raw=b"",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(browser.server.max_body_bytes + 1),
        },
    )
    assert status == 413 and body["error"]["code"] == "payload_too_large"


def test_url_submission_keeps_network_off_policy(browser: Browser) -> None:
    result = browser.post("/api/ingest/url", url="https://example.com/note", request_id="url-once")
    assert result["status"] == "needs_attention"
    assert browser.get("/api/sources") == []


def test_pdf_page_endpoint_returns_real_page_count_and_png(browser: Browser) -> None:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=120, height=160)
    writer.add_blank_page(width=120, height=160)
    writer.write(output)
    captured = upload(browser, filename="scanned.pdf", body=output.getvalue())
    status, headers, body = browser.request(
        "/api/sources/" + captured["source_id"] + "/page?page=2"
    )
    assert status == 200 and body.startswith(b"\x89PNG\r\n\x1a\n")
    assert headers["x-ruminate-page-count"] == "2" and headers["x-ruminate-page"] == "2"
    assert browser.request("/api/sources/" + captured["source_id"] + "/page?page=3")[0] == 400


def test_server_only_binds_ipv4_loopback(browser: Browser) -> None:
    assert browser.server.server_address[0] == "127.0.0.1"
    assert browser.get("/api/state")["health"]["status"] == "ok"


def test_management_http_matches_application_and_does_not_write(browser: Browser) -> None:
    upload(browser)
    before = browser.get("/api/state")
    result = browser.get("/api/management?view=sources&query=control&limit=1&offset=0")
    assert result["total"] == 1
    assert result["items"][0]["source_id"] == before["sources"][0]["source_id"]
    assert browser.get("/api/management?view=thoughts&status=pending")["total"] == 0
    assert browser.get("/api/state") == before
    status, _, _ = browser.request("/api/management?view=thoughts&status=invalid")
    assert status == 400
