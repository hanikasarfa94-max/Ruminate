"""Render an explicitly selected PDF page for a host's visual inspection.

This module does not perform OCR, infer claims, or call a model. Rendering runs
in a fresh process: PDFium is not thread safe, and input documents are untrusted.
The process has resource budgets; it is not an operating-system security sandbox.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import subprocess
import sys
from dataclasses import dataclass

from ruminate.errors import RuminateError

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 1800
MAX_RSS_BYTES = 512 * 1024 * 1024
MAX_WIRE_BYTES = (MAX_IMAGE_BYTES * 4 // 3) + 8192


@dataclass(frozen=True)
class RenderedPage:
    png: bytes
    width: int
    height: int
    page: int
    page_count: int
    renderer: str
    warnings: list[str]


def render_pdf_page(
    data: bytes,
    *,
    page: int,
    max_pages: int = 500,
    timeout: float = 10.0,
) -> RenderedPage:
    """Return a bounded PNG of a one-based page, without changing the source."""
    if type(page) is not int or page < 1:
        raise RuminateError("invalid_page", "Page must be a positive one-based integer")
    if type(max_pages) is not int or max_pages < 1:
        raise RuminateError("invalid_limit", "Maximum page count must be a positive integer")
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise RuminateError("invalid_limit", "Render timeout must be between 0 and 60 seconds")
    if len(data) > MAX_INPUT_BYTES:
        raise RuminateError("source_too_large", "PDF exceeds the rendering input budget")
    if b"%PDF-" not in data[:1024]:
        raise RuminateError("invalid_pdf", "Rendering requires a PDF document")
    command = [
        sys.executable,
        "-m",
        "ruminate.ingestion.vision_worker",
        "--page",
        str(page),
        "--max-pages",
        str(max_pages),
        "--cpu-seconds",
        str(max(1, math.ceil(timeout))),
        "--max-rss-bytes",
        str(MAX_RSS_BYTES),
    ]
    try:
        process = subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuminateError("render_timeout", "Page rendering exceeded its time budget") from exc
    if process.returncode == 86:
        raise RuminateError("render_memory_limit", "Page rendering exceeded its memory budget")
    if process.returncode != 0:
        raise RuminateError("render_resource_limit", "Page renderer stopped before completing")
    if len(process.stdout) > MAX_WIRE_BYTES:
        raise RuminateError("render_output_limit", "Page image exceeds the output budget")
    try:
        payload = json.loads(process.stdout)
        if not isinstance(payload, dict):
            raise ValueError("Invalid worker envelope")
        if not payload.get("ok"):
            code = payload.get("code", "invalid_pdf")
            if code not in {
                "invalid_pdf", "invalid_page", "pdf_page_limit", "pdf_encrypted",
                "render_dimensions", "render_output_limit", "render_memory_limit",
                "renderer_unavailable", "source_too_large",
            }:
                code = "invalid_pdf"
            raise RuminateError(code, "PDF page could not be rendered safely")
        png = base64.b64decode(payload["png"], validate=True)
        width, height = payload["width"], payload["height"]
        count = payload["page_count"]
        if (
            not png.startswith(b"\x89PNG\r\n\x1a\n")
            or len(png) > MAX_IMAGE_BYTES
            or type(width) is not int
            or type(height) is not int
            or not 1 <= width <= MAX_DIMENSION
            or not 1 <= height <= MAX_DIMENSION
            or type(count) is not int
            or not page <= count <= max_pages
            or payload["page"] != page
            or not isinstance(payload["renderer"], str)
            or not isinstance(payload["warnings"], list)
            or not all(isinstance(value, str) for value in payload["warnings"])
        ):
            raise ValueError("Invalid page result")
        return RenderedPage(
            png=png,
            width=width,
            height=height,
            page=page,
            page_count=count,
            renderer=payload["renderer"],
            warnings=payload["warnings"],
        )
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise RuminateError("invalid_render_result", "Page renderer returned invalid data") from exc
