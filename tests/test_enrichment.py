"""Optional proposals fail safely and can retry exact captured source versions."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ruminate.application import Application
from ruminate.config import Config
from ruminate.enrichment import EnrichmentService
from ruminate.errors import RuminateError
from ruminate.ingestion.files import BlobStore
from ruminate.persistence.database import connect
from ruminate.providers import MockLLMProvider, NoLLMProvider


class Captured:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "evidence.md"
        self.path.write_text("Original preserved evidence has limited experimental scope.\n")
        self.config = Config(directory / "store", network_mode="off", allowed_roots=(directory,))
        self.app = Application(self.config)
        self.app.initialize()
        self.saved = self.app.ingest_file(self.path)
        self.source_id = self.saved["source_id"]
        self.version_id = self.saved["version_id"]
        self.source = self.app.get_source(self.source_id)
        self.block = self.source["blocks"][0]
        self.service = EnrichmentService(self.config)

    def proposal(self, **changes: Any) -> dict[str, Any]:
        return {
            "kind": "hypothesis",
            "statement": "The applicability needs evaluation.",
            "block_id": self.block["block_id"],
            "quote": self.block["text"],
            **changes,
        }

    def response(self, **changes: Any) -> dict[str, Any]:
        return {"proposals": [self.proposal(**changes)]}

    def job_id(self) -> str:
        with connect(self.config.database_path, readonly=True) as connection:
            return str(connection.execute("SELECT job_id FROM enrichment_inputs").fetchone()[0])

    def source_unchanged(self) -> None:
        assert self.app.get_source(self.source_id) == self.source
        assert (
            BlobStore(self.config).read(self.source["sha256"], self.source["blob_path"])
            == self.path.read_bytes()
        )

    def no_claims_or_beliefs(self) -> None:
        with connect(self.config.database_path, readonly=True) as connection:
            for table in ("claims", "claim_evidence", "user_beliefs"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.fixture
def captured(tmp_path: Path) -> Captured:
    return Captured(tmp_path)


def test_no_llm_questions_are_deterministic_unverified_and_never_create_beliefs(
    captured: Captured,
) -> None:
    first = asyncio.run(captured.service.start(captured.source_id, request_id="no-llm-1"))
    second = asyncio.run(captured.service.start(captured.source_id, request_id="no-llm-2"))
    assert first["provider"] == "no_llm" and first["status"] == "enriched"
    assert first["result"] == second["result"]
    proposal = first["result"]["proposals"][0]
    assert proposal["kind"] == "question" and proposal["status"] == "unverified"
    assert (
        proposal["quote"]
        == captured.block["text"][proposal["span"]["start"] : proposal["span"]["end"]]
    )
    assert proposal["version_id"] == captured.version_id
    captured.source_unchanged()
    captured.no_claims_or_beliefs()


def test_g15_timeout_preserves_source_and_retry_uses_old_snapshot(captured: Captured) -> None:
    response = captured.response()
    failed = asyncio.run(
        captured.service.start(
            captured.source_id,
            request_id="timeout",
            provider=MockLLMProvider(response, delay=0.2),
            timeout=0.01,
        )
    )
    assert failed["status"] == "needs_attention" and failed["error_code"] == "provider_timeout"
    assert failed["attempts"] == 1 and failed["result"] is None
    captured.source_unchanged()
    original_bytes = captured.path.read_bytes()
    captured.path.write_text("Updated evidence contradicts the previous experimental scope.\n")
    newer = captured.app.ingest_file(captured.path)
    assert newer["version_id"] != captured.version_id
    captured.path.unlink()
    restarted = EnrichmentService(replace(captured.config, allowed_roots=()))
    restored = asyncio.run(restarted.retry(failed["job_id"], provider=MockLLMProvider(response)))
    assert restored["status"] == "enriched" and restored["attempts"] == 2
    assert restored["version_id"] == captured.version_id
    assert restored["result"]["proposals"][0]["quote"] == captured.block["text"]
    assert (
        BlobStore(captured.config).read(captured.source["sha256"], captured.source["blob_path"])
        == original_bytes
    )
    repeated = asyncio.run(restarted.retry(restored["job_id"], provider=MockLLMProvider(response)))
    assert repeated == restored
    captured.no_claims_or_beliefs()


@pytest.mark.parametrize(
    "alteration",
    [
        {"kind": "confirmed_fact"},
        {"kind": []},
        {"block_id": "fabricated"},
        {"block_id": []},
        {"quote": "not present in the source"},
        {"quote": ""},
        {"quote": 42},
        {"statement": None},
        {"statement": "x" * 2001},
    ],
)
def test_fabricated_or_malformed_proposals_fail_without_partial_result(
    captured: Captured, alteration: dict
) -> None:
    response = {"proposals": [captured.proposal(), captured.proposal(**alteration)]}
    result = asyncio.run(
        captured.service.start(
            captured.source_id,
            request_id="bad",
            provider=MockLLMProvider(response),
        )
    )
    assert result["status"] == "needs_attention" and result["error_code"] == "invalid_proposals"
    assert result["result"] is None
    captured.source_unchanged()
    captured.no_claims_or_beliefs()


@pytest.mark.parametrize(
    "response", [{}, {"proposals": None}, {"proposals": {}}, {"proposals": [None]}]
)
def test_invalid_provider_envelopes_have_stable_errors(captured: Captured, response: Any) -> None:
    result = asyncio.run(
        captured.service.start(
            captured.source_id,
            request_id="envelope",
            provider=MockLLMProvider(response),
        )
    )
    assert result["error_code"] == "invalid_proposals"


def test_maximum_proposals_and_fabricated_metadata_are_not_saved(captured: Captured) -> None:
    too_many = asyncio.run(
        captured.service.start(
            captured.source_id,
            request_id="large",
            provider=MockLLMProvider({"proposals": [captured.proposal()] * 6}),
        )
    )
    assert too_many["error_code"] == "invalid_proposals"
    response = captured.response(
        page=999, version_id="fake-version", user_confirmed=True, confidence=1
    )
    result = asyncio.run(
        captured.service.start(
            captured.source_id, request_id="extra", provider=MockLLMProvider(response)
        )
    )
    proposal = result["result"]["proposals"][0]
    assert proposal["version_id"] == captured.version_id
    assert (
        "page" not in proposal and "user_confirmed" not in proposal and "confidence" not in proposal
    )
    assert proposal["status"] == "unverified"
    captured.no_claims_or_beliefs()


def test_provider_mutating_inputs_cannot_validate_its_own_fabricated_quote(
    captured: Captured,
) -> None:
    class Mutator:
        name = "mutator"
        external = False

        async def generate_structured(self, task: str, schema: type, inputs: dict) -> dict:
            inputs["blocks"][0]["text"] = "fabricated provider text"
            inputs["version_id"] = "provider-created-version"
            return {
                "proposals": [
                    {
                        "kind": "source_assertion",
                        "statement": "fabricated assertion",
                        "quote": "fabricated provider text",
                        "block_id": inputs["blocks"][0]["block_id"],
                    }
                ]
            }

    result = asyncio.run(
        captured.service.start(captured.source_id, request_id="mutation", provider=Mutator())
    )
    assert result["error_code"] == "invalid_proposals"
    with connect(captured.config.database_path, readonly=True) as connection:
        stored = json.loads(
            connection.execute("SELECT input_json FROM enrichment_inputs").fetchone()[0]
        )
    assert stored["version_id"] == captured.version_id
    assert stored["blocks"][0]["text"] == captured.block["text"]
    captured.no_claims_or_beliefs()


def test_external_provider_requires_explicit_boolean_and_network_off_cannot_be_overridden(
    captured: Captured,
) -> None:
    class ExternalMock(MockLLMProvider):
        external = True
        calls = 0

        async def generate_structured(self, task: str, schema: type, inputs: dict) -> dict:
            self.calls += 1
            return await super().generate_structured(task, schema, inputs)

    provider = ExternalMock(captured.response())
    ask = EnrichmentService(replace(captured.config, network_mode="ask"))
    for service, authorization, expected in [
        (ask, False, "model_not_authorized"),
        (ask, "yes", "invalid_input"),
        (captured.service, True, "model_not_authorized"),
    ]:
        with pytest.raises(RuminateError) as error:
            asyncio.run(
                service.start(
                    captured.source_id,
                    request_id="external",
                    provider=provider,
                    allow_external=authorization,
                )
            )
        assert error.value.code == expected
    assert provider.calls == 0
    with connect(captured.config.database_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM enrichment_inputs").fetchone()[0] == 0
    # No network is used: this is an explicitly external-labelled test double.
    allowed = asyncio.run(
        ask.start(captured.source_id, request_id="external", provider=provider, allow_external=True)
    )
    assert allowed["status"] == "enriched" and provider.calls == 1


@pytest.mark.parametrize("timeout", [0, -1, 61, True, "1", float("nan"), float("inf")])
def test_timeout_validation_has_no_persistent_side_effects(
    captured: Captured, timeout: Any
) -> None:
    with pytest.raises(RuminateError) as invalid:
        asyncio.run(
            captured.service.start(captured.source_id, request_id="bad-timeout", timeout=timeout)
        )
    assert invalid.value.code == "invalid_limit"
    with connect(captured.config.database_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM enrichment_inputs").fetchone()[0] == 0


def test_completed_retries_and_start_retries_are_idempotent(captured: Captured) -> None:
    provider = MockLLMProvider(captured.response())
    result = asyncio.run(
        captured.service.start(captured.source_id, request_id="once", provider=provider)
    )
    repeated = asyncio.run(
        captured.service.start(captured.source_id, request_id="once", provider=provider)
    )
    assert result == repeated and result["attempts"] == 1
    with pytest.raises(RuminateError) as conflict:
        asyncio.run(captured.service.start(captured.source_id, request_id="once"))
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(RuminateError) as mismatch:
        asyncio.run(captured.service.retry(result["job_id"]))
    assert mismatch.value.code == "provider_mismatch"


def test_provider_exception_is_redacted_and_retryable(captured: Captured) -> None:
    class Failing(NoLLMProvider):
        async def generate_structured(self, task: str, schema: type, inputs: dict) -> dict:
            raise RuntimeError("secret-provider-token-never-log")

    failed = asyncio.run(
        captured.service.start(captured.source_id, request_id="failure", provider=Failing())
    )
    assert failed["error_code"] == "provider_failure"
    with connect(captured.config.database_path, readonly=True) as connection:
        assert "secret-provider-token-never-log" not in "\n".join(connection.iterdump())
    assert asyncio.run(captured.service.retry(failed["job_id"]))["status"] == "enriched"
    captured.no_claims_or_beliefs()


def test_live_lease_blocks_duplicate_workers(captured: Captured) -> None:
    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        class Blocking(MockLLMProvider):
            async def generate_structured(self, task: str, schema: type, inputs: dict) -> dict:
                started.set()
                await release.wait()
                return await super().generate_structured(task, schema, inputs)

        worker = asyncio.create_task(
            captured.service.start(
                captured.source_id,
                request_id="parallel",
                provider=Blocking(captured.response()),
            )
        )
        await started.wait()
        job_id = captured.job_id()
        with pytest.raises(RuminateError) as busy:
            await captured.service.retry(job_id, provider=MockLLMProvider(captured.response()))
        assert busy.value.code == "job_busy"
        release.set()
        assert (await worker)["attempts"] == 1

    asyncio.run(scenario())


def test_cancelled_worker_releases_lease_and_retains_source(captured: Captured) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class Waiting(NoLLMProvider):
            async def generate_structured(self, task: str, schema: type, inputs: dict) -> dict:
                started.set()
                await asyncio.Event().wait()
                return {}

        task = asyncio.create_task(
            captured.service.start(captured.source_id, request_id="cancel", provider=Waiting())
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = captured.service.get(captured.job_id())
        assert (
            result["status"] == "needs_attention" and result["error_code"] == "provider_cancelled"
        )
        restored = await captured.service.retry(result["job_id"])
        assert restored["status"] == "enriched" and restored["attempts"] == 2

    asyncio.run(scenario())
    captured.source_unchanged()


def test_replaced_expired_lease_rejects_stale_worker_commit(captured: Captured) -> None:
    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        class Stale(MockLLMProvider):
            async def generate_structured(self, task: str, schema: type, inputs: dict) -> dict:
                started.set()
                await release.wait()
                return await super().generate_structured(task, schema, inputs)

        task = asyncio.create_task(
            captured.service.start(
                captured.source_id,
                request_id="stale",
                provider=Stale(captured.response(statement="stale result")),
            )
        )
        await started.wait()
        job_id = captured.job_id()
        with connect(captured.config.database_path) as connection:
            connection.execute(
                "UPDATE enrichment_inputs SET lease_expires_at=0 WHERE job_id=?", (job_id,)
            )
        current = await captured.service.retry(
            job_id, provider=MockLLMProvider(captured.response(statement="current result"))
        )
        release.set()
        with pytest.raises(RuminateError) as stale:
            await task
        assert stale.value.code == "job_lease_lost"
        assert captured.service.get(job_id) == current
        assert current["attempts"] == 2

    asyncio.run(scenario())


def test_process_crash_leaves_recoverable_lease(captured: Captured) -> None:
    script = """
import asyncio, os, sys
from pathlib import Path
from ruminate.config import Config
from ruminate.enrichment import EnrichmentService
class Crash:
    name = "no_llm"
    external = False
    async def generate_structured(self, task, schema, inputs):
        os._exit(23)
asyncio.run(EnrichmentService(Config(Path(sys.argv[1]), network_mode="off")).start(
    sys.argv[2], request_id="crash", provider=Crash()))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(captured.config.data_dir), captured.source_id],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "RUMINATE_NETWORK_MODE": "off"},
    )
    assert process.returncode == 23
    job_id = captured.job_id()
    assert captured.service.get(job_id)["status"] == "queued"
    with pytest.raises(RuminateError) as busy:
        asyncio.run(captured.service.retry(job_id))
    assert busy.value.code == "job_busy"
    with connect(captured.config.database_path) as connection:
        connection.execute(
            "UPDATE enrichment_inputs SET lease_expires_at=0 WHERE job_id=?", (job_id,)
        )
    recovered = asyncio.run(captured.service.retry(job_id))
    assert recovered["status"] == "enriched" and recovered["attempts"] == 2
    captured.source_unchanged()
    captured.no_claims_or_beliefs()


def test_no_llm_handles_long_leading_whitespace_without_empty_quote() -> None:
    text = " " * 600 + "A bounded excerpt after indentation."
    inputs = {"blocks": [{"block_id": "block", "text": text}]}
    result = asyncio.run(NoLLMProvider().generate_structured("propose_claims", dict, inputs))
    quote = result["proposals"][0]["quote"]
    assert quote.strip() and quote in text and len(quote) <= 500
    assert inputs["blocks"][0]["text"] == text


def test_provider_suppressing_timeout_cancellation_cannot_commit_late_success(
    captured: Captured,
) -> None:
    class Late(MockLLMProvider):
        async def generate_structured(self, task: str, schema: type, inputs: dict) -> dict:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return self.response
            return self.response

    result = asyncio.run(
        captured.service.start(
            captured.source_id, request_id="late", provider=Late(captured.response()), timeout=0.01
        )
    )
    assert result["status"] == "needs_attention" and result["error_code"] == "provider_timeout"
    assert result["result"] is None
    captured.no_claims_or_beliefs()
