"""Retryable optional proposal generation; parsed evidence and beliefs are never overwritten."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from typing import Any

from ruminate.config import Config
from ruminate.errors import RuminateError
from ruminate.persistence.common import Store, audit, json_dump, new_id, timestamp, validate_text
from ruminate.persistence.sources import SourceRepository
from ruminate.providers import LLMProvider, NoLLMProvider


class EnrichmentService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(config)

    def get(self, job_id: str) -> dict[str, Any]:
        validate_text(job_id, "job_id", 100)
        with self.store.read() as connection:
            row = connection.execute(
                "SELECT j.*,i.source_id,i.version_id,i.provider,i.result_json FROM jobs j "
                "JOIN enrichment_inputs i ON j.id=i.job_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise RuminateError("job_not_found", "Enrichment job does not exist")
        result = dict(row)
        result["job_id"] = result.pop("id")
        result["result"] = json.loads(result.pop("result_json") or "null")
        return result

    def _authorize(self, provider: LLMProvider, allow_external: bool) -> None:
        if type(allow_external) is not bool:
            raise RuminateError("invalid_input", "allow_external must be a boolean")
        if (
            not isinstance(getattr(provider, "name", None), str)
            or type(getattr(provider, "external", None)) is not bool
            or not callable(getattr(provider, "generate_structured", None))
        ):
            raise RuminateError(
                "invalid_provider", "Provider must implement the proposal interface"
            )
        validate_text(provider.name, "provider name", 100)
        if provider.external and (not allow_external or self.config.network_mode == "off"):
            raise RuminateError("model_not_authorized", "External model use requires authorization")

    @staticmethod
    def _timeout(value: float) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= 60
        ):
            raise RuminateError("invalid_limit", "Provider timeout must be in (0,60]")

    async def start(
        self,
        source_id: str,
        *,
        version_id: str | None = None,
        request_id: str,
        provider: LLMProvider | None = None,
        timeout: float = 10,
        allow_external: bool = False,
    ) -> dict[str, Any]:
        selected = NoLLMProvider() if provider is None else provider
        self._authorize(selected, allow_external)
        validate_text(request_id, "request_id", 100)
        validate_text(source_id, "source_id", 100)
        if version_id is not None:
            validate_text(version_id, "version_id", 100)
        self._timeout(timeout)
        source = SourceRepository(self.config.database_path).get_source(
            source_id, version_id=version_id, block_limit=20
        )
        inputs = {
            "source_id": source_id,
            "version_id": source["version_id"],
            "blocks": [
                {"block_id": b["block_id"], "text": b["text"], "locator": b["locator"]}
                for b in source["blocks"]
            ],
        }
        with self.store.write() as connection:
            old = connection.execute(
                "SELECT * FROM enrichment_inputs WHERE request_id=?", (request_id,)
            ).fetchone()
            if old is not None:
                if (old["source_id"], old["version_id"], old["provider"]) != (
                    source_id,
                    source["version_id"],
                    selected.name,
                ):
                    raise RuminateError("idempotency_conflict", "Enrichment request was reused")
                job_id = old["job_id"]
            else:
                job_id = new_id("job")
                connection.execute(
                    "INSERT INTO jobs(id,kind,input_ref,status) VALUES(?,'enrichment',?,'queued')",
                    (job_id, source["version_id"]),
                )
                connection.execute(
                    "INSERT INTO enrichment_inputs(job_id,request_id,source_id,version_id,"
                    "provider,input_json) VALUES(?,?,?,?,?,?)",
                    (
                        job_id,
                        request_id,
                        source_id,
                        source["version_id"],
                        selected.name,
                        json_dump(inputs),
                    ),
                )
                audit(connection, "enrichment_started", job_id, {"provider": selected.name})
        return await self.retry(
            job_id, provider=selected, timeout=timeout, allow_external=allow_external
        )

    async def retry(
        self,
        job_id: str,
        *,
        provider: LLMProvider | None = None,
        timeout: float = 10,
        allow_external: bool = False,
    ) -> dict[str, Any]:
        selected = NoLLMProvider() if provider is None else provider
        self._authorize(selected, allow_external)
        validate_text(job_id, "job_id", 100)
        self._timeout(timeout)
        token = new_id("lease")
        with self.store.write() as connection:
            row = connection.execute(
                "SELECT j.status,i.* FROM jobs j JOIN enrichment_inputs i ON j.id=i.job_id "
                "WHERE j.id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise RuminateError("job_not_found", "Enrichment job does not exist")
            if row["provider"] != selected.name:
                raise RuminateError("provider_mismatch", "Retry requires the original provider")
            if row["status"] == "enriched":
                completed = True
            else:
                completed = False
                if row["lease_token"] and (row["lease_expires_at"] or 0) > time.time():
                    raise RuminateError("job_busy", "Enrichment job is already running")
                connection.execute(
                    "UPDATE enrichment_inputs SET lease_token=?,lease_expires_at=? WHERE job_id=?",
                    (token, time.time() + timeout + 5, job_id),
                )
                connection.execute(
                    "UPDATE jobs SET status='queued',attempts=attempts+1,error_code=NULL,"
                    "started_at=?,finished_at=NULL WHERE id=?",
                    (timestamp(), job_id),
                )
            inputs = json.loads(row["input_json"])
        if completed:
            return self.get(job_id)
        error: str | None = None
        proposals: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        try:
            raw = await asyncio.wait_for(
                selected.generate_structured("propose_claims", dict, copy.deepcopy(inputs)),
                timeout=timeout,
            )
            # A provider may catch cancellation and return late. Such output still
            # misses the contract; async cancellation is not a process CPU sandbox.
            if time.monotonic() > deadline:
                raise TimeoutError
            proposals = self._validate(raw, inputs)
        except TimeoutError:
            error = "provider_timeout"
        except RuminateError as exc:
            error = exc.code
        except asyncio.CancelledError:
            self._finish(job_id, token, [], "provider_cancelled")
            raise
        except Exception:
            error = "provider_failure"
        self._finish(job_id, token, proposals, error)
        return self.get(job_id)

    def _finish(
        self, job_id: str, token: str, proposals: list[dict[str, Any]], error: str | None
    ) -> None:
        with self.store.write() as connection:
            updated = connection.execute(
                "UPDATE enrichment_inputs SET result_json=?,lease_token=NULL,lease_expires_at=NULL "
                "WHERE job_id=? AND lease_token=?",
                (
                    None if error else json_dump({"proposals": proposals, "status": "unverified"}),
                    job_id,
                    token,
                ),
            )
            if updated.rowcount != 1:
                raise RuminateError("job_lease_lost", "Enrichment worker lost its lease")
            connection.execute(
                "UPDATE jobs SET status=?,error_code=?,finished_at=? WHERE id=?",
                ("needs_attention" if error else "enriched", error, timestamp(), job_id),
            )
            audit(
                connection,
                "enrichment_finished",
                job_id,
                {"error_code": error, "proposal_count": len(proposals)},
            )

    @staticmethod
    def _validate(raw: dict[str, Any], inputs: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(raw, dict) or not isinstance(raw.get("proposals"), list):
            raise RuminateError("invalid_proposals", "Provider result must contain proposals")
        proposals = raw["proposals"]
        if len(proposals) > 5:
            raise RuminateError("invalid_proposals", "At most five proposals are allowed")
        blocks = {b["block_id"]: b for b in inputs["blocks"]}
        result = []
        for item in proposals:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("kind"), str)
                or item.get("kind")
                not in {"source_assertion", "model_inference", "hypothesis", "question"}
            ):
                raise RuminateError("invalid_proposals", "Invalid proposal kind")
            try:
                for field, maximum in (("statement", 2000), ("quote", 1000), ("block_id", 100)):
                    value = item.get(field)
                    if not isinstance(value, str):
                        raise RuminateError("invalid_proposals", "Proposal text must be a string")
                    validate_text(value, field, maximum)
            except RuminateError:
                raise RuminateError(
                    "invalid_proposals", "Invalid proposal text or identifier"
                ) from None
            block = blocks.get(item.get("block_id"))
            if block is None or item["quote"] not in block["text"]:
                raise RuminateError("invalid_proposals", "Proposal quote must match provided text")
            start = block["text"].find(item["quote"])
            result.append(
                {
                    "kind": item["kind"],
                    "statement": item["statement"],
                    "block_id": item["block_id"],
                    "version_id": inputs["version_id"],
                    "quote": item["quote"],
                    "span": {"start": start, "end": start + len(item["quote"])},
                    "status": "unverified",
                }
            )
        return result
