"""Durable capture jobs; authorization is checked before any outside access."""

import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.ingestion.fetch import fetch_url, normalize_url
from ruminate.ingestion.files import BlobStore, read_local_file
from ruminate.ingestion.parsers import parse_document
from ruminate.persistence.common import Store, audit, mutate, validate_text
from ruminate.persistence.database import connect
from ruminate.persistence.sources import SourceRepository


def now() -> str:
    return datetime.now(UTC).isoformat()


class IngestionService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.blobs = BlobStore(config)
        self.repository = SourceRepository(config.database_path)

    def ingest_file(
        self, path: Path, *, topic: str | None = None, request_id: str | None = None
    ) -> dict[str, Any]:
        return self._start("file", str(path.expanduser().absolute()), topic, False, request_id)

    def ingest_url(
        self,
        url: str,
        *,
        topic: str | None = None,
        network_allowed: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._start("url", url, topic, network_allowed, request_id)

    def _start(
        self,
        kind: str,
        input_ref: str,
        topic: str | None,
        network_allowed: bool,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if request_id is not None:
            return self._start_idempotent(kind, input_ref, topic, network_allowed, request_id)
        job_id = f"job_{uuid4().hex}"
        with connect(self.config.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO jobs(id,kind,input_ref,status) VALUES (?,?,?,'queued')",
                    (job_id, kind, input_ref),
                )
                connection.execute(
                    "INSERT INTO job_inputs(job_id,topic) VALUES (?,?)", (job_id, topic)
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.retry_job(job_id, network_allowed=network_allowed)

    def _start_idempotent(
        self,
        kind: str,
        input_ref: str,
        topic: str | None,
        network_allowed: bool,
        request_id: str,
    ) -> dict[str, Any]:
        """Reserve one durable job before capture, so retries never repeat outside access.

        A repeated request returns that job's current state, including failures. An interrupted
        job can be explicitly resumed with retry_job; replay alone does not authorize new I/O.
        """
        created = False
        payload = {
            "kind": kind,
            "input_ref": input_ref,
            "topic": topic,
            "network_allowed": network_allowed,
        }
        with Store(self.config).write() as connection:

            def reserve() -> dict[str, Any]:
                nonlocal created
                job_id = f"job_{uuid4().hex}"
                connection.execute(
                    "INSERT INTO jobs(id,kind,input_ref,status) VALUES (?,?,?,'queued')",
                    (job_id, kind, input_ref),
                )
                connection.execute(
                    "INSERT INTO job_inputs(job_id,topic) VALUES (?,?)", (job_id, topic)
                )
                audit(connection, "capture_requested", job_id, {"kind": kind, "topic": topic})
                created = True
                return {"job_id": job_id}

            reservation = mutate(connection, "capture", request_id, payload, reserve)
        job_id = str(reservation["job_id"])
        if created:
            self.retry_job(job_id, network_allowed=network_allowed)
        return {"job_id": job_id, **self.get_job(job_id)}

    def ingest_upload(
        self, filename: str, body: bytes, *, topic: str | None = None, request_id: str
    ) -> dict[str, Any]:
        """Capture bytes deliberately uploaded by the caller, without granting any file path."""
        validate_text(filename, "filename", 200)
        if (
            filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        ):
            raise RuminateError("invalid_filename", "Choose a file name without directory parts")
        if not isinstance(body, bytes) or not body:
            raise RuminateError("invalid_upload", "Upload a non-empty file")
        if len(body) > self.config.max_source_bytes:
            raise RuminateError("source_too_large", "Uploaded file exceeds the source size limit")
        sha256 = hashlib.sha256(body).hexdigest()
        created = False
        payload = {"filename": filename, "sha256": sha256, "topic": topic}
        with Store(self.config).write() as connection:

            def reserve() -> dict[str, Any]:
                nonlocal created
                job_id = f"job_{uuid4().hex}"
                digest, relative = self.blobs.put(body)
                uri = f"upload://{sha256}/{quote(filename, safe='')}"
                snapshot: dict[str, Any] = {
                    "sha256": digest,
                    "blob_path": relative,
                    "uri": uri,
                    "filename": filename,
                    "content_type": None,
                    "aliases": [],
                    "retrieved_at": now(),
                }
                connection.execute(
                    "INSERT INTO jobs(id,kind,input_ref,status) VALUES (?,'file',?,'queued')",
                    (job_id, uri),
                )
                connection.execute(
                    "INSERT INTO job_inputs(job_id,topic,snapshot_json) VALUES (?,?,?)",
                    (job_id, topic, json.dumps(snapshot, ensure_ascii=False)),
                )
                audit(connection, "upload_requested", job_id, {"filename": filename})
                created = True
                return {"job_id": job_id}

            reservation = mutate(connection, "capture_upload", request_id, payload, reserve)
        job_id = str(reservation["job_id"])
        if created:
            self.retry_job(job_id)
        return {"job_id": job_id, **self.get_job(job_id)}

    def get_job(self, job_id: str) -> dict[str, Any]:
        with connect(self.config.database_path, readonly=True) as connection:
            row = connection.execute(
                "SELECT j.*,r.source_id,r.version_id FROM jobs j "
                "LEFT JOIN job_results r ON r.job_id=j.id WHERE j.id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise RuminateError("job_not_found", "Capture job does not exist")
        return dict(row)

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise RuminateError("invalid_limit", "Job limit must be between 1 and 100")
        with connect(self.config.database_path, readonly=True) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id,kind,status,attempts,error_code,started_at,finished_at FROM jobs "
                    "ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                )
            ]

    def _claim(self, job_id: str) -> tuple[dict[str, Any], str]:
        token = uuid4().hex
        with connect(self.config.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT j.*,i.topic,i.snapshot_json,i.lease_token,i.lease_expires_at "
                    "FROM jobs j JOIN job_inputs i ON i.job_id=j.id WHERE j.id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise RuminateError("job_not_found", "Capture job does not exist")
                if row["lease_token"] and (row["lease_expires_at"] or 0) > time.time():
                    raise RuminateError(
                        "job_busy", "Capture is running; retry after its lease expires"
                    )
                if row["status"] == "indexed":
                    return dict(row), ""
                connection.execute(
                    "UPDATE job_inputs SET lease_token=?,lease_expires_at=? WHERE job_id=?",
                    (
                        token,
                        time.time() + self.config.parse_timeout + self.config.fetch_timeout + 60,
                        job_id,
                    ),
                )
                connection.execute(
                    "UPDATE jobs SET status='queued',attempts=attempts+1,error_code=NULL,"
                    "started_at=?,finished_at=NULL WHERE id=?",
                    (now(), job_id),
                )
                connection.commit()
                return dict(row), token
            except BaseException:
                connection.rollback()
                raise

    def _progress(self, job_id: str, token: str, status: str, snapshot: dict[str, Any]) -> None:
        with connect(self.config.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                updated = connection.execute(
                    "UPDATE job_inputs SET snapshot_json=? WHERE job_id=? AND lease_token=?",
                    (json.dumps(snapshot, ensure_ascii=False), job_id, token),
                )
                if updated.rowcount != 1:
                    raise RuminateError(
                        "job_lease_lost", "Capture lease no longer belongs to this worker"
                    )
                connection.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _failure(self, job_id: str, token: str, error: RuminateError) -> dict[str, Any]:
        with connect(self.config.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE job_inputs SET lease_token=NULL,lease_expires_at=NULL "
                    "WHERE job_id=? AND lease_token=?",
                    (job_id, token),
                )
                if cursor.rowcount:
                    connection.execute(
                        "UPDATE jobs SET status='needs_attention',error_code=?,finished_at=? "
                        "WHERE id=?",
                        (error.code, now(), job_id),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {"job_id": job_id, "status": "needs_attention", "error": error.as_dict()}

    def retry_job(self, job_id: str, *, network_allowed: bool = False) -> dict[str, Any]:
        job, token = self._claim(job_id)
        if not token:
            return self.get_job(job_id)
        try:
            snapshot: dict[str, Any]
            if job["snapshot_json"]:
                snapshot = json.loads(job["snapshot_json"])
                body = self.blobs.read(snapshot["sha256"], snapshot["blob_path"])
            else:
                snapshot, body = self._capture(job, network_allowed)
                digest, relative = self.blobs.put(body)
                snapshot.update(sha256=digest, blob_path=relative, retrieved_at=now())
            self._progress(job_id, token, "fetched", snapshot)
            parsed = parse_document(
                body,
                filename=snapshot["filename"],
                content_type=snapshot["content_type"],
                max_pages=self.config.max_pdf_pages,
                timeout=self.config.parse_timeout,
            )
            self._progress(job_id, token, "parsed", snapshot)
            result = self.repository.save_document(
                document=parsed,
                sha256=snapshot["sha256"],
                blob_path=snapshot["blob_path"],
                uri=snapshot["uri"],
                source_type=job["kind"],
                retrieved_at=snapshot["retrieved_at"],
                topic=job["topic"],
                aliases=tuple(snapshot["aliases"]),
                job_id=job_id,
                lease_token=token,
            )
            return result
        except RuminateError as exc:
            return self._failure(job_id, token, exc)
        except (OSError, sqlite3.Error):
            return self._failure(
                job_id, token, RuminateError("capture_io_error", "Capture storage failed")
            )
        except (ValueError, KeyError, TypeError):
            return self._failure(
                job_id, token, RuminateError("invalid_snapshot", "Capture snapshot is invalid")
            )

    def _capture(self, job: dict[str, Any], network_allowed: bool) -> tuple[dict[str, Any], bytes]:
        if job["kind"] == "file":
            body, path = read_local_file(Path(job["input_ref"]), self.config)
            return {
                "uri": path.as_uri(),
                "filename": path.name,
                "content_type": None,
                "aliases": [],
            }, body
        if job["kind"] != "url":
            raise RuminateError("invalid_job_kind", "Unsupported capture job kind")
        fetched = fetch_url(
            job["input_ref"],
            network_mode=self.config.network_mode,
            network_allowed=network_allowed,
            allowed_hosts=self.config.allowed_hosts,
            max_bytes=self.config.max_source_bytes,
            timeout=self.config.fetch_timeout,
        )
        return {
            "uri": normalize_url(fetched.original_url),
            "filename": Path(urlsplit(fetched.final_url).path).name or "article.html",
            "content_type": fetched.content_type,
            "aliases": [normalize_url(fetched.final_url)],
        }, fetched.body
