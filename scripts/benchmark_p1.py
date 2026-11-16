"""Benchmark synthetic CAS storage, SQLite indexing and retrieval, excluding parsing.

All material is generated locally. The database and blobs live in a temporary
directory and are removed after the report is collected. No network or model is
used. Results describe this run and are not a performance SLA.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ruminate import __version__
from ruminate.application import Application
from ruminate.config import Config
from ruminate.ingestion.files import BlobStore
from ruminate.ingestion.parsers import ParsedBlock, ParsedDocument
from ruminate.persistence.database import connect
from ruminate.persistence.sources import SourceRepository, _terms


def synthetic_document(number: int, block_count: int) -> tuple[bytes, ParsedDocument]:
    pieces = []
    blocks = []
    offset = 0
    for ordinal in range(block_count):
        text = (
            f"Document {number:04d}, block {ordinal:03d}. "
            "Agent benchmark evidence retrieval retains exact source versions. "
            "认知反刍需要可靠证据，研究提纲保留引用定位。 "
            f"cohort{number % 10} section{ordinal % 10}."
        )
        blocks.append(
            ParsedBlock(
                text,
                {"type": "text_span", "char_start": offset, "char_end": offset + len(text)},
                section=f"Block {ordinal}",
            )
        )
        pieces.append(text + "\n\n")
        offset += len(text) + 2
    raw = "".join(pieces).encode("utf-8")
    return raw, ParsedDocument(f"Synthetic document {number}", "text/plain", blocks)


def validate_persisted_locators(config: Config, blobs: BlobStore) -> int:
    verified = 0
    previous_version = None
    raw = ""
    with connect(config.database_path, readonly=True) as connection:
        for row in connection.execute(
            "SELECT b.text,b.locator_json,b.version_id,v.sha256,v.blob_path "
            "FROM blocks b JOIN source_versions v ON v.id=b.version_id "
            "ORDER BY b.version_id,b.ordinal"
        ):
            if row["version_id"] != previous_version:
                raw = blobs.read(row["sha256"], row["blob_path"]).decode("utf-8")
                previous_version = row["version_id"]
            locator = json.loads(row["locator_json"])
            if raw[locator["char_start"] : locator["char_end"]] != row["text"]:
                raise RuntimeError("Persisted evidence locator did not match the saved blob")
            verified += 1
    return verified


def measure_query(repository: SourceRepository, query: str) -> dict[str, Any]:
    timings = []
    hits = []
    for _ in range(5):
        started = time.perf_counter()
        hits = repository.search(query, limit=10)
        timings.append(round((time.perf_counter() - started) * 1000, 3))
    # Count all matches separately; API results are deliberately capped to ten.
    match = " AND ".join('"' + term + '"' for term in _terms(query, indexing=False))
    with connect(repository.database_path, readonly=True) as connection:
        matched_count = connection.execute(
            "SELECT COUNT(*) FROM block_fts JOIN blocks b ON b.id=block_fts.block_id "
            "JOIN sources s ON s.current_version_id=b.version_id WHERE block_fts MATCH ?",
            (match,),
        ).fetchone()[0]
    return {
        "query": query,
        "limit": 10,
        "returned_count": len(hits),
        "matched_count": matched_count,
        "query_times_ms": timings,
        "median_query_ms": round(statistics.median(timings), 3),
        "cache_condition": "same process and filesystem cache immediately after indexing",
    }


def benchmark(documents: int, blocks_per_document: int) -> dict[str, Any]:
    with TemporaryDirectory(prefix="ruminate-benchmark-") as temporary:
        config = Config(Path(temporary) / "data", network_mode="off")
        Application(config).initialize()
        repository = SourceRepository(config.database_path)
        blobs = BlobStore(config)
        blob_bytes = 0
        generated_seconds = 0.0
        blob_seconds = 0.0
        index_seconds = 0.0
        started = time.perf_counter()
        for number in range(documents):
            generation_start = time.perf_counter()
            body, document = synthetic_document(number, blocks_per_document)
            generated_seconds += time.perf_counter() - generation_start
            blob_bytes += len(body)
            blob_start = time.perf_counter()
            digest, relative = blobs.put(body)
            blob_seconds += time.perf_counter() - blob_start
            index_start = time.perf_counter()
            repository.save_document(
                document=document,
                sha256=digest,
                blob_path=relative,
                uri=f"file:///synthetic/benchmark/document-{number:04d}.txt",
                source_type="file",
                retrieved_at="2026-11-11T00:00:00+00:00",
            )
            index_seconds += time.perf_counter() - index_start
        import_seconds = time.perf_counter() - started
        validation_start = time.perf_counter()
        verified = validate_persisted_locators(config, blobs)
        validation_seconds = time.perf_counter() - validation_start
        if verified != documents * blocks_per_document:
            raise RuntimeError("Unexpected persisted block count")
        queries = [
            measure_query(repository, query)
            for query in ("evidence retrieval", "认知反刍", "cohort7 section3")
        ]
        with connect(config.database_path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            actual_sources = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            actual_blocks = connection.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        sqlite_bytes = config.database_path.stat().st_size
    return {
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "application_version": __version__,
        "environment": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "scope": "synthetic bytes → CAS → SourceRepository + FTS; excludes parser and network",
        "materials": "developer-generated synthetic text; no user documents",
        "storage": "temporary local directory, automatically removed after measurement",
        "documents": actual_sources,
        "blocks_per_document": blocks_per_document,
        "blocks": actual_blocks,
        "index_seconds": round(index_seconds, 3),
        "blob_write_seconds": round(blob_seconds, 3),
        "fixture_generation_seconds": round(generated_seconds, 3),
        "total_import_seconds": round(import_seconds, 3),
        "verified_locator_count": verified,
        "locator_verification_seconds": round(validation_seconds, 3),
        "sqlite_bytes": sqlite_bytes,
        "blob_bytes": blob_bytes,
        "queries": queries,
        "interpretation": "Observed local synthetic benchmark only; not a promised SLA",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=1000)
    parser.add_argument("--blocks-per-document", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.documents <= 10_000 or not 1 <= args.blocks_per_document <= 1000:
        parser.error("documents must be 1..10000 and blocks-per-document must be 1..1000")
    report = benchmark(args.documents, args.blocks_per_document)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
