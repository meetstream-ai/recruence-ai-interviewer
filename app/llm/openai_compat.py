"""OpenAI and OpenAI-compatible servers (Ollama, vLLM, LM Studio, ...) via the Chat Completions API.

Plain HTTP with httpx, no SDK, so any server that speaks `POST {base_url}/chat/completions`
works. The answer is requested as JSON matching the output schema (`response_format`
json_schema, not strict, since the schemas have optional fields). A server that rejects
json_schema is asked once more with `json_object` and the schema in the instructions.
Either way the answer is validated with Pydantic, as for Claude and Gemini.

Self-hosted models keep candidate data on your own machines; check the data terms of any
hosted provider before sending real candidate data.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import OPENAI_DEFAULT_BASE_URL
from app.llm import prompts
from app.llm.base import EvaluationInput, InterviewAI, LLMError
from app.models import GeneratedPlan, InterviewSetup, LLMEvaluation, ScreeningOutput

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
TIMEOUT_S = 180.0  # self-hosted models on modest hardware can take minutes for an evaluation
MAX_TOKENS = 16000


class _SchemaUnsupported(Exception):
    pass


class OpenAICompatibleAI(InterviewAI):
    is_ai = True

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = OPENAI_DEFAULT_BASE_URL,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=TIMEOUT_S))
        hosted = self.base_url == OPENAI_DEFAULT_BASE_URL
        self.name = f"openai:{model}" if hosted else f"openai-compatible:{model}"

    async def generate_plan(self, setup: InterviewSetup) -> GeneratedPlan:
        return await self._generate(prompts.PLANNER_SYSTEM, prompts.plan_prompt(setup), GeneratedPlan, "plan")

    async def evaluate(self, data: EvaluationInput) -> LLMEvaluation:
        return await self._generate(prompts.EVALUATOR_SYSTEM, prompts.evaluation_prompt(data), LLMEvaluation, "evaluation")

    async def screen(self, system: str, user: str) -> ScreeningOutput:
        return await self._generate(system, user, ScreeningOutput, "screening")

    async def _generate(self, system: str, user: str, output: type[T], purpose: str) -> T:
        schema = output.model_json_schema()
        started = time.perf_counter()
        async with self._client_factory() as client:
            try:
                text = await self._ask(client, system, user, purpose,
                                       {"type": "json_schema", "json_schema": {"name": purpose, "schema": schema, "strict": False}})
            except _SchemaUnsupported:
                logger.info("%s: the server doesn't take a JSON schema; asking for plain JSON instead", self.name)
                text = await self._ask(client, f"{system}\n\nAnswer with one JSON object matching this JSON schema:\n{json.dumps(schema)}",
                                       user, purpose, {"type": "json_object"})
        logger.info("%s %s %.1fs", self.name, purpose, time.perf_counter() - started)
        self.last_served_by = self.name
        try:
            return output.model_validate_json(_strip_fences(text))
        except ValidationError as e:
            raise LLMError(f"{self.name}'s {purpose} did not match the expected format") from e

    async def _ask(self, client: httpx.AsyncClient, system: str, user: str, purpose: str, response_format: dict) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": response_format,
            "max_tokens": MAX_TOKENS,
        }
        try:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise LLMError(f"{self.name} timed out during the {purpose}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Couldn't reach {self.base_url}: {type(e).__name__}") from e
        if resp.status_code in (401, 403):
            raise LLMError(f"{self.name} rejected the API key")
        if resp.status_code == 400 and response_format["type"] == "json_schema" and _about_response_format(resp.text):
            raise _SchemaUnsupported()
        if resp.status_code == 429:
            raise LLMError(f"{self.name} is rate-limited or out of quota (429)")
        if resp.status_code >= 400:
            raise LLMError(f"{self.name} rejected the {purpose} request ({resp.status_code}): {resp.text[:300]}")
        try:
            choice = resp.json()["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self.name} returned an unexpected response for the {purpose}") from e
        if message.get("refusal"):
            raise LLMError(f"{self.name} refused the {purpose}: {str(message['refusal'])[:200]}")
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{self.name}'s {purpose} output was cut off")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError(f"{self.name} returned no answer for the {purpose}")
        return content


def _about_response_format(text: str) -> bool:
    lowered = text.lower()
    return "response_format" in lowered or "json_schema" in lowered


def _strip_fences(text: str) -> str:
    """Some local models wrap JSON in a ```json fence even when asked for JSON."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
