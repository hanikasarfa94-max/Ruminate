"""Real synthetic PDF/Markdown/HTML golden cases; source text is never executed."""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import pytest
from lxml import html
from pypdf import PdfReader, PdfWriter

from ruminate.errors import RuminateError
from ruminate.ingestion import parsers
from ruminate.ingestion.parsers import parse_document

FIXTURES = Path(__file__).parent / "fixtures"


def parse_fixture(name: str, **kwargs: object) -> parsers.ParsedDocument:
    return parse_document((FIXTURES / name).read_bytes(), filename=name, **kwargs)


def test_g01_native_pdf_preserves_real_pages_and_metadata() -> None:
    document = parse_fixture("single_column.pdf")
    assert document.mime_type == "application/pdf"
    assert document.title == "Synthetic Evidence"
    assert document.authors == ["Ruminate Test Fixture"]
    assert document.published_at is None
    assert [block.page for block in document.blocks] == [1, 2]
    assert "evaluation protocol" in document.blocks[1].text
    reader = PdfReader(FIXTURES / "single_column.pdf")
    for block in document.blocks:
        assert block.page is not None
        raw = reader.pages[block.page - 1].extract_text()
        assert raw[block.locator["char_start"] : block.locator["char_end"]] == block.text
        assert block.locator["page"] == block.page


def test_g02_columns_and_chart_keep_page_and_warn_about_missing_visuals() -> None:
    document = parse_fixture("multi_column_chart.pdf")
    assert document.blocks[0].page == 1
    assert "Left column" in document.blocks[0].text
    assert "Right column" in document.blocks[0].text
    assert "pdf_page_1_images_not_extracted" in document.warnings
    assert "pdf_layout_and_formulas_not_verified" in document.warnings
    assert "pdf_drawing_content_not_extracted" in document.warnings


@pytest.mark.parametrize("name", ["scanned.pdf", "empty_page.pdf"])
def test_g03_image_only_and_blank_pages_do_not_claim_text_extraction(name: str) -> None:
    document = parse_fixture(name)
    assert document.status == "needs_ocr"
    assert document.blocks == []
    assert "pdf_page_1_needs_ocr_or_manual_review" in document.warnings


def test_g04_static_html_metadata_and_snapshot_xpath_are_reproducible() -> None:
    document = parse_fixture("article.html")
    assert document.title == "Synthetic evidence study"
    assert document.published_at == "2026-10-01"
    assert document.authors == ["Ruminate Test Fixture"]
    assert document.canonical_url == "https://example.org/synthetic-evidence"
    tree = html.fromstring((FIXTURES / "article.html").read_text())
    assert len(document.blocks) >= 4
    for block in document.blocks:
        assert block.page is None
        assert block.locator["type"] == "html_xpath"
        element = tree.xpath(block.locator["xpath"])[0]
        original = element.text_content()
        assert original[block.locator["char_start"] : block.locator["char_end"]] == block.text
    assert all("Home | Contact | Menu" not in block.text for block in document.blocks)


@pytest.mark.parametrize(
    "name, code",
    [
        ("login.html", "access_restricted"),
        ("dynamic.html", "needs_browser"),
    ],
)
def test_g05_unavailable_article_body_has_explainable_failure(name: str, code: str) -> None:
    with pytest.raises(RuminateError) as error:
        parse_fixture(name)
    assert error.value.code == code


@pytest.mark.parametrize("name", ["injected.pdf", "article.html"])
def test_g08_hostile_instructions_are_retained_as_source_text(name: str, tmp_path: Path) -> None:
    document = parse_fixture(name)
    assert "DO_NOT_EXECUTE" in "\n".join(block.text for block in document.blocks)
    assert not list(tmp_path.iterdir())


def test_markdown_unicode_line_and_char_locations_preserve_exact_source() -> None:
    raw = (FIXTURES / "notes.md").read_text()
    document = parse_fixture("notes.md")
    assert document.title == "电商 Agent 研究笔记"
    for block in document.blocks:
        assert raw[block.locator["char_start"] : block.locator["char_end"]] == block.text
        lines = raw.splitlines()
        located = "\n".join(lines[block.locator["start_line"] - 1 : block.locator["end_line"]])
        assert block.text in located
    table = next(block for block in document.blocks if "| 材料" in block.text)
    assert "合成白皮书" in table.text
    code = next(block for block in document.blocks if "This is code" in block.text)
    assert code.section == "待验证的假设"


def test_plain_text_crlf_and_large_paragraph_splits_preserve_offsets() -> None:
    raw = "First paragraph.\r\n\r\n" + "证据" * 6000
    document = parse_document(raw.encode(), filename="notes.txt")
    assert len(document.blocks) == 4
    assert all(len(block.text) <= 5000 for block in document.blocks)
    for block in document.blocks:
        assert raw[block.locator["char_start"] : block.locator["char_end"]] == block.text
    assert document.blocks[-1].locator["start_line"] == 3
    assert "".join(block.text for block in document.blocks[1:]) == "证据" * 6000


def test_missing_html_date_stays_unknown_and_canonical_is_not_executed() -> None:
    raw = (FIXTURES / "article.html").read_text()
    raw = raw.replace('<meta property="article:published_time" content="2026-10-01">', "")
    raw = raw.replace("https://example.org/synthetic-evidence", "javascript:DO_NOT_EXECUTE")
    document = parse_document(raw.encode(), filename="article.html")
    assert document.published_at is None
    assert document.canonical_url is None


def test_g13_pdf_page_limit() -> None:
    with pytest.raises(RuminateError) as error:
        parse_fixture("too_many_pages.pdf", max_pages=2)
    assert error.value.code == "pdf_page_limit"


def test_compressed_pdf_stream_is_stopped_by_decoder_before_full_expansion() -> None:
    # pdf_decode_limit comes from pypdf's LimitReachedError, not the post-decode
    # length check or a process timeout. Input is tiny; decoded content is >16 MiB.
    fixture = FIXTURES / "compressed_budget.pdf"
    assert fixture.stat().st_size < 20 * 1024
    with pytest.raises(RuminateError) as error:
        parse_fixture(fixture.name)
    assert error.value.code == "pdf_decode_limit"


def test_malformed_and_disguised_pdf_have_stable_errors() -> None:
    with pytest.raises(RuminateError) as error:
        parse_fixture("malformed.pdf")
    assert error.value.code == "invalid_pdf"
    with pytest.raises(RuminateError) as error:
        parse_document(b"<html>not a PDF</html>", filename="report.pdf")
    assert error.value.code == "format_mismatch"


def test_pdf_signature_routes_correctly_despite_wrong_suffix() -> None:
    document = parse_document(
        (FIXTURES / "single_column.pdf").read_bytes(), filename="download.txt"
    )
    assert document.mime_type == "application/pdf"
    assert document.blocks[1].page == 2


def test_encrypted_pdf_is_not_silently_read() -> None:
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt("synthetic-test-password")
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(RuminateError) as error:
        parse_document(stream.getvalue(), filename="encrypted.pdf")
    assert error.value.code == "pdf_encrypted"


@pytest.mark.parametrize("filename", ["single_column.pdf", "article.html"])
def test_real_subprocess_wall_timeout(filename: str) -> None:
    with pytest.raises(RuminateError) as error:
        parse_fixture(filename, timeout=0.001)
    assert error.value.code == "parse_timeout"


@pytest.mark.skipif(sys.platform == "win32", reason="RSS monitor uses POSIX resource module")
def test_rss_monitor_stops_worker_above_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parsers, "MAX_RSS_KIB", 1)
    with pytest.raises(RuminateError) as error:
        parse_fixture("single_column.pdf")
    assert error.value.code == "parse_memory_limit"


def test_size_encoding_and_format_limits() -> None:
    for data, name, expected in [
        (b"x" * (parsers.MAX_INPUT_BYTES + 1), "large.txt", "source_too_large"),
        (b"\x89PNG\r\n", "fake.md", "unsupported_format"),
        (b"\xff\xfe", "unsupported.txt", "unsupported_encoding"),
        (b"", "empty.txt", "empty_document"),
    ]:
        with pytest.raises(RuminateError) as error:
            parse_document(data, filename=name)
        assert error.value.code == expected


def test_many_tiny_markdown_blocks_are_bounded() -> None:
    with pytest.raises(RuminateError) as error:
        parse_document(("a\n\n" * (parsers.MAX_BLOCKS + 1)).encode(), filename="many.md")
    assert error.value.code == "text_limit"


def test_markdown_html_code_sample_does_not_change_parser_or_lose_prose() -> None:
    raw = (
        "# Real Markdown Title\n\nThis is a note about HTML.\n\n```html\n"
        "<html><body><p>This paragraph is an HTML example and must remain code.</p></body></html>\n"
        "```\n\nFinal note.\n"
    )
    document = parse_document(raw.encode(), filename="note.md")
    assert document.mime_type == "text/markdown"
    assert document.title == "Real Markdown Title"
    assert "Final note." in document.blocks[-1].text
    assert any("```html" in block.text and "<html>" in block.text for block in document.blocks)
    for block in document.blocks:
        assert raw[block.locator["char_start"] : block.locator["char_end"]] == block.text


def test_markdown_fence_with_trailing_text_does_not_close_code() -> None:
    raw = (
        "```python\nprint(1)\n```not-a-closing-fence\n"
        "# This is code, not the title\n```\n\n# Actual heading\n\nActual prose.\n"
    )
    document = parse_document(raw.encode(), filename="note.md")
    assert document.title == "Actual heading"
    assert "# This is code, not the title" in document.blocks[0].text
    assert document.blocks[0].section is None


def test_xhtml_encoding_declaration_is_supported_without_losing_article() -> None:
    raw = b'<?xml version="1.0" encoding="UTF-8"?>\n' + (FIXTURES / "article.html").read_bytes()
    document = parse_document(raw, filename="article.xhtml", content_type="application/xhtml+xml")
    assert document.title == "Synthetic evidence study"
    assert any("A reproducible research workflow" in block.text for block in document.blocks)
    assert any(block.locator["type"] == "html_xpath" for block in document.blocks)


def test_markdown_heading_whitespace_has_linear_work_and_bounded_metadata() -> None:
    raw = "# title" + " " * 100_000 + "end\n"
    started = time.monotonic()
    document = parse_document(raw.encode(), filename="note.md", timeout=0.2)
    assert time.monotonic() - started < 1.0
    assert document.title == "title end"
    assert "".join(block.text for block in document.blocks) == raw.rstrip()
    long_heading = parse_document(("# " + "文" * 1000).encode(), filename="long.md")
    assert len(long_heading.title) == 300
    assert len(long_heading.blocks[0].text) == 1002


def test_plain_text_timeout_is_enforced() -> None:
    with pytest.raises(RuminateError) as error:
        parse_document(b"Normal text.", filename="note.txt", timeout=1e-9)
    assert error.value.code == "parse_timeout"


def test_html_metadata_is_bounded_and_malformed_canonical_is_ignored() -> None:
    raw = (FIXTURES / "article.html").read_text()
    raw = raw.replace(
        "<title>Synthetic evidence study</title>", "<title>" + "x" * 1000 + "</title>"
    )
    raw = raw.replace("https://example.org/synthetic-evidence", "https://[")
    document = parse_document(raw.encode(), filename="article.html")
    assert len(document.title) == 300
    assert document.canonical_url is None
    assert any("reproducible research" in block.text for block in document.blocks)


@pytest.mark.parametrize("tag", ["p", "div"])
def test_html_windows1252_locator_preserves_decoding_for_replay(tag: str) -> None:
    passage = (
        "Café evidence — a reproducible research workflow preserves original passages and "
        "records exactly how the saved HTML was decoded. This synthetic paragraph includes "
        "characters outside ASCII so a later UTF-8-only replay cannot silently succeed."
    )
    raw = (
        '<!doctype html><html><head><meta charset="windows-1252">'
        "<title>Café evidence</title></head><body><article>"
        f"<{tag}>{passage}</{tag}></article></body></html>"
    ).encode("windows-1252")
    document = parse_document(
        raw,
        filename="article.html",
        content_type="text/html; charset=windows-1252",
    )
    assert document.title == "Café evidence"
    assert any(passage == block.text for block in document.blocks)
    assert all(block.locator["encoding"] == "windows-1252" for block in document.blocks)
    expected_type = "html_xpath" if tag == "p" else "html_extracted_text"
    assert document.blocks[0].locator["type"] == expected_type
    encoding = document.blocks[0].locator["encoding"]
    replay = parse_document(
        raw,
        filename="article.html",
        content_type=f"text/html; charset={encoding}",
    )
    assert replay.blocks == document.blocks
    if tag == "p":
        tree = html.fromstring(raw.decode(encoding))
        for block in document.blocks:
            original = tree.xpath(block.locator["xpath"])[0].text_content()
            assert original[block.locator["char_start"] : block.locator["char_end"]] == block.text


def test_html_partial_mapping_warnings_are_not_repeated_per_paragraph() -> None:
    raw = (FIXTURES / "article.html").read_text().replace("<p>", "<div>").replace("</p>", "</div>")
    document = parse_document(raw.encode(), filename="article.html")
    assert sum(block.locator["type"] == "html_extracted_text" for block in document.blocks) >= 2
    assert document.warnings.count("html_partial_dom_mapping") == 1
    assert len(document.warnings) == len(set(document.warnings))
