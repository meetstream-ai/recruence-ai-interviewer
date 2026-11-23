from app.config import Settings
from app.llm.base import EvaluationInput, InterviewAI, LLMError
from app.llm.offline import OfflineAI


def build_ai(settings: Settings) -> InterviewAI:
    provider = settings.resolved_llm
    if provider == "claude":
        from app.llm.claude import ClaudeAI

        return ClaudeAI(
            settings.anthropic_api_key,
            model=settings.claude_model,
            plan_effort=settings.claude_plan_effort,
            eval_effort=settings.claude_eval_effort,
        )
    if provider == "gemini":
        from app.llm.gemini import GeminiAI

        return GeminiAI(
            settings.gemini_api_key,
            model=settings.gemini_model,
            fallback_models=settings.gemini_fallback_models,
            plan_thinking=settings.gemini_plan_thinking,
            eval_thinking=settings.gemini_eval_thinking,
        )
    if provider == "openai":
        from app.llm.openai_compat import OpenAICompatibleAI

        return OpenAICompatibleAI(settings.openai_api_key, model=settings.openai_model, base_url=settings.openai_base_url)
    if provider == "xai":
        from app.config import XAI_BASE_URL
        from app.llm.openai_compat import OpenAICompatibleAI

        return OpenAICompatibleAI(settings.xai_api_key, model=settings.xai_model, base_url=XAI_BASE_URL)
    if provider == "ollama":
        from app.llm.openai_compat import OpenAICompatibleAI

        return OpenAICompatibleAI("", model=settings.ollama_model, base_url=f"{settings.ollama_base_url.rstrip('/')}/v1")
    return OfflineAI()


class SwitchableAI(InterviewAI):
    """The app's AI, replaceable while the app runs: changing the provider in Settings swaps the client
    inside, and everything holding this object (routes, the session manager) uses the new one."""

    def __init__(self, inner: InterviewAI):
        self.inner = inner

    def swap(self, inner: InterviewAI) -> None:
        self.inner = inner

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def is_ai(self) -> bool:
        return self.inner.is_ai

    @property
    def last_served_by(self):
        return self.inner.last_served_by

    @last_served_by.setter
    def last_served_by(self, value) -> None:
        self.inner.last_served_by = value

    async def generate_plan(self, setup):
        return await self.inner.generate_plan(setup)

    async def evaluate(self, data):
        return await self.inner.evaluate(data)

    async def screen(self, system, user):
        return await self.inner.screen(system, user)


__all__ = ["EvaluationInput", "InterviewAI", "LLMError", "OfflineAI", "SwitchableAI", "build_ai"]
