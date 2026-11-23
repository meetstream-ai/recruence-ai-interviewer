"""The app's own LLM: generates interview plans and evaluates finished interviews.

The live conversation is not here: MeetStream's MIA agent runs it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models import AnswerNote, GeneratedPlan, InterviewContext, InterviewSetup, LLMEvaluation, ScreeningOutput, Utterance


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationInput:
    context: InterviewContext
    transcript: list[Utterance]
    notes: list[AnswerNote]
    questions_asked: list[str]


class InterviewAI(ABC):
    name: str
    is_ai: bool
    # The model that produced the last result (differs from `name` when a fallback served it).
    last_served_by: str | None = None

    @property
    def served_name(self) -> str:
        return self.last_served_by or self.name

    @abstractmethod
    async def generate_plan(self, setup: InterviewSetup) -> GeneratedPlan: ...

    @abstractmethod
    async def evaluate(self, data: EvaluationInput) -> LLMEvaluation: ...

    async def screen(self, system: str, user: str) -> ScreeningOutput:
        """Judge an application against a role's eligibility criteria (app/recruiting.py applies the rules)."""
        raise LLMError(f"{self.name} can't screen applications")
