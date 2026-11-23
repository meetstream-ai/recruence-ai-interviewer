"""Gemini via the google-genai SDK, with a JSON schema and Pydantic validation.

On overload (5xx), rate limits (429) or timeouts the call moves on to the next model
in GEMINI_FALLBACK_MODELS instead of retrying the same one.

Note: on Google's free tier, submitted content may be used to improve Google's products.
Don't send real candidate data on the free tier.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence, TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.llm import prompts
from app.llm.base import EvaluationInput, InterviewAI, LLMError
from app.models import GeneratedPlan, InterviewSetup, LLMEvaluation, ScreeningOutput

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
TIMEOUT_MS = 90_000  # the last model in the chain: nothing to fall back to, so it gets the full wait
# A model with another one behind it gets less: a hung model (seen for days with one model: 504 after 90 s on every
# call) shouldn't cost every plan and screening a minute and a half before the fallback answers in seconds.
EARLY_TIMEOUT_MS = {"plan": 35_000, "screening": 35_000, "evaluation": 60_000}
COOL_OFF_SECONDS = 600  # a model that timed out or was overloaded is skipped this long while another one works


class _TryNext(Exception):
    pass


class GeminiAI(InterviewAI):
    is_ai = True

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.6-flash",
        fallback_models: Sequence[str] = (),
        client: genai.Client | None = None,
        plan_thinking: str = "low",
        eval_thinking: str = "medium",
    ):
        # Gemini 3 thinks at a high level by default; measured 2026-10-29 that made plans take 79 s and
        # evaluations time out. Explicit levels keep both to seconds.
        self.thinking = {"plan": plan_thinking, "evaluation": eval_thinking}
        self._client = client or genai.Client(
            api_key=api_key, http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))
        )
        self.model = model
        self.fallback_models = [m for m in fallback_models if m and m != model]
        self.name = f"gemini:{model}"
        self._cooling: dict[str, float] = {}  # model -> when it may be tried again (time.monotonic)

    async def generate_plan(self, setup: InterviewSetup) -> GeneratedPlan:
        return await self._generate(prompts.PLANNER_SYSTEM, prompts.plan_prompt(setup), GeneratedPlan, "plan")

    async def evaluate(self, data: EvaluationInput) -> LLMEvaluation:
        return await self._generate(prompts.EVALUATOR_SYSTEM, prompts.evaluation_prompt(data), LLMEvaluation, "evaluation")

    async def screen(self, system: str, user: str) -> ScreeningOutput:
        return await self._generate(system, user, ScreeningOutput, "screening")

    async def _generate(self, system: str, user: str, output: type[T], purpose: str) -> T:
        failures = []
        chain = [self.model, *self.fallback_models]
        now = time.monotonic()
        # Models that just failed are skipped while they cool off, unless that would leave nothing to try.
        ready = [m for m in chain if self._cooling.get(m, 0) <= now] or chain
        for n, model in enumerate(ready):
            last = n == len(ready) - 1
            try:
                result = await self._once(model, system, user, output, purpose,
                                          TIMEOUT_MS if last else EARLY_TIMEOUT_MS.get(purpose, TIMEOUT_MS))
                self.last_served_by = f"gemini:{model}"
                return result
            except _TryNext as e:
                self._cooling[model] = time.monotonic() + COOL_OFF_SECONDS
                failures.append(f"{model}: {e}")
                logger.warning("gemini %s: %s unavailable (%s)", purpose, model, e)
        raise LLMError(f"No Gemini model was available for the {purpose}: " + "; ".join(failures))

    async def _once(self, model: str, system: str, user: str, output: type[T], purpose: str, timeout_ms: int = TIMEOUT_MS) -> T:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=output.model_json_schema(),
            max_output_tokens=16000,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
        level = self.thinking.get(purpose)
        if level and not model.startswith("gemini-2"):  # thinking levels are a Gemini 3 setting
            config.thinking_config = types.ThinkingConfig(thinking_level=level.upper())
        started = time.perf_counter()
        try:
            response = await self._client.aio.models.generate_content(model=model, contents=user, config=config)
        except errors.ClientError as e:
            if e.code in (401, 403):
                raise LLMError("The Gemini API key was rejected") from e
            if e.code == 429:
                raise _TryNext(f"429 rate limit or quota: {e.message}") from e
            if e.code == 404:
                # A model this key can't use (live call 18: gemini-2.5-flash is closed to new Google projects).
                raise _TryNext(f"404 not available to this key: {e.message}") from e
            raise LLMError(f"Gemini rejected the {purpose} request ({e.code}): {e.message}") from e
        except errors.ServerError as e:
            raise _TryNext(f"{e.code} {e.message}") from e
        except errors.APIError as e:
            raise LLMError(f"Gemini API error {e.code} during {purpose}") from e
        except httpx.TimeoutException as e:
            raise _TryNext("timed out") from e
        except httpx.HTTPError as e:
            # Seen live when a model was overloaded: a dropped connection, not a bad request. Try the next model.
            raise _TryNext(f"network error: {type(e).__name__}: {e}") from e

        candidate = response.candidates[0] if response.candidates else None
        finish = candidate.finish_reason.value if candidate and candidate.finish_reason else None
        logger.info("gemini %s model=%s finish=%s %.1fs", purpose, model, finish, time.perf_counter() - started)
        if candidate is None:
            raise LLMError(f"Gemini returned no answer for the {purpose}")
        if finish == "MAX_TOKENS":
            raise LLMError(f"Gemini's {purpose} output was cut off")
        if finish not in (None, "STOP"):
            raise LLMError(f"Gemini stopped the {purpose} early ({finish})")
        try:
            return output.model_validate_json(response.text or "")
        except ValidationError as e:
            raise LLMError(f"Gemini's {purpose} did not match the expected format") from e
