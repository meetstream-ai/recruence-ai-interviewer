"""Claude via the Anthropic Python SDK, with structured outputs (Pydantic models).

Server-side refusal fallback is on (`fallbacks="default"`): if the model declines,
the API re-runs the request on Anthropic's recommended fallback in the same call.
"""

from __future__ import annotations

import logging
import time
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from app.llm import prompts
from app.llm.base import EvaluationInput, InterviewAI, LLMError
from app.models import GeneratedPlan, InterviewSetup, LLMEvaluation, ScreeningOutput

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeAI(InterviewAI):
    is_ai = True

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-5-5",
        plan_effort: str = "medium",
        eval_effort: str = "high",
        client: anthropic.AsyncAnthropic | None = None,
    ):
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=300.0)
        self.model = model
        self.plan_effort = plan_effort
        self.eval_effort = eval_effort
        self.name = f"claude:{model}"

    async def generate_plan(self, setup: InterviewSetup) -> GeneratedPlan:
        return await self._parse(prompts.PLANNER_SYSTEM, prompts.plan_prompt(setup), GeneratedPlan, self.plan_effort, "plan")

    async def evaluate(self, data: EvaluationInput) -> LLMEvaluation:
        return await self._parse(
            prompts.EVALUATOR_SYSTEM, prompts.evaluation_prompt(data), LLMEvaluation, self.eval_effort, "evaluation"
        )

    async def screen(self, system: str, user: str) -> ScreeningOutput:
        return await self._parse(system, user, ScreeningOutput, self.plan_effort, "screening")

    async def _parse(self, system: str, user: str, output: type[T], effort: str, purpose: str) -> T:
        started = time.perf_counter()
        try:
            response = await self._client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output,
                output_config={"effort": effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("The Anthropic API key was rejected") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError("The Anthropic API key lacks permission for this model") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Claude model {self.model!r} was not found") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Claude rejected the {purpose} request: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude rate limit reached; try again shortly") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code} during {purpose}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Claude API") from e

        logger.info(
            "claude %s served_by=%s stop=%s in=%s out=%s %.1fs request_id=%s",
            purpose,
            response.model,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
            time.perf_counter() - started,
            response._request_id,
        )
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMError(f"Claude declined the {purpose} request (category: {category})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Claude's {purpose} output was cut off at max_tokens")
        if response.parsed_output is None:
            raise LLMError(f"Claude returned no structured output for the {purpose}")
        return response.parsed_output
