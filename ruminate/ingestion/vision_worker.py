"""Private single-page renderer; never accepts filenames, passwords or URLs."""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import sys
from typing import Any

from ruminate.ingestion.pdf_worker import _limits, _watch_memory
from ruminate.ingestion.vision import MAX_DIMENSION, MAX_IMAGE_BYTES, MAX_INPUT_BYTES


def process(data: bytes, page_number: int, max_pages: int, warnings: list[str]) -> dict[str, Any]:
    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
        from pypdfium2 import raw as pdfium_c
    except ImportError:
        return {"ok": False, "code": "renderer_unavailable"}

    try:
        with pdfium.PdfDocument(data) as document:
            # PDFs with an empty user password may open automatically. They
            # remain encrypted documents and follow the same policy as capture.
            if pdfium_c.FPDF_GetSecurityHandlerRevision(document) != -1:
                return {"ok": False, "code": "pdf_encrypted"}
            count = len(document)
            if count > max_pages:
                return {"ok": False, "code": "pdf_page_limit"}
            if not 1 <= page_number <= count:
                return {"ok": False, "code": "invalid_page"}
            page = document[page_number - 1]
            try:
                width, height = page.get_size()
                if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                    return {"ok": False, "code": "render_dimensions"}
                # 144 dpi for ordinary pages, reduced for large sheets. Use
                # nextafter to keep ceil() inside the fixed pixel boundary.
                scale = min(2.0, math.nextafter(MAX_DIMENSION / max(width, height), 0.0))
                pixel_width, pixel_height = math.ceil(width * scale), math.ceil(height * scale)
                if not all(1 <= value <= MAX_DIMENSION for value in (pixel_width, pixel_height)):
                    return {"ok": False, "code": "render_dimensions"}
                if scale < 2.0:
                    warnings.append("page_resolution_reduced_to_fit_budget")
                # No form environment is initialized: no form actions or JS.
                # Include static page annotations; links remain inert pixels.
                bitmap = page.render(
                    scale=scale,
                    may_draw_forms=False,
                    draw_annots=True,
                    limit_image_cache=True,
                    rev_byteorder=True,
                )
                try:
                    image = bitmap.to_pil()
                    try:
                        output = io.BytesIO()
                        image.save(output, format="PNG", compress_level=6)
                        png = output.getvalue()
                        rendered_width, rendered_height = image.size
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
            if len(png) > MAX_IMAGE_BYTES:
                return {"ok": False, "code": "render_output_limit"}
            warnings.extend([
                "visual_content_is_untrusted",
                "rendering_does_not_verify_transcription",
                "interactive_forms_not_rendered",
            ])
            return {
                "ok": True,
                "png": base64.b64encode(png).decode("ascii"),
                "width": rendered_width,
                "height": rendered_height,
                "page": page_number,
                "page_count": count,
                "renderer": f"pypdfium2/{pdfium.PYPDFIUM_INFO};pdfium/{pdfium.PDFIUM_INFO}",
                "warnings": sorted(set(warnings)),
            }
    except pdfium.PdfiumError as exc:
        code = "pdf_encrypted" if exc.err_code in {4, 5} else "invalid_pdf"
        return {"ok": False, "code": code}
    except ImportError:
        return {"ok": False, "code": "renderer_unavailable"}
    except MemoryError:
        return {"ok": False, "code": "render_memory_limit"}
    except Exception:
        return {"ok": False, "code": "invalid_pdf"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--max-rss-bytes", type=int, required=True)
    args = parser.parse_args()
    warnings = [
        value.replace("parser_", "renderer_", 1)
        for value in [
            *_limits(args.cpu_seconds, args.max_rss_bytes),
            *_watch_memory(args.max_rss_bytes),
        ]
    ]
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        result: dict[str, Any] = {"ok": False, "code": "source_too_large"}
    else:
        result = process(data, args.page, args.max_pages, warnings)
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
