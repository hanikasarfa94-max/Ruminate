"""SSRF and HTTP framing regression tests; no external network is used."""

from __future__ import annotations

import io
import socket
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from ruminate import __version__
from ruminate.errors import RuminateError
from ruminate.ingestion import fetch


class FakeSocket:
    def __init__(self, family: int, wire: bytes):
        self.family = family
        self.wire = wire
        self.connected: tuple[Any, ...] | None = None
        self.sent = b""
        self.timeout: float | None = None
        self.closed = False
        self.shutdown_called = False
        self.handshakes = 0

    def connect(self, endpoint: tuple[Any, ...]) -> None:
        self.connected = endpoint

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def makefile(self, *args: Any, **kwargs: Any) -> io.BytesIO:
        return io.BytesIO(self.wire)

    def close(self) -> None:
        self.closed = True

    def shutdown(self, _how: int) -> None:
        self.shutdown_called = True

    def do_handshake(self) -> None:
        self.handshakes += 1


class FakeTLSContext:
    def __init__(self) -> None:
        self.hostnames: list[str] = []
        self.protocols: list[str] = []

    def set_alpn_protocols(self, protocols: list[str]) -> None:
        self.protocols = protocols

    def wrap_socket(
        self, sock: FakeSocket, *, server_hostname: str, do_handshake_on_connect: bool
    ) -> FakeSocket:
        assert do_handshake_on_connect is False
        self.hostnames.append(server_hostname)
        return sock


def wire_response(
    body: bytes = b"<html><body>Article text</body></html>",
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> bytes:
    fields = {"Content-Type": "text/html; charset=utf-8", "Connection": "close"}
    fields.update(headers or {})
    encoded_headers = "".join(f"{key}: {value}\r\n" for key, value in fields.items())
    return f"HTTP/1.1 {status} Response\r\n{encoded_headers}\r\n".encode() + body


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "responses": [wire_response()],
        "sockets": [],
        "dns": [],
        "addresses": ["93.184.216.34"],
        "tls": FakeTLSContext(),
    }

    def resolve(host: str, port: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
        state["dns"].append((host, port))
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in state["addresses"]
        ]

    def create_socket(family: int, _kind: int) -> FakeSocket:
        sock = FakeSocket(family, state["responses"].pop(0))
        state["sockets"].append(sock)
        return sock

    monkeypatch.setattr(fetch.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(fetch.socket, "socket", create_socket)
    monkeypatch.setattr(fetch.ssl, "create_default_context", lambda: state["tls"])
    return state


def assert_error(code: str, operation: Callable[[], Any]) -> RuminateError:
    with pytest.raises(RuminateError) as raised:
        operation()
    assert raised.value.code == code
    return raised.value


def test_normalization_preserves_semantic_query_encoding_and_order() -> None:
    assert (
        fetch.normalize_url(
            "HTTPS://Example.COM.:443/path/%2f?q=a%2Bb&flag&x=2&UTM_source=a&x=1&fbclid=id#part"
        )
        == "https://example.com/path/%2f?q=a%2Bb&flag&x=2&x=1"
    )
    assert fetch.normalize_url("https://例子.测试/中文?q=研究") == (
        "https://xn--fsqu00a.xn--0zwm56d/%E4%B8%AD%E6%96%87?q=%E7%A0%94%E7%A9%B6"
    )


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("file:///etc/passwd", "unsupported_scheme"),
        ("gopher://example.org/a", "unsupported_scheme"),
        ("https://user:secret@example.org/a", "url_credentials"),
        ("http://example.org:8080/a", "blocked_port"),
        ("https://example.org:80/a", "blocked_port"),
        ("http://example.org:bad/a", "invalid_url"),
        ("http://[::1", "invalid_url"),
        ("http://[fe80::1%25en0]/", "invalid_url"),
        ("http://example.org\\@127.0.0.1/", "invalid_url"),
        (" http://example.org/", "invalid_url"),
        ("http://example.org/a\r\nHost:other", "invalid_url"),
        ("https:///missing", "invalid_url"),
    ],
)
def test_invalid_urls_are_rejected_before_dns_or_socket(
    network: dict[str, Any], url: str, code: str
) -> None:
    assert_error(code, lambda: fetch.fetch_url(url, network_allowed=True))
    assert network["dns"] == []
    assert network["sockets"] == []


@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({}, "network_authorization_required"),
        ({"network_mode": "off", "network_allowed": True}, "network_disabled"),
        ({"network_mode": "allowlist", "allowed_hosts": ("other.org",)}, "host_not_allowed"),
    ],
)
def test_unauthorized_requests_perform_no_dns_or_connection(
    network: dict[str, Any], options: dict[str, Any], code: str
) -> None:
    assert_error(code, lambda: fetch.fetch_url("https://example.org", **options))
    assert network["dns"] == []
    assert network["sockets"] == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.0.1",
        "172.16.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.100.100.200",
        "192.0.0.9",
        "192.88.99.1",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::",
        "64:ff9b::7f00:1",
        "ff02::1",
        "2001:db8::1",
    ],
)
def test_private_special_ipv4_ipv6_and_transition_destinations_blocked(
    network: dict[str, Any], address: str
) -> None:
    authority = f"[{address}]" if ":" in address else address
    assert_error(
        "blocked_address", lambda: fetch.fetch_url(f"http://{authority}/", network_allowed=True)
    )
    assert network["sockets"] == []


@pytest.mark.parametrize(
    "host", ["localhost", "example.localhost", "printer.local", "service.internal"]
)
def test_local_hostnames_never_resolve(network: dict[str, Any], host: str) -> None:
    assert_error(
        "blocked_address", lambda: fetch.fetch_url(f"https://{host}", network_allowed=True)
    )
    assert network["dns"] == []


def test_mixed_public_and_private_dns_answers_fail_closed(network: dict[str, Any]) -> None:
    network["addresses"] = ["93.184.216.34", "fd00::1"]
    assert_error(
        "blocked_address", lambda: fetch.fetch_url("https://example.org", network_allowed=True)
    )
    assert network["sockets"] == []


def test_socket_is_pinned_and_tls_uses_original_hostname(network: dict[str, Any]) -> None:
    body = b"%PDF-1.7\nnot-parsed-by-network-layer\n"
    network["responses"] = [wire_response(body, headers={"Content-Type": "application/pdf"})]
    result = fetch.fetch_url("https://EXAMPLE.org/paper.pdf?utm_source=x", network_allowed=True)
    assert result.body == body
    assert result.original_url == "https://EXAMPLE.org/paper.pdf?utm_source=x"
    assert result.final_url == "https://example.org/paper.pdf"
    assert result.content_type == "application/pdf"
    assert network["dns"] == [("example.org", 443)]
    sock = network["sockets"][0]
    assert sock.connected == ("93.184.216.34", 443)
    assert b"Host: example.org\r\n" in sock.sent
    assert b"Accept-Encoding: identity\r\n" in sock.sent
    assert f"User-Agent: Ruminate/{__version__} ".encode() in sock.sent
    assert network["tls"].hostnames == ["example.org"]
    assert network["tls"].protocols == ["http/1.1"]
    assert sock.handshakes == 1
    assert sock.shutdown_called and sock.closed


def test_dns_rebinding_cannot_trigger_second_resolution_during_connect(
    network: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def rebinding(host: str, port: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
        calls.append(host)
        ip = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", rebinding)
    fetch.fetch_url("http://example.org", network_allowed=True)
    assert calls == ["example.org"]
    assert network["sockets"][0].connected == ("93.184.216.34", 80)


def test_public_ipv6_connects_to_numeric_ipv6_sockaddr(network: dict[str, Any]) -> None:
    network["addresses"] = ["2606:4700:4700::1111"]
    fetch.fetch_url("https://example.org", network_allowed=True)
    assert network["sockets"][0].connected == ("2606:4700:4700::1111", 443, 0, 0)


def test_implicit_proxy_environment_is_ignored(
    network: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://169.254.169.254/")
    fetch.fetch_url("https://example.org", network_allowed=True)
    assert network["dns"] == [("example.org", 443)]
    assert network["sockets"][0].connected == ("93.184.216.34", 443)


def test_relative_redirect_and_authorized_final_document(network: dict[str, Any]) -> None:
    network["responses"] = [
        wire_response(status=302, headers={"Location": "/article?utm_source=redirect"}),
        wire_response(b"plain article", headers={"Content-Type": "text/plain"}),
    ]
    result = fetch.fetch_url(
        "https://example.org/old", network_mode="allowlist", allowed_hosts=("EXAMPLE.ORG",)
    )
    assert result.body == b"plain article"
    assert result.final_url == "https://example.org/article"
    assert len(network["dns"]) == 2


@pytest.mark.parametrize(
    ("location", "code", "options"),
    [
        ("https://127.0.0.1/private", "blocked_address", {"network_allowed": True}),
        ("https://[::1]/", "blocked_address", {"network_allowed": True}),
        ("http://example.org/plain", "redirect_downgrade", {"network_allowed": True}),
        ("file:///etc/passwd", "unsupported_scheme", {"network_allowed": True}),
        (
            "https://other.org/",
            "host_not_allowed",
            {"network_mode": "allowlist", "allowed_hosts": ("example.org",)},
        ),
    ],
)
def test_every_redirect_is_reauthorized_and_revalidated(
    network: dict[str, Any], location: str, code: str, options: dict[str, Any]
) -> None:
    network["responses"] = [wire_response(status=302, headers={"Location": location})]
    assert_error(code, lambda: fetch.fetch_url("https://example.org/start", **options))
    assert len(network["sockets"]) == 1


def test_same_host_redirect_rechecks_changed_dns(
    network: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    network["responses"] = [wire_response(status=302, headers={"Location": "/next"})]
    calls = 0

    def resolve(host: str, port: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
        nonlocal calls
        calls += 1
        ip = "93.184.216.34" if calls == 1 else "10.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", resolve)
    assert_error(
        "blocked_address", lambda: fetch.fetch_url("https://example.org", network_allowed=True)
    )
    assert calls == 2
    assert len(network["sockets"]) == 1


def test_redirect_loop_and_count_limit(network: dict[str, Any]) -> None:
    network["responses"] = [wire_response(status=302, headers={"Location": "/"})]
    assert_error(
        "redirect_loop", lambda: fetch.fetch_url("https://example.org", network_allowed=True)
    )
    network["responses"] = [wire_response(status=302, headers={"Location": "/other"})]
    assert_error(
        "too_many_redirects",
        lambda: fetch.fetch_url("https://example.org", network_allowed=True, max_redirects=0),
    )


@pytest.mark.parametrize("headers", [{"Content-Length": "9999"}, {}])
def test_declared_or_streamed_oversize_body_rejected(
    network: dict[str, Any], headers: dict[str, str]
) -> None:
    network["responses"] = [wire_response(b"x" * 100, headers=headers)]
    assert_error(
        "download_too_large",
        lambda: fetch.fetch_url("https://example.org", network_allowed=True, max_bytes=10),
    )


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip"])
def test_compressed_response_is_never_decompressed(network: dict[str, Any], encoding: str) -> None:
    network["responses"] = [
        wire_response(b"\x1f\x8bcompressed", headers={"Content-Encoding": encoding})
    ]
    assert_error(
        "unsupported_encoding", lambda: fetch.fetch_url("https://example.org", network_allowed=True)
    )


@pytest.mark.parametrize(
    "headers", [{"Content-Length": "-1"}, {"Content-Length": "2, 2"}, {"Content-Length": "50"}]
)
def test_invalid_or_incomplete_content_length_rejected(
    network: dict[str, Any], headers: dict[str, str]
) -> None:
    network["responses"] = [wire_response(b"abc", headers=headers)]
    assert_error(
        "invalid_response", lambda: fetch.fetch_url("https://example.org", network_allowed=True)
    )


def test_chunked_body_is_bounded_and_preserves_bytes(network: dict[str, Any]) -> None:
    network["responses"] = [
        wire_response(b"3\r\nabc\r\n0\r\n\r\n", headers={"Transfer-Encoding": "chunked"})
    ]
    assert fetch.fetch_url("https://example.org", network_allowed=True).body == b"abc"
    network["responses"] = [
        wire_response(b"4\r\nABCD\r\n0\r\n\r\n", headers={"Transfer-Encoding": "chunked"})
    ]
    assert_error(
        "download_too_large",
        lambda: fetch.fetch_url("https://example.org", network_allowed=True, max_bytes=3),
    )


def test_http_status_errors_do_not_expose_sensitive_query(network: dict[str, Any]) -> None:
    network["responses"] = [wire_response(status=403)]
    error = assert_error(
        "http_error",
        lambda: fetch.fetch_url("https://example.org/?token=private-secret", network_allowed=True),
    )
    assert "403" in error.message
    assert "private-secret" not in error.message
    assert "example.org" not in error.message


def test_dns_and_connection_failures_are_stable_and_redacted(
    network: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_dns(*_args: Any, **_kwargs: Any) -> Any:
        raise socket.gaierror("private resolver configuration")

    monkeypatch.setattr(fetch.socket, "getaddrinfo", fail_dns)
    error = assert_error(
        "dns_error", lambda: fetch.fetch_url("https://example.org", network_allowed=True)
    )
    assert "private" not in error.message


def test_dns_wait_obeys_total_deadline(
    network: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    finished = threading.Event()

    def slow_dns(*_args: Any, **_kwargs: Any) -> Any:
        finished.wait(0.5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", slow_dns)
    started = time.monotonic()
    try:
        assert_error(
            "fetch_timeout",
            lambda: fetch.fetch_url("https://example.org", network_allowed=True, timeout=0.02),
        )
        assert time.monotonic() - started < 0.3
        assert network["sockets"] == []
    finally:
        finished.set()


def test_connection_timeout_maps_to_fetch_timeout(
    network: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_connect(_self: Any, _endpoint: Any) -> None:
        raise TimeoutError("underlying private network detail")

    monkeypatch.setattr(FakeSocket, "connect", slow_connect)
    error = assert_error(
        "fetch_timeout", lambda: fetch.fetch_url("https://example.org", network_allowed=True)
    )
    assert "private" not in error.message


def test_tls_uses_secure_default_context() -> None:
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_content_type_retains_charset_for_parser_decoding(network: dict[str, Any]) -> None:
    body = b"<html><body>caf\xe9</body></html>"
    network["responses"] = [
        wire_response(body, headers={"Content-Type": "text/html; charset=windows-1252"})
    ]
    result = fetch.fetch_url("https://example.org", network_allowed=True)
    assert result.content_type == "text/html; charset=windows-1252"
    assert result.body == body


@pytest.mark.parametrize("value", ["text/html; " + "x" * 512, "text/html\r\n charset=UTF-8"])
def test_content_type_rejects_oversized_or_folded_metadata(
    network: dict[str, Any], value: str
) -> None:
    network["responses"] = [wire_response(headers={"Content-Type": value})]
    assert_error(
        "invalid_response",
        lambda: fetch.fetch_url("https://example.org", network_allowed=True),
    )


def test_giant_content_length_is_rejected_without_integer_conversion_failure(
    network: dict[str, Any],
) -> None:
    network["responses"] = [wire_response(headers={"Content-Length": "9" * 5000})]
    assert_error(
        "download_too_large",
        lambda: fetch.fetch_url("https://example.org", network_allowed=True),
    )


@pytest.mark.parametrize(
    "prefix",
    [
        b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nX-Unfinished-Header: ",
    ],
)
def test_watchdog_interrupts_real_blocked_header_or_body_socket_read(
    monkeypatch: pytest.MonkeyPatch, prefix: bytes
) -> None:
    # A local socketpair exercises real socket/file reads without making any
    # network request or adding a production SSRF bypass.
    client, peer = socket.socketpair()
    finished = threading.Event()

    def attach(connection: Any) -> None:
        client.settimeout(2)
        connection.sock = client
        connection._active_socket = client

    def write_partial_response() -> None:
        try:
            peer.recv(65536)
            peer.sendall(prefix)
            finished.wait(1)
        finally:
            peer.close()

    monkeypatch.setattr(fetch._PinnedConnection, "connect", attach)
    monkeypatch.setattr(
        fetch,
        "_resolve_addresses",
        lambda *_args: (fetch._Address(socket.AF_INET, "93.184.216.34"),),
    )
    writer = threading.Thread(target=write_partial_response, daemon=True)
    writer.start()
    started = time.monotonic()
    try:
        assert_error(
            "fetch_timeout",
            lambda: fetch.fetch_url("http://example.org", network_allowed=True, timeout=0.05),
        )
        assert time.monotonic() - started < 0.5
    finally:
        finished.set()
        client.close()
        writer.join(timeout=1)
