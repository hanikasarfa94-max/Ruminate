"""Explicitly authorized HTTP imports with DNS-pinned, bounded connections.

http.client deliberately replaces a generic HTTP client here: validation and the
socket connect use the same numeric IP, while TLS verifies the original hostname.
No proxy environment, cookies, credentials, decompression or browser execution is
involved. Sources remain untrusted bytes for the parser.
"""

from __future__ import annotations

import http.client
import ipaddress
import math
import queue
import re
import socket
import ssl
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote, unquote_plus, urljoin, urlsplit, urlunsplit

from ruminate import __version__
from ruminate.errors import RuminateError

_DNS_SLOTS = threading.BoundedSemaphore(8)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_TRACKING_KEYS = {"fbclid", "gclid", "dclid", "msclkid"}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_MAX_URL_LENGTH = 8192


@dataclass(frozen=True)
class FetchedDocument:
    body: bytes
    original_url: str
    final_url: str
    content_type: str


@dataclass(frozen=True)
class _Address:
    family: int
    ip: str


def _canonical_host(host: str) -> str:
    if "%" in host:
        raise RuminateError("invalid_url", "Scoped or escaped hostnames are not supported")
    if host.endswith("."):
        host = host[:-1]
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise RuminateError("invalid_url", "Invalid URL hostname") from exc
    if len(ascii_host) > 253 or not all(_LABEL.fullmatch(part) for part in ascii_host.split(".")):
        raise RuminateError("invalid_url", "Invalid URL hostname")
    return ascii_host


def normalize_url(url: str) -> str:
    """Normalize identity without reordering or rewriting semantic query values."""
    if not url or len(url) > _MAX_URL_LENGTH:
        raise RuminateError("invalid_url", "URL is empty or exceeds the length limit")
    if "\\" in url or any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise RuminateError("invalid_url", "URL contains whitespace, controls or backslashes")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise RuminateError("unsupported_scheme", "Only HTTP and HTTPS URLs are allowed")
        if parts.username is not None or parts.password is not None:
            raise RuminateError("url_credentials", "Credentials in URLs are not allowed")
        if not parts.hostname:
            raise RuminateError("invalid_url", "URL must contain a hostname")
        host = _canonical_host(parts.hostname)
        expected_port = 443 if scheme == "https" else 80
        if parts.port is not None and parts.port != expected_port:
            raise RuminateError("blocked_port", "Only the default HTTP or HTTPS port is allowed")
    except ValueError as exc:
        raise RuminateError("invalid_url", "URL authority is invalid") from exc
    netloc = f"[{host}]" if ":" in host else host
    retained = []
    for part in parts.query.split("&"):
        key = unquote_plus(part.partition("=")[0]).lower()
        if key.startswith("utm_") or key in _TRACKING_KEYS:
            continue
        retained.append(part)
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote("&".join(retained), safe="/%?:@!$&'()*+,;=-._~")
    normalized = urlunsplit((scheme, netloc, path, query, ""))
    if len(normalized) > _MAX_URL_LENGTH:
        raise RuminateError("invalid_url", "Normalized URL exceeds the length limit")
    return normalized


def _public_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise RuminateError("blocked_address", "DNS returned an invalid network address") from exc
    if not address.is_global or any(
        (
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    ):
        raise RuminateError("blocked_address", "Only public unicast destinations are allowed")
    if isinstance(address, ipaddress.IPv4Address):
        # Conservative exclusions also cover older Python ipaddress classifications.
        if address in ipaddress.ip_network("192.0.0.0/24") or address in ipaddress.ip_network(
            "192.88.99.0/24"
        ):
            raise RuminateError("blocked_address", "Special-purpose destinations are not allowed")
    elif (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
        or address in ipaddress.ip_network("64:ff9b::/96")
        or address in ipaddress.ip_network("64:ff9b:1::/48")
    ):
        raise RuminateError("blocked_address", "IPv6 transition destinations are not allowed")
    return address


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuminateError("fetch_timeout", "URL retrieval exceeded its time limit")
    return remaining


def _resolve_addresses(host: str, port: int, deadline: float) -> tuple[_Address, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        checked = _public_ip(host)
        return (
            _Address(socket.AF_INET6 if checked.version == 6 else socket.AF_INET, str(checked)),
        )
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise RuminateError("blocked_address", "Local hostnames are not allowed")

    # getaddrinfo has no portable timeout. A bounded number of daemon resolver
    # workers may outlive a request, but a caller never waits beyond its deadline.
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise RuminateError("fetch_timeout", "DNS resolution capacity was unavailable")
    results: queue.Queue[
        Sequence[
            tuple[
                int, int, int, str, tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]
            ]
        ]
        | OSError
    ] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            results.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            results.put(exc)
        finally:
            _DNS_SLOTS.release()

    worker = threading.Thread(target=resolve, name="ruminate-dns", daemon=True)
    try:
        worker.start()
    except RuntimeError as exc:
        _DNS_SLOTS.release()
        raise RuminateError("dns_error", "DNS resolver could not start") from exc
    try:
        answer = results.get(timeout=_remaining(deadline))
    except queue.Empty as exc:
        raise RuminateError("fetch_timeout", "DNS resolution exceeded its time limit") from exc
    if isinstance(answer, OSError):
        raise RuminateError("dns_error", "Hostname resolution failed") from answer
    addresses: list[_Address] = []
    for family, _kind, _protocol, _canonical, endpoint in answer:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise RuminateError("blocked_address", "DNS returned an unsupported address family")
        if not isinstance(endpoint[0], str):
            raise RuminateError("blocked_address", "DNS returned an invalid network address")
        address = _public_ip(endpoint[0])
        if family == socket.AF_INET6 and len(endpoint) == 4 and endpoint[3] != 0:
            raise RuminateError("blocked_address", "Scoped IPv6 destinations are not allowed")
        expected_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        if family != expected_family:
            raise RuminateError("blocked_address", "DNS returned an inconsistent address family")
        resolved = _Address(family, str(address))
        if resolved not in addresses:
            addresses.append(resolved)
    if not addresses:
        raise RuminateError("dns_error", "Hostname resolved to no usable addresses")
    return tuple(addresses)


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: _Address, secure: bool, deadline: float):
        super().__init__(host, port=port, timeout=_remaining(deadline))
        self._address = address
        self._secure = secure
        self._deadline = deadline
        self._active_socket: socket.socket | None = None

    def connect(self) -> None:
        # Use a numeric sockaddr directly, never socket.create_connection(host)
        # or a second hostname lookup after public-address validation.
        raw = socket.socket(self._address.family, socket.SOCK_STREAM)
        self._active_socket = raw
        self.sock = raw
        try:
            raw.settimeout(_remaining(self._deadline))
            endpoint = (
                (self._address.ip, self.port, 0, 0)
                if self._address.family == socket.AF_INET6
                else (self._address.ip, self.port)
            )
            raw.connect(endpoint)
            if self._secure:
                context = ssl.create_default_context()
                context.set_alpn_protocols(["http/1.1"])
                wrapped = context.wrap_socket(
                    raw, server_hostname=self.host, do_handshake_on_connect=False
                )
                self._active_socket = wrapped
                self.sock = wrapped
                wrapped.settimeout(_remaining(self._deadline))
                wrapped.do_handshake()
            _remaining(self._deadline)
        except BaseException:
            self.abort()
            raise

    def interrupt(self) -> None:
        # shutdown also interrupts a response.makefile() read after http.client
        # has detached self.sock for a Connection: close response. Do not close
        # HTTPResponse concurrently with its reader: http.client is not thread-safe.
        sock = self._active_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def abort(self) -> None:
        self.interrupt()
        sock = self._active_socket
        if sock is not None:
            sock.close()
        self.close()


def _read_body(response: http.client.HTTPResponse, max_bytes: int, deadline: float) -> bytes:
    encoding = response.getheader("Content-Encoding", "identity").strip().lower()
    if encoding != "identity":
        raise RuminateError("unsupported_encoding", "Compressed responses are not accepted")
    length_header = response.getheader("Content-Length")
    expected_length = None
    if length_header is not None:
        if not length_header.isdecimal() or response.getheader("Transfer-Encoding") is not None:
            raise RuminateError("invalid_response", "Response has ambiguous body framing")
        if len(length_header) > 20:
            raise RuminateError("download_too_large", "Response exceeds the download size limit")
        expected_length = int(length_header)
        if expected_length > max_bytes:
            raise RuminateError("download_too_large", "Response exceeds the download size limit")
    chunks: list[bytes] = []
    size = 0
    while True:
        _remaining(deadline)
        chunk = response.read1(min(65536, max_bytes + 1 - size))
        _remaining(deadline)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise RuminateError("download_too_large", "Response exceeds the download size limit")
        chunks.append(chunk)
    if expected_length is not None and size != expected_length:
        raise RuminateError("invalid_response", "Response ended before its declared body length")
    if size == 0:
        raise RuminateError("empty_response", "Response contains no document body")
    return b"".join(chunks)


def _authorize(host: str, mode: str, allowed: bool, allowed_hosts: tuple[str, ...]) -> None:
    if mode == "off":
        raise RuminateError("network_disabled", "Network imports are disabled")
    if mode == "ask":
        if not allowed:
            raise RuminateError(
                "network_authorization_required", "URL import requires authorization"
            )
    elif mode == "allowlist":
        if host not in {_canonical_host(item) for item in allowed_hosts}:
            raise RuminateError("host_not_allowed", "Destination is outside the network allowlist")
    else:
        raise RuminateError("invalid_config", "Unknown network authorization mode")


def fetch_url(
    url: str,
    *,
    network_mode: str = "ask",
    network_allowed: bool = False,
    allowed_hosts: tuple[str, ...] = (),
    max_bytes: int = 20 * 1024 * 1024,
    timeout: float = 10.0,
    max_redirects: int = 5,
) -> FetchedDocument:
    """Retrieve public bytes under explicit authorization and a single deadline."""
    if max_bytes < 1 or not math.isfinite(timeout) or timeout <= 0 or not 0 <= max_redirects <= 10:
        raise RuminateError("invalid_config", "Invalid URL retrieval resource limits")
    deadline = time.monotonic() + timeout
    current = normalize_url(url)
    visited: set[str] = set()
    for redirect_count in range(max_redirects + 1):
        if current in visited:
            raise RuminateError("redirect_loop", "URL redirect loop detected")
        visited.add(current)
        parts = urlsplit(current)
        host = parts.hostname
        assert host is not None  # normalize_url requires a hostname.
        _authorize(host, network_mode, network_allowed, allowed_hosts)
        port = 443 if parts.scheme == "https" else 80
        addresses = _resolve_addresses(host, port, deadline)
        connection = _PinnedConnection(host, port, addresses[0], parts.scheme == "https", deadline)
        timer = threading.Timer(_remaining(deadline), connection.interrupt)
        timer.daemon = True
        timer.start()
        try:
            target = parts.path + ("?" + parts.query if parts.query else "")
            connection.request(
                "GET",
                target,
                headers={
                    "Host": f"[{host}]" if ":" in host else host,
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": f"Ruminate/{__version__} (local-first document import)",
                },
            )
            response = connection.getresponse()
            _remaining(deadline)
            if response.status in _REDIRECT_STATUSES:
                if redirect_count == max_redirects:
                    raise RuminateError("too_many_redirects", "URL exceeded the redirect limit")
                location = response.getheader("Location")
                if not location:
                    raise RuminateError("invalid_redirect", "Redirect contains no destination")
                destination = normalize_url(urljoin(current, location))
                if parts.scheme == "https" and urlsplit(destination).scheme != "https":
                    raise RuminateError("redirect_downgrade", "HTTPS redirects must remain HTTPS")
                current = destination
                continue
            if response.status not in {200, 203}:
                raise RuminateError(
                    "http_error", f"HTTP request failed with status {response.status}"
                )
            body = _read_body(response, max_bytes, deadline)
            content_type = response.getheader("Content-Type", "application/octet-stream")
            if len(content_type) > 512 or any(
                (ord(character) < 32 and character != "\t") or ord(character) == 127
                for character in content_type
            ):
                raise RuminateError("invalid_response", "Response has an invalid content type")
            # Charset parameters are needed to decode the original bytes. The
            # parser separately extracts the MIME type for persisted metadata.
            return FetchedDocument(body, url, current, content_type.strip())
        except RuminateError:
            raise
        except TimeoutError as exc:
            raise RuminateError("fetch_timeout", "URL retrieval exceeded its time limit") from exc
        except (OSError, http.client.HTTPException) as exc:
            if time.monotonic() >= deadline:
                raise RuminateError(
                    "fetch_timeout", "URL retrieval exceeded its time limit"
                ) from exc
            raise RuminateError("fetch_failed", "HTTP connection or response failed") from exc
        finally:
            timer.cancel()
            connection.abort()
    raise RuminateError("too_many_redirects", "URL exceeded the redirect limit")
