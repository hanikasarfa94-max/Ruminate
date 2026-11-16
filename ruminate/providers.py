"""Optional structured proposal providers; no credentials or external client by default."""

from __future__ import annotations

import asyncio
import copy
from typing import Any, Protocol


class LLMProvider(Protocol):
    name: str
    external: bool

    async def generate_structured(
        self, task: str, schema: type, inputs: dict[str, Any]
    ) -> dict[str, Any]: ...


class NoLLMProvider:
    name = "no_llm"
    external = False

    async def generate_structured(
        self, task: str, schema: type, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        """Produce review questions with verbatim excerpts, not inferred conclusions."""
        return {
            "proposals": [
                {
                    "kind": "question",
                    "statement": "这段材料的适用条件与证据边界是什么？",
                    "block_id": block["block_id"],
                    "quote": block["text"].lstrip()[:500],
                }
                for block in inputs["blocks"][:3]
                if block["text"].strip()
            ]
        }


class MockLLMProvider:
    """Explicit test double; never advertised as actual host/model integration."""

    name = "mock"
    external = False

    def __init__(self, response: dict[str, Any], *, delay: float = 0) -> None:
        self.response = response
        self.delay = delay

    async def generate_structured(
        self, task: str, schema: type, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        await asyncio.sleep(self.delay)
        return copy.deepcopy(self.response)
