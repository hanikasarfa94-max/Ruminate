"""Host-independent application boundary shared by CLI and MCP."""

import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ruminate import __version__
from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.database import (
    bundled_migrations,
    connect,
    fts5_available,
    initialize,
    validate_database,
    validate_storage_paths,
    verify_history,
)

if TYPE_CHECKING:
    from ruminate.backup import BackupService
    from ruminate.enrichment import EnrichmentService
    from ruminate.knowledge import KnowledgeService
    from ruminate.management import ManagementService
    from ruminate.organization import OrganizationService
    from ruminate.outputs import OutputService
    from ruminate.review import ReviewService
    from ruminate.thinking import ThinkingService


class Application:
    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    def knowledge(self) -> "KnowledgeService":
        from ruminate.knowledge import KnowledgeService

        return KnowledgeService(self.config)

    @property
    def management(self) -> "ManagementService":
        from ruminate.management import ManagementService

        return ManagementService(self.config)

    @property
    def reviews(self) -> "ReviewService":
        from ruminate.review import ReviewService

        return ReviewService(self.config)

    @property
    def outputs(self) -> "OutputService":
        from ruminate.outputs import OutputService

        return OutputService(self.config)

    @property
    def enrichment(self) -> "EnrichmentService":
        from ruminate.enrichment import EnrichmentService

        return EnrichmentService(self.config)

    @property
    def backups(self) -> "BackupService":
        from ruminate.backup import BackupService

        return BackupService(self.config)

    @property
    def thinking(self) -> "ThinkingService":
        from ruminate.thinking import ThinkingService

        return ThinkingService(self.config)

    @property
    def organization(self) -> "OrganizationService":
        from ruminate.organization import OrganizationService

        return OrganizationService(self.config)

    def initialize(self) -> dict[str, Any]:
        if not fts5_available():
            raise RuminateError(
                "fts5_unavailable", "This Python SQLite build does not support FTS5"
            )
        validate_storage_paths(self.config.data_dir)
        self.config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        initialize(self.config.database_path)
        self.config.database_path.chmod(0o600)
        for name in ("blobs", "outputs"):
            (self.config.data_dir / name).mkdir(exist_ok=True, mode=0o700)
        return self.health_check()

    def _require_ready(self) -> None:
        validate_storage_paths(self.config.data_dir)
        if not self.config.database_path.is_file():
            raise RuminateError("not_initialized", "Run ruminate init before accessing the store")
        with connect(self.config.database_path, readonly=True) as connection:
            validate_database(connection)
            current = verify_history(connection, bundled_migrations())
            if current != len(bundled_migrations()):
                raise RuminateError("migration_required", "Run ruminate init to upgrade the store")

    def ingest_file(
        self, path: Path, *, topic: str | None = None, request_id: str | None = None
    ) -> dict[str, Any]:
        from ruminate.ingestion.service import IngestionService

        self._require_ready()
        return IngestionService(self.config).ingest_file(path, topic=topic, request_id=request_id)

    def ingest_url(
        self,
        url: str,
        *,
        topic: str | None = None,
        network_allowed: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        from ruminate.ingestion.service import IngestionService

        self._require_ready()
        return IngestionService(self.config).ingest_url(
            url, topic=topic, network_allowed=network_allowed, request_id=request_id
        )

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        from ruminate.ingestion.service import IngestionService

        self._require_ready()
        return IngestionService(self.config).list_jobs(limit)

    def ingest_upload(
        self, filename: str, body: bytes, *, topic: str | None = None, request_id: str
    ) -> dict[str, Any]:
        from ruminate.ingestion.service import IngestionService

        self._require_ready()
        return IngestionService(self.config).ingest_upload(
            filename, body, topic=topic, request_id=request_id
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        from ruminate.ingestion.service import IngestionService

        self._require_ready()
        job = IngestionService(self.config).get_job(job_id)
        return self.enrichment.get(job_id) if job["kind"] == "enrichment" else job

    def retry_job(self, job_id: str, *, network_allowed: bool = False) -> dict[str, Any]:
        import asyncio

        from ruminate.ingestion.service import IngestionService

        self._require_ready()
        if self.get_job(job_id)["kind"] == "enrichment":
            return asyncio.run(self.enrichment.retry(job_id))
        return IngestionService(self.config).retry_job(job_id, network_allowed=network_allowed)

    def list_sources(
        self, *, topic: str | None = None, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        from ruminate.persistence.sources import SourceRepository

        self._require_ready()
        return SourceRepository(self.config.database_path).list_sources(
            topic=topic, limit=limit, offset=offset
        )

    def get_source(
        self,
        source_id: str,
        *,
        version_id: str | None = None,
        block_start: int = 0,
        block_limit: int = 20,
    ) -> dict[str, Any]:
        from ruminate.persistence.sources import SourceRepository

        self._require_ready()
        return SourceRepository(self.config.database_path).get_source(
            source_id,
            version_id=version_id,
            block_start=block_start,
            block_limit=block_limit,
        )

    def search_sources(
        self, query: str, *, topic: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        from ruminate.persistence.sources import SourceRepository

        self._require_ready()
        return SourceRepository(self.config.database_path).search(query, topic=topic, limit=limit)

    def get_source_page_image(
        self, source_id: str, *, version_id: str | None = None, page: int = 1
    ) -> tuple[dict[str, Any], bytes]:
        """Render one preserved PDF page for the host; never invoke a model or persist an image."""
        from ruminate.ingestion.files import BlobStore
        from ruminate.ingestion.vision import render_pdf_page

        source = self.get_source(source_id, version_id=version_id, block_limit=1)
        if source["mime_type"] != "application/pdf":
            raise RuminateError("not_pdf", "Page images currently support stored PDF versions only")
        body = BlobStore(self.config).read(source["sha256"], source["blob_path"])
        rendered = render_pdf_page(
            body, page=page, max_pages=self.config.max_pdf_pages, timeout=self.config.parse_timeout
        )
        metadata = {
            "source_id": source_id,
            "version_id": source["version_id"],
            "source_sha256": source["sha256"],
            "image_sha256": hashlib.sha256(rendered.png).hexdigest(),
            "page": rendered.page,
            "page_count": rendered.page_count,
            "locator": {"type": "pdf_page", "page": rendered.page},
            "width": rendered.width,
            "height": rendered.height,
            "mime_type": "image/png",
            "renderer": rendered.renderer,
            "warnings": rendered.warnings,
            "content_trust": "untrusted_source",
            "interpretation_status": "not_interpreted",
        }
        return metadata, rendered.png

    def add_visual_observation(
        self,
        *,
        source_id: str,
        version_id: str,
        page: int,
        kind: str,
        text: str,
        host: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Save host-supplied text separately from native evidence; always unverified."""
        from ruminate.persistence.observations import ObservationRepository

        # Rendering verifies the requested page actually exists in the preserved bytes.
        self.get_source_page_image(source_id, version_id=version_id, page=page)
        return ObservationRepository(self.config.database_path).add(
            source_id=source_id,
            version_id=version_id,
            page=page,
            kind=kind,
            text=text,
            host=host,
            request_id=request_id,
        )

    def list_visual_observations(
        self,
        source_id: str,
        *,
        version_id: str | None = None,
        page: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        from ruminate.persistence.observations import ObservationRepository

        self._require_ready()
        return ObservationRepository(self.config.database_path).list(
            source_id=source_id,
            version_id=version_id,
            page=page,
            limit=limit,
        )

    def health_check(self) -> dict[str, Any]:
        """Inspect capabilities without creating a database or modifying its records."""
        fts5 = fts5_available()
        exists = self.config.database_path.is_file()
        result: dict[str, Any] = {
            "service": "ruminate",
            "version": __version__,
            "status": "uninitialized",
            "initialized": False,
            "python_version": sys.version.split()[0],
            "sqlite_version": sqlite3.sqlite_version,
            "schema_version": None,
            "expected_schema_version": len(bundled_migrations()),
            "fts5": fts5,
            "model": {"provider": "none", "credentials_required": False},
            "network_mode": self.config.network_mode,
        }
        try:
            validate_storage_paths(self.config.data_dir)
            if not exists:
                return result
            with connect(self.config.database_path, readonly=True) as connection:
                validate_database(connection)
                current = verify_history(connection, bundled_migrations())
                integrity = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
                journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            result.update(
                initialized=True,
                schema_version=current,
                integrity=integrity,
                journal_mode=journal,
                status="ok"
                if (
                    fts5
                    and integrity == "ok"
                    and current == len(bundled_migrations())
                    and journal == "wal"
                )
                else "needs_attention",
            )
        except RuminateError as exc:
            result.update(status="needs_attention", error=exc.as_dict())
        except sqlite3.Error:
            result.update(
                status="needs_attention",
                error={
                    "code": "database_unreadable",
                    "message": "Database is unreadable or incomplete",
                },
            )
        return result
