"""The OpenAI-compatible adapter (OpenAI, Ollama, vLLM, ...) against a fake Chat Completions server."""

import json
from dataclasses import replace

import httpx
import pytest

from app.config import OPENAI_DEFAULT_BASE_URL
from app.llm import OfflineAI, build_ai
from app.llm.base import EvaluationInput, LLMError
from app.llm.openai_compat import OpenAICompatibleAI
from app.models import GeneratedPlan, LLMEvaluation, Utterance
from app.planning import context_for

PLAN = GeneratedPlan(candidate_summary="s", competencies=[{"name": "Ownership", "description": "d"}],
                     questions=[{"text": "Tell me about an outage.", "competency_names": ["Ownership"], "look_for": ["owns it"]}])


def completion(content, finish="stop", refusal=None):
    return {"choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content, "refusal": refusal}}]}


def server(*replies):
    """A fake /chat/completions: each reply is (status, json body or text). Records the requests."""
    seen, queue = [], list(replies)

    def handler(request):
        seen.append(request)
        status, body = queue.pop(0)
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

    return seen, lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ai(factory, key="sk-test", base_url=OPENAI_DEFAULT_BASE_URL, model="gpt-4.1"):
    return OpenAICompatibleAI(key, model=model, base_url=base_url, client_factory=factory)


async def test_plan_request_shape(setup):
    seen, factory = server((200, completion(PLAN.model_dump_json())))
    model = ai(factory)
    assert await model.generate_plan(setup) == PLAN
    [req] = seen
    assert str(req.url) == "https://api.openai.com/v1/chat/completions" and req.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "gpt-4.1" and [m["role"] for m in body["messages"]] == ["system", "user"]
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["schema"] == GeneratedPlan.model_json_schema()
    assert fmt["json_schema"]["strict"] is False
    assert model.name == "openai:gpt-4.1" and model.served_name == "openai:gpt-4.1"


async def test_self_hosted_server_without_key_and_fenced_json(setup, ready_interview):
    seen, factory = server((200, completion("```json\n" + PLAN.model_dump_json() + "\n```")))
    local = ai(factory, key="", base_url="http://localhost:11434/v1/", model="qwen2.5:7b")
    assert await local.generate_plan(setup) == PLAN
    assert str(seen[0].url) == "http://localhost:11434/v1/chat/completions" and "Authorization" not in seen[0].headers
    assert local.name == "openai-compatible:qwen2.5:7b"


async def test_falls_back_to_json_object_when_schema_is_rejected(ready_interview):
    ev = LLMEvaluation(recommendation="yes", summary="s", competency_scores=[], strengths=[], areas_for_improvement=[],
                       suggested_next_round_focus=[], confidence="low")
    seen, factory = server((400, "Unsupported value: 'response_format.type' json_schema"), (200, completion(ev.model_dump_json())))
    data = EvaluationInput(context_for(ready_interview), [Utterance(speaker="P", role="candidate", text="x")], [], [])
    assert await ai(factory).evaluate(data) == ev
    second = json.loads(seen[1].content)
    assert second["response_format"] == {"type": "json_object"} and "JSON schema" in second["messages"][0]["content"]


@pytest.mark.parametrize("reply, match", [
    ((401, {"error": "bad key"}), "rejected the API key"),
    ((429, {"error": "quota"}), "rate-limited or out of quota"),
    ((500, "boom"), r"rejected the plan request \(500\)"),
    ((200, completion("", finish="length")), "cut off"),
    ((200, completion(None, refusal="I can't help with that")), "refused"),
    ((200, completion("not json")), "did not match the expected format"),
    ((200, {"unexpected": True}), "unexpected response"),
    ((200, httpx.ConnectError("refused")), "Couldn't reach"),
    ((200, httpx.ReadTimeout("slow")), "timed out"),
])
async def test_failures_become_llm_errors(setup, reply, match):
    _, factory = server(reply)
    with pytest.raises(LLMError, match=match):
        await ai(factory).generate_plan(setup)


@pytest.mark.parametrize("provider, key, base_url, expected", [
    ("openai", "sk", OPENAI_DEFAULT_BASE_URL, OpenAICompatibleAI),
    ("openai", "", OPENAI_DEFAULT_BASE_URL, OfflineAI),          # OpenAI itself needs a key
    ("openai", "", "http://localhost:11434/v1", OpenAICompatibleAI),  # a local server usually doesn't
    ("auto", "sk", OPENAI_DEFAULT_BASE_URL, OpenAICompatibleAI),  # used by auto when it's the only key
])
def test_provider_selection(settings, provider, key, base_url, expected):
    s = replace(settings, llm_provider=provider, anthropic_api_key="", gemini_api_key="", openai_api_key=key, openai_base_url=base_url)
    assert isinstance(build_ai(s), expected)
    assert build_ai(replace(s, llm_provider="auto", gemini_api_key="g")).name.startswith("gemini:")  # auto: Gemini before OpenAI
