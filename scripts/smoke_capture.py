"""Demonstrate P1 with self-authored fixtures; an optional URL fetch is explicit."""

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from ruminate.application import Application
from ruminate.config import Config
from ruminate.ingestion.files import BlobStore
from ruminate.ingestion.parsers import parse_document


def demonstrate(live_url: str | None = None) -> dict[str, Any]:
    fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    with tempfile.TemporaryDirectory(prefix="ruminate-p1-smoke-") as temporary:
        app = Application(Config(Path(temporary) / "data", "ask", (fixtures,)))
        app.initialize()
        imported = []
        for filename in (
            "single_column.pdf",
            "multi_column_chart.pdf",
            "notes.md",
            "article.html",
            "scanned.pdf",
        ):
            result = app.ingest_file(fixtures / filename, topic="ecommerce-agents")
            if result["status"] not in {"indexed", "needs_ocr"}:
                raise RuntimeError(f"Fixture import failed: {result}")
            imported.append({"fixture": filename, **result})
        duplicate = app.ingest_file(fixtures / "single_column.pdf")
        if not duplicate["deduplicated"]:
            raise RuntimeError("Duplicate PDF created a new version")
        hits = app.search_sources("Agent", topic="ecommerce-agents")
        chinese_hits = app.search_sources("证据", topic="ecommerce-agents")
        if not hits or not chinese_hits:
            raise RuntimeError("English or Chinese source retrieval failed")
        if live_url:
            live_result = app.ingest_url(
                live_url, topic="official-documentation", network_allowed=True
            )
            if live_result["status"] != "indexed":
                raise RuntimeError(f"Live URL import failed: {live_result}")
            imported.append({"url": live_url, **live_result})
        verified = 0
        for result in imported:
            source = app.get_source(
                result["source_id"], version_id=result["version_id"], block_limit=50
            )
            version = source["version"]
            raw = BlobStore(app.config).read(version["sha256"], version["blob_path"])
            content_type = version["mime_type"]
            if content_type == "text/html" and source["blocks"]:
                encoding = source["blocks"][0]["locator"].get("encoding")
                if encoding:
                    content_type += "; charset=" + encoding
            reparsed = parse_document(
                raw,
                filename=result.get("fixture", "article.html"),
                content_type=content_type,
            )
            expected = {
                (json.dumps(block.locator, sort_keys=True), block.text) for block in reparsed.blocks
            }
            while True:
                for block in source["blocks"]:
                    if (
                        json.dumps(block["locator"], sort_keys=True),
                        block["text"],
                    ) not in expected:
                        raise RuntimeError(
                            "A stored block did not reproduce from its original blob"
                        )
                    verified += 1
                if source["next_block_start"] is None:
                    break
                source = app.get_source(
                    result["source_id"],
                    version_id=result["version_id"],
                    block_start=source["next_block_start"],
                    block_limit=50,
                )
        return {
            "status": "SUPPORTED",
            "schema_version": app.health_check()["schema_version"],
            "model_provider": "none",
            "live_url_tested": live_url,
            "imports": imported,
            "duplicate_pdf_reused_version": True,
            "english_hits": len(hits),
            "chinese_hits": len(chinese_hits),
            "queries": ["Agent", "证据"],
            "blocks_reproduced_from_original_snapshot": verified,
            "jobs": app.list_jobs(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-url", help="Explicitly authorize fetching this public URL")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = demonstrate(args.live_url)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
