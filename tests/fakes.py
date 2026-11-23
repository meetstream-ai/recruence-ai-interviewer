"""Test doubles: a fake MeetStream client (the calls this app makes) and a scripted LLM."""

from __future__ import annotations

import meetstream

from app.llm.base import EvaluationInput, InterviewAI, LLMError
from app.models import CompetencyScore, GeneratedPlan, InterviewSetup, LLMEvaluation

MEET_LINK = "https://meet.google.com/abc-defg-hij"


class FakeBots:
    def __init__(self, status="Joining", create_error=None, remove_error=None, send_error=None):
        self.created: list[dict] = []
        self.removed: list[str] = []
        self.erased: list[str] = []
        self.status_value = status
        self.create_error = create_error
        self.remove_error = remove_error
        self.send_error = send_error
        self.messages: list[tuple[str, str]] = []
        self.bot_id = "bot-123"
        self.streams: list = []  # get_recording_streams (proctored interviews)
        self.streams_error = None

    async def create(self, params, *, idempotency_key=None):
        if self.create_error:
            raise self.create_error
        self.created.append({"params": params, "idempotency_key": idempotency_key})
        return {"bot_id": self.bot_id, "transcript_id": None, "status": "Active"}

    async def status(self, bot_id):
        return {"bot_id": bot_id, "status": self.status_value}

    async def remove(self, bot_id):
        self.removed.append(bot_id)
        if self.remove_error:
            raise self.remove_error
        return {"status": "ok"}

    async def delete_data(self, bot_id):
        self.erased.append(bot_id)
        return {"status": "deleted"}

    async def send_message(self, bot_id, message):
        if self.send_error:
            raise self.send_error
        self.messages.append((bot_id, message))
        return {"status": "accepted"}

    async def recording_streams(self, bot_id):
        if self.streams_error:
            raise self.streams_error
        return self.streams

    async def wait_for_audio(self, bot_id, *, timeout=600.0, interval=5.0):
        return {"audio_url": "https://example.com/audio.mp3"}


class FakeTranscripts:
    """Post-call transcription: returns `post` (the documented participant-keyed shape) or raises `error`."""

    def __init__(self, post=None, error=None):
        self.post, self.error = post, error
        self.requested: list[tuple[str, dict]] = []

    async def transcribe_bot_audio(self, bot_id, body=None):
        self.requested.append((bot_id, body))
        if self.error:
            raise self.error
        return {"bot_id": bot_id, "transcript_id": "tr-1", "provider": "meetstream", "message": "Transcription processing started"}

    async def wait_for(self, transcript_id, *, timeout=900.0, interval=5.0):
        return self.post


def chunks(*phrases, speaker="Priya Raman"):
    """A post-call transcript where every phrase is credited to the human participant, as Google Meet does."""
    words, t = [], 0.0
    for p in phrases:
        words.append({"text": p, "start_timestamp": {"relative": t, "absolute": t}, "end_timestamp": {"relative": t + 2, "absolute": t + 2}})
        t += 2.5
    return [{"participant": {"id": 100, "name": speaker, "platform": "google_meet"}, "words": words, "language_code": "en"}]


class FakeMia:
    def __init__(self, create_error=None):
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.create_error = create_error

    async def create(self, params):
        if self.create_error:
            raise self.create_error
        self.created.append(params)
        return {"agent_config_id": f"cfg-{len(self.created)}"}

    async def delete(self, agent_config_id):
        self.deleted.append(agent_config_id)
        return {}

    async def list(self):
        return []


class FakeMeetStream:
    def __init__(self, mia_create_error=None, post=None, post_error=None, **bot_kwargs):
        self.bots = FakeBots(**bot_kwargs)
        self.mia = FakeMia(mia_create_error)
        self.transcripts = FakeTranscripts(post, post_error)

    async def aclose(self):
        pass


def event(name, bot_id="bot-123", status="InMeeting", message="ok"):
    return {"event": "bot.stopped" if name in {"bot.kicked", "bot.denied", "bot.notallowed", "bot.failed"} else name,
            "bot_event": name, "bot_id": bot_id, "bot_status": status, "message": message, "status_code": 200}


def sentence(text, speaker="Priya Raman", ts=None, bot_id="bot-123"):
    return {"bot_id": bot_id, "speakerName": speaker, "speakerId": "1", "timestamp": ts or text,
            "utterance": text, "transcript": text, "transcription_mode": "sentence"}


class FakeAI(InterviewAI):
    """Scripted plan and evaluation; records what it was asked."""

    name = "fake-ai"
    is_ai = True

    def __init__(self, plan: GeneratedPlan | None = None, plan_error: str | None = None, eval_error: str | None = None,
                 evidence: list[str] | None = None, score: int = 4):
        self.plan, self.plan_error, self.eval_error = plan, plan_error, eval_error
        self.evidence = evidence or []
        self.score = score
        self.plan_requests: list[InterviewSetup] = []
        self.evaluations: list[EvaluationInput] = []

    async def generate_plan(self, setup):
        self.plan_requests.append(setup)
        if self.plan_error:
            raise LLMError(self.plan_error)
        return self.plan

    screening = None  # a ScreeningOutput, or a function (system, user) -> ScreeningOutput
    screenings: list = []

    async def screen(self, system, user):
        self.screenings = [*self.screenings, user]
        if self.screening is None:
            raise LLMError("no screening scripted")
        return self.screening(system, user) if callable(self.screening) else self.screening

    async def evaluate(self, data):
        self.evaluations.append(data)
        if self.eval_error:
            raise LLMError(self.eval_error)
        return LLMEvaluation(
            recommendation="yes",
            summary="Solid answers.",
            competency_scores=[
                CompetencyScore(competency_id=c.id, competency_name="model name", score=self.score, rationale="r", evidence=list(self.evidence))
                for c in data.context.competencies
            ],
            strengths=["Clear examples"],
            areas_for_improvement=["More metrics"],
            suggested_next_round_focus=[],
            confidence="medium",
        )


def conflict():
    return meetstream.ConflictError("conflict", status=409)
