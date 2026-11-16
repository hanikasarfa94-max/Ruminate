"""Private PDF subprocess: resource limits precede importing the PDF engine."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict
from typing import Any

MAX_INPUT = 20 * 1024 * 1024
MAX_TEXT = 2_000_000
MAX_CONTENT_STREAM = 16 * 1024 * 1024


def _limits(seconds: int, memory_bytes: int) -> list[str]:
    warnings = []
    try:
        import resource
    except ImportError:
        return ["parser_os_resource_limits_unavailable"]
    for name, limit, value in (
        ("cpu", resource.RLIMIT_CPU, max(1, seconds)),
        ("memory", resource.RLIMIT_AS, memory_bytes),
    ):
        try:
            resource.setrlimit(limit, (value, value))
        except (OSError, ValueError):
            warnings.append(f"parser_os_{name}_limit_unavailable")
    return sorted(set(warnings))


def _watch_memory(memory_bytes: int) -> list[str]:
    """Self-RSS monitoring is a sampled budget, not a hard allocation boundary."""
    try:
        import resource
    except ImportError:
        return ["parser_rss_monitor_unavailable"]

    def check() -> None:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = peak if sys.platform == "darwin" else peak * 1024
        if peak_bytes > memory_bytes:
            # Use a dedicated exit code. Avoid racing writes to the JSON channel.
            os._exit(86)

    def monitor() -> None:
        while True:
            check()
            time.sleep(0.025)

    check()
    threading.Thread(target=monitor, daemon=True).start()
    return []


def process(data: bytes, max_pages: int, warnings: list[str]) -> dict[str, Any]:
    from pypdf import PdfReader, overwrite_configuration
    from pypdf.errors import LimitReachedError

    logging.disable(logging.CRITICAL)
    # Public pypdf 7 API: cap decoder allocation before get_data() returns an
    # expanded stream. The context is private to this single-request process.
    overwrite_configuration(
        maximum_declared_stream_length=MAX_CONTENT_STREAM,
        array_based_stream_maximum_output_length=MAX_CONTENT_STREAM,
        zlib_maximum_output_length=MAX_CONTENT_STREAM,
        lzw_maximum_output_length=MAX_CONTENT_STREAM,
        run_length_maximum_output_length=MAX_CONTENT_STREAM,
        jbig2_maximum_output_length=MAX_CONTENT_STREAM,
        brotli_maximum_output_length=MAX_CONTENT_STREAM,
        image_maximum_buffer_size=MAX_CONTENT_STREAM,
        stream_decoding_work_maximum_length=MAX_CONTENT_STREAM * 2,
        jbig2dec_binary=None,
    )
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return {"ok": False, "code": "pdf_encrypted"}
        if len(reader.pages) > max_pages:
            return {"ok": False, "code": "pdf_page_limit"}
        if not reader.pages:
            return {"ok": False, "code": "invalid_pdf"}
        pages: list[dict[str, Any]] = []
        total = 0
        needs_ocr = False
        warnings.extend(
            ["pdf_layout_and_formulas_not_verified", "pdf_drawing_content_not_extracted"]
        )
        for number, page in enumerate(reader.pages, 1):
            contents = page.get_contents()
            if contents and len(contents.get_data()) > MAX_CONTENT_STREAM:
                return {"ok": False, "code": "pdf_content_limit"}
            text = page.extract_text() or ""
            total += len(text)
            if total > MAX_TEXT:
                return {"ok": False, "code": "text_limit"}
            if not text.strip():
                needs_ocr = True
                warnings.append(f"pdf_page_{number}_needs_ocr_or_manual_review")
            resources = page.get("/Resources", {})
            if hasattr(resources, "get_object"):
                resources = resources.get_object()
            xobjects = resources.get("/XObject", {})
            if hasattr(xobjects, "get_object"):
                xobjects = xobjects.get_object()
            if any(obj.get_object().get("/Subtype") == "/Image" for obj in xobjects.values()):
                warnings.append(f"pdf_page_{number}_images_not_extracted")
            pages.append({"page": number, "text": text})
        metadata = reader.metadata
        title = str(metadata.title).strip()[:300] if metadata and metadata.title else None
        author = str(metadata.author).strip()[:300] if metadata and metadata.author else None
        return {
            "ok": True,
            "pages": pages,
            "metadata": {"title": title, "author": author},
            "warnings": sorted(set(warnings)),
            "status": "needs_ocr" if needs_ocr else "indexed",
        }
    except LimitReachedError:
        return {"ok": False, "code": "pdf_decode_limit"}
    except MemoryError:
        return {"ok": False, "code": "pdf_resource_limit"}
    except Exception:
        return {"ok": False, "code": "invalid_pdf"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--kind", choices=["pdf", "html"], default="pdf")
    parser.add_argument("--filename", default="article.html")
    parser.add_argument("--content-type", default="text/html")
    parser.add_argument("--max-rss-bytes", type=int, default=512 * 1024 * 1024)
    args = parser.parse_args()
    warnings = _limits(args.cpu_seconds, args.max_rss_bytes)
    warnings.extend(_watch_memory(args.max_rss_bytes))
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        result: dict[str, Any] = {"ok": False, "code": "source_too_large"}
    elif args.kind == "pdf":
        result = process(data, args.max_pages, warnings)
    else:
        from ruminate.errors import RuminateError
        from ruminate.ingestion.parsers import _parse_html_direct

        try:
            document = _parse_html_direct(data, args.filename, args.content_type)
            document.warnings = list(dict.fromkeys([*document.warnings, *warnings]))
            result = {"ok": True, "document": asdict(document)}
        except RuminateError as exc:
            result = {"ok": False, "code": exc.code}
        except MemoryError:
            result = {"ok": False, "code": "html_resource_limit"}
        except Exception:
            result = {"ok": False, "code": "invalid_html"}
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 16 * 1024 * 1024:
        encoded = json.dumps({"ok": False, "code": "text_limit"})
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
