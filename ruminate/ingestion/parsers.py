"""Pure document parsing with exact text spans and a bounded PDF subprocess.

No parser follows hyperlinks, executes source content, or calls a model. HTML
locators refer to the saved snapshot, never to a subsequently updated webpage.
"""

from __future__ import annotations

import bisect
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lxml import html  # type: ignore[import-untyped]
from trafilatura import extract

from ruminate.errors import RuminateError

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MAX_BLOCK_CHARS = 5000
MAX_BLOCKS = 10_000
MAX_RSS_KIB = 512 * 1024


def _worker(command: list[str], data: bytes, timeout: float) -> subprocess.CompletedProcess[bytes]:
    """Bound wall time; the worker monitors its own RSS in a watchdog thread."""
    command = [*command, "--max-rss-bytes", str(MAX_RSS_KIB * 1024)]
    try:
        result = subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuminateError("parse_timeout", "Parsing exceeded its time budget") from exc
    if result.returncode == 86:
        raise RuminateError("parse_memory_limit", "Parsing exceeded its memory budget")
    return result


@dataclass
class ParsedBlock:
    text: str
    locator: dict[str, Any]
    page: int | None = None
    section: str | None = None
    quality: str = "native_text"


@dataclass
class ParsedDocument:
    title: str
    mime_type: str
    blocks: list[ParsedBlock]
    authors: list[str] = field(default_factory=list)
    published_at: str | None = None
    warnings: list[str] = field(default_factory=list)
    status: str = "indexed"
    canonical_url: str | None = None


def _fallback_title(filename: str) -> str:
    # Input can be a local basename or a URL; titles never include URL queries.
    path = urlsplit(filename).path if "://" in filename else filename
    return Path(path).name[:300] or "Untitled document"


def _text(data: bytes, content_type: str | None = None) -> tuple[str, str]:
    charset = "utf-8-sig"
    if content_type and (match := re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)):
        requested = match[1].lower()
        accepted = {"utf-8", "utf8", "utf-8-sig", "iso-8859-1", "windows-1252"}
        if requested not in accepted:
            raise RuminateError("unsupported_encoding", "Document encoding is not supported")
        charset = requested
    try:
        value = data.decode(charset)
    except UnicodeDecodeError as exc:
        raise RuminateError("unsupported_encoding", "Document is not valid supported text") from exc
    if "\x00" in value:
        raise RuminateError("unsupported_format", "Binary content is not supported as text")
    if len(value) > MAX_TEXT_CHARS:
        raise RuminateError("text_limit", "Extracted text exceeds the configured text budget")
    return value, charset


def _split_span(
    text: str,
    start: int,
    end: int,
    *,
    locator: dict[str, Any],
    section: str | None = None,
    page: int | None = None,
    quality: str = "native_text",
    line_starts: list[int] | None = None,
) -> list[ParsedBlock]:
    raw = text[start:end]
    start += len(raw) - len(raw.lstrip())
    end -= len(raw) - len(raw.rstrip())
    blocks = []
    for offset in range(start, end, MAX_BLOCK_CHARS):
        stop = min(end, offset + MAX_BLOCK_CHARS)
        position = {**locator, "char_start": offset, "char_end": stop}
        if line_starts is not None:
            position["start_line"] = bisect.bisect_right(line_starts, offset)
            position["end_line"] = bisect.bisect_right(line_starts, max(offset, stop - 1))
        blocks.append(ParsedBlock(text[offset:stop], position, page, section, quality))
    return blocks


def _parse_text(
    data: bytes,
    filename: str,
    *,
    markdown: bool,
    timeout: float,
) -> ParsedDocument:
    deadline = time.monotonic() + timeout

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise RuminateError("parse_timeout", "Text parsing exceeded its time budget")

    text, encoding = _text(data)
    lines = text.splitlines(keepends=True)
    line_starts: list[int] = []
    cursor = 0
    for index, line in enumerate(lines):
        if index % 256 == 0:
            check_deadline()
        line_starts.append(cursor)
        cursor += len(line)
    blocks: list[ParsedBlock] = []
    start: int | None = None
    section: str | None = None
    title: str | None = None
    fenced: str | None = None

    def flush(stop: int) -> None:
        nonlocal start
        if start is not None:
            blocks.extend(
                _split_span(
                    text,
                    start,
                    stop,
                    locator={"type": "text_lines", "encoding": encoding},
                    section=section,
                    line_starts=line_starts,
                )
            )
            start = None
            if len(blocks) > MAX_BLOCKS:
                raise RuminateError("text_limit", "Document has too many text blocks")

    for index, line in enumerate(lines):
        if index % 128 == 0:
            check_deadline()
        offset = line_starts[index]
        heading = (
            re.match(r"^ {0,3}#{1,6}(?:[ \t]|$)", line.rstrip("\r\n"))
            if markdown and not fenced
            else None
        )
        if heading:
            flush(offset)
            # Linear suffix handling avoids regex backtracking on long runs of
            # spaces. A closing hash run is syntax only when preceded by space.
            body = line[heading.end() :].strip()
            without_hashes = body.rstrip("#")
            if without_hashes and without_hashes[-1].isspace():
                body = without_hashes.rstrip()
            section = " ".join(body.split())[:300] or None
            title = title or section
            blocks.extend(
                _split_span(
                    text,
                    offset,
                    offset + len(line),
                    locator={"type": "text_lines", "encoding": encoding},
                    section=section,
                    line_starts=line_starts,
                )
            )
            if len(blocks) > MAX_BLOCKS:
                raise RuminateError("text_limit", "Document has too many text blocks")
            continue
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line) if markdown else None
        if fence:
            if fenced is None:
                fenced = fence[1]
            elif (
                fence[1][0] == fenced[0]
                and len(fence[1]) >= len(fenced)
                and not line[fence.end() :].strip()
            ):
                fenced = None
        if not line.strip() and fenced is None:
            flush(offset)
        elif start is None:
            start = offset
    check_deadline()
    flush(len(text))
    if not blocks:
        raise RuminateError("empty_document", "Document contains no text")
    return ParsedDocument(
        title or _fallback_title(filename), "text/markdown" if markdown else "text/plain", blocks
    )


def _parse_pdf(data: bytes, filename: str, max_pages: int, timeout: float) -> ParsedDocument:
    command = [
        sys.executable,
        "-m",
        "ruminate.ingestion.pdf_worker",
        "--max-pages",
        str(max_pages),
        "--cpu-seconds",
        str(max(1, int(timeout))),
    ]
    process = _worker(command, data, timeout)
    if process.returncode != 0:
        raise RuminateError("pdf_resource_limit", "PDF worker stopped before completing safely")
    if len(process.stdout) > 16 * 1024 * 1024:
        raise RuminateError("text_limit", "PDF extraction exceeded its output budget")
    try:
        payload = json.loads(process.stdout)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuminateError("invalid_pdf", "PDF worker returned no valid extraction") from exc
    if not payload.get("ok"):
        raise RuminateError(
            str(payload.get("code", "invalid_pdf")), "PDF could not be parsed safely"
        )
    blocks: list[ParsedBlock] = []
    for item in payload["pages"]:
        page_text = item["text"]
        blocks.extend(
            _split_span(
                page_text,
                0,
                len(page_text),
                locator={"type": "pdf_page", "page": item["page"], "bbox": None},
                page=item["page"],
            )
        )
        if len(blocks) > MAX_BLOCKS:
            raise RuminateError("text_limit", "PDF has too many text blocks")
    metadata = payload["metadata"]
    return ParsedDocument(
        title=metadata.get("title") or _fallback_title(filename),
        mime_type="application/pdf",
        blocks=blocks,
        authors=[metadata["author"]] if metadata.get("author") else [],
        warnings=payload["warnings"],
        status=payload["status"],
    )


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _parse_html_direct(data: bytes, filename: str, content_type: str | None) -> ParsedDocument:
    text, encoding = _text(data, content_type)
    # Encoding has already been decoded above. lxml rejects a Unicode string
    # carrying an XML encoding declaration; removing only that declaration keeps
    # XHTML element text and snapshot XPath locations unchanged.
    text = re.sub(r"\A\s*<\?xml\s[^?]*\?>", "", text, count=1, flags=re.I)
    try:
        parser = html.HTMLParser(no_network=True, recover=True, huge_tree=False)
        tree = html.fromstring(text, parser=parser)
    except Exception as exc:
        # lxml exceptions are not stable public API types across its builds.
        raise RuminateError("invalid_html", "HTML could not be parsed") from exc
    visible_tree = html.fromstring(text, parser=parser)
    for element in visible_tree.xpath("//script|//style|//noscript"):
        element.drop_tree()
    visible = _normalized(visible_tree.text_content())
    title_nodes = tree.xpath("//title/text()") or tree.xpath("//h1/text()")
    title = _normalized(str(title_nodes[0]))[:300] if title_nodes else _fallback_title(filename)
    locked = tree.xpath("//input[translate(@type, 'PASSWORD', 'password')='password']")
    auth_title = re.search(
        r"\b(log ?in|sign ?in|access denied|subscribe to (read|continue))\b|登录|请先登录",
        title,
        re.I,
    )
    paywall = tree.xpath("//*[@itemprop='isAccessibleForFree'][@content='false']")
    if locked or auth_title or paywall:
        raise RuminateError("access_restricted", "Page requires login or subscription")
    try:
        extracted = extract(
            text,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            include_links=False,
            include_formatting=False,
            with_metadata=False,
            favor_precision=True,
            date_extraction_params={"extensive_search": False},
        )
    except Exception as exc:
        raise RuminateError("invalid_html", "HTML body extraction failed") from exc
    if not isinstance(extracted, str) or len(extracted.strip()) < 40:
        if tree.xpath("//script") or re.search(
            r"enable javascript|请开启.*javascript", visible, re.I
        ):
            raise RuminateError("needs_browser", "Page has no usable static article body")
        raise RuminateError("empty_document", "Page has no usable article body")
    if len(extracted) > MAX_TEXT_CHARS:
        raise RuminateError("text_limit", "HTML extraction exceeds its text budget")
    selected = _normalized(extracted)
    blocks: list[ParsedBlock] = []
    # Each locator names a real node in the saved HTML's lxml parse tree. Avoid
    # nested paragraph/list/pre/table duplication by selecting only outer nodes.
    nodes = tree.xpath("//p|//h1|//h2|//h3|//h4|//h5|//h6|//pre|//table|//li")
    chosen: set[Any] = set()
    for node in nodes:
        if any(parent in chosen for parent in node.iterancestors()):
            continue
        value = str(node.text_content())
        normalized = _normalized(value)
        if not normalized or normalized not in selected:
            continue
        chosen.add(node)
        xpath = str(tree.getroottree().getpath(node))
        blocks.extend(
            _split_span(
                value,
                0,
                len(value),
                locator={
                    "type": "html_xpath",
                    "xpath": xpath,
                    "text_basis": "element_text_content",
                    "encoding": encoding,
                },
                quality="html_text",
            )
        )
        if len(blocks) > MAX_BLOCKS:
            raise RuminateError("text_limit", "HTML has too many text blocks")
    warnings: list[str] = []
    if not blocks:
        blocks.extend(
            _split_span(
                extracted,
                0,
                len(extracted),
                locator={
                    "type": "html_extracted_text",
                    "text_basis": "trafilatura_body",
                    "encoding": encoding,
                },
                quality="html_text",
            )
        )
        warnings.append("html_dom_locator_unavailable")
    else:
        mapped = " ".join(_normalized(block.text) for block in blocks)
        cursor = 0
        for line in extracted.splitlines(keepends=True):
            if _normalized(line) and _normalized(line) not in mapped:
                blocks.extend(
                    _split_span(
                        extracted,
                        cursor,
                        cursor + len(line),
                        locator={
                            "type": "html_extracted_text",
                            "text_basis": "trafilatura_body",
                            "encoding": encoding,
                        },
                        quality="html_text",
                    )
                )
                warnings.append("html_partial_dom_mapping")
                if len(blocks) > MAX_BLOCKS:
                    raise RuminateError("text_limit", "HTML has too many text blocks")
            cursor += len(line)
    warnings.append("html_images_and_dynamic_content_not_extracted")
    authors = [
        _normalized(str(value))[:300]
        for value in tree.xpath("//meta[translate(@name,'AUTHOR','author')='author']/@content")
        if str(value).strip()
    ]
    dates = tree.xpath(
        "//meta[@property='article:published_time']/@content|"
        "//meta[@name='datePublished']/@content|//time[@itemprop='datePublished']/@datetime"
    )
    published_at: str | None = None
    for candidate in dates:
        candidate = str(candidate).strip()
        if len(candidate) > 80:
            continue
        try:
            datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        published_at = candidate
        break
    canonical = tree.xpath(
        "//link[contains(concat(' ', normalize-space(@rel), ' '), ' canonical ')]/@href"
    )
    canonical_url = str(canonical[0]).strip() if canonical else None
    if canonical_url:
        try:
            if len(canonical_url) > 8192 or urlsplit(canonical_url).scheme not in {"http", "https"}:
                canonical_url = None
        except ValueError:
            canonical_url = None
    return ParsedDocument(
        title,
        "text/html",
        blocks,
        authors=list(dict.fromkeys(authors))[:50],
        published_at=published_at,
        warnings=list(dict.fromkeys(warnings)),
        canonical_url=canonical_url,
    )


def _parse_html(
    data: bytes,
    filename: str,
    content_type: str | None,
    timeout: float,
) -> ParsedDocument:
    command = [
        sys.executable,
        "-m",
        "ruminate.ingestion.pdf_worker",
        "--kind",
        "html",
        "--max-pages",
        "1",
        "--cpu-seconds",
        str(max(1, int(timeout))),
        "--filename",
        _fallback_title(filename),
        "--content-type",
        content_type or "text/html",
    ]
    process = _worker(command, data, timeout)
    if process.returncode != 0:
        raise RuminateError("html_resource_limit", "HTML worker stopped before completing safely")
    if len(process.stdout) > 16 * 1024 * 1024:
        raise RuminateError("text_limit", "HTML extraction exceeded its output budget")
    try:
        payload = json.loads(process.stdout)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuminateError("invalid_html", "HTML worker returned no valid extraction") from exc
    if not payload.get("ok"):
        raise RuminateError(
            str(payload.get("code", "invalid_html")), "HTML could not be parsed safely"
        )
    document = payload["document"]
    document["blocks"] = [ParsedBlock(**block) for block in document["blocks"]]
    return ParsedDocument(**document)


def parse_document(
    data: bytes,
    *,
    filename: str,
    content_type: str | None = None,
    max_pages: int = 500,
    timeout: float = 10.0,
) -> ParsedDocument:
    """Parse a bounded, already-authorized byte snapshot without following links."""
    if not data:
        raise RuminateError("empty_document", "Document is empty")
    if len(data) > MAX_INPUT_BYTES:
        raise RuminateError("source_too_large", "Document exceeds the parser input budget")
    if max_pages < 1 or timeout <= 0:
        raise RuminateError("invalid_limits", "Parser limits must be positive")
    path = urlsplit(filename).path if "://" in filename else filename
    suffix = Path(path).suffix.lower()
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    head = data.removeprefix(b"\xef\xbb\xbf")[:2048].lstrip().lower()
    if data.startswith(b"%PDF-"):
        return _parse_pdf(data, filename, max_pages, timeout)
    if suffix == ".pdf" or mime == "application/pdf":
        raise RuminateError("format_mismatch", "Declared PDF has no PDF signature")
    if data.startswith(
        (b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff", b"\x7fELF", b"MZ", b"\x1f\x8b")
    ):
        raise RuminateError("unsupported_format", "Binary document format is not supported")
    # Markdown and plain text can intentionally contain complete HTML examples.
    # Their declaration wins over HTML's ambiguous textual signature; a genuine
    # binary/PDF signature has already been handled above.
    if suffix in {".md", ".markdown"} or mime == "text/markdown":
        return _parse_text(data, filename, markdown=True, timeout=timeout)
    if suffix == ".txt" or mime == "text/plain":
        return _parse_text(data, filename, markdown=False, timeout=timeout)
    html_signature = re.match(
        rb"(?:(?:<\?xml[^?]*\?>|<!--.*?-->)\s*)*"
        rb"<(?:!doctype\s+html|html|head|body|article)(?:\s|>)",
        head,
        re.S,
    )
    if (
        html_signature
        or suffix in {".html", ".htm"}
        or mime in {"text/html", "application/xhtml+xml"}
    ):
        return _parse_html(data, filename, content_type, timeout)
    raise RuminateError("unsupported_format", "Expected PDF, Markdown, plain text or static HTML")
