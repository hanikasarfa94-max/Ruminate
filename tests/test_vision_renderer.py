"""Real bounded rasterization; image content is never claimed to be verified OCR."""

from __future__ import annotations

import io
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from ruminate.errors import RuminateError
from ruminate.ingestion import vision
from ruminate.ingestion.parsers import parse_document
from ruminate.ingestion.vision import render_pdf_page

FIXTURES = Path(__file__).parent / "fixtures"


def test_image_only_page_has_real_pixels_and_no_native_text() -> None:
    raw = (FIXTURES / "vision_scan.pdf").read_bytes()
    native = parse_document(raw, filename="scan.pdf")
    assert native.status == "needs_ocr"
    assert native.blocks == []
    assert PdfReader(io.BytesIO(raw)).pages[0].extract_text() == ""
    result = render_pdf_page(raw, page=1)
    assert result.page == result.page_count == 1
    assert result.renderer.startswith("pypdfium2/")
    assert "rendering_does_not_verify_transcription" in result.warnings
    assert len(result.png) < vision.MAX_IMAGE_BYTES
    with Image.open(io.BytesIO(result.png)) as image:
        assert image.size == (result.width, result.height) == (1000, 1300)
        # Check that both actual chart regions survive rasterization. A blank
        # placeholder PNG or metadata-only result cannot pass this assertion.
        orchard = image.convert("RGB").getpixel((300, 800))
        harbor = image.convert("RGB").getpixel((650, 800))
        assert orchard[2] > orchard[0] + 50
        assert harbor[0] > harbor[2] + 50


def test_page_selection_is_one_based_and_preserves_original_bytes() -> None:
    raw = (FIXTURES / "single_column.pdf").read_bytes()
    original = bytes(raw)
    first = render_pdf_page(raw, page=1)
    second = render_pdf_page(raw, page=2)
    assert (first.page, second.page, second.page_count) == (1, 2, 2)
    assert first.png != second.png
    assert raw == original
    assert render_pdf_page(raw, page=2).png == second.png


def test_large_page_dimensions_reduce_resolution_before_allocating_bitmap() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=500_000, height=250_000)
    stream = io.BytesIO()
    writer.write(stream)
    result = render_pdf_page(stream.getvalue(), page=1)
    assert result.width <= vision.MAX_DIMENSION
    assert result.height <= vision.MAX_DIMENSION
    assert "page_resolution_reduced_to_fit_budget" in result.warnings


@pytest.mark.parametrize("page", [0, -1, True, 1.5])
def test_invalid_page_numbers_fail_before_worker(page: int) -> None:
    with pytest.raises(RuminateError) as exc:
        render_pdf_page((FIXTURES / "single_column.pdf").read_bytes(), page=page)
    assert exc.value.code == "invalid_page"


def test_missing_page_and_document_page_limit_are_explained() -> None:
    raw = (FIXTURES / "single_column.pdf").read_bytes()
    with pytest.raises(RuminateError) as exc:
        render_pdf_page(raw, page=3)
    assert exc.value.code == "invalid_page"
    with pytest.raises(RuminateError) as exc:
        render_pdf_page(raw, page=1, max_pages=1)
    assert exc.value.code == "pdf_page_limit"


@pytest.mark.parametrize("password", ["synthetic-password", ""])
def test_encrypted_pdf_is_refused_even_if_empty_password_would_open(password: str) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt(password, owner_password="synthetic-owner")
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(RuminateError) as exc:
        render_pdf_page(stream.getvalue(), page=1)
    assert exc.value.code == "pdf_encrypted"


@pytest.mark.parametrize("raw", [b"not a PDF", (FIXTURES / "malformed.pdf").read_bytes()])
def test_malformed_input_returns_stable_error(raw: bytes) -> None:
    with pytest.raises(RuminateError) as exc:
        render_pdf_page(raw, page=1)
    assert exc.value.code == "invalid_pdf"


def test_input_budget_precedes_worker() -> None:
    with pytest.raises(RuminateError) as exc:
        render_pdf_page(b"%PDF-" + bytes(vision.MAX_INPUT_BYTES), page=1)
    assert exc.value.code == "source_too_large"


def test_real_render_timeout() -> None:
    with pytest.raises(RuminateError) as exc:
        render_pdf_page((FIXTURES / "vision_scan.pdf").read_bytes(), page=1, timeout=0.001)
    assert exc.value.code == "render_timeout"


@pytest.mark.skipif(sys.platform == "win32", reason="RSS monitoring uses POSIX resource")
def test_memory_watchdog_stops_worker_above_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "MAX_RSS_BYTES", 1)
    with pytest.raises(RuminateError) as exc:
        render_pdf_page((FIXTURES / "vision_scan.pdf").read_bytes(), page=1)
    assert exc.value.code == "render_memory_limit"


def test_parallel_calls_use_independent_pdfium_processes() -> None:
    raw = (FIXTURES / "single_column.pdf").read_bytes()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pages = list(pool.map(lambda number: render_pdf_page(raw, page=number), [1, 2]))
    assert [page.page for page in pages] == [1, 2]
    assert pages[0].png != pages[1].png
