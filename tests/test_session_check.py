import os
import subprocess
import sys
from pathlib import Path

from app.models import Utterance

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "session_check.py"


def run(data_dir, *args):
    # DATABASE_URL is always set, so a value in the real .env is never used.
    env = {**os.environ, "DATA_DIR": str(data_dir), "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", ""), "LOG_LEVEL": "WARNING"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)


async def test_summarises_latest_session(settings, store, ready_interview):
    store.create_session("s1", ready_interview.id, "Google Meet")
    q = ready_interview.plan.questions[0]
    for u in (
        Utterance(speaker="AI Interviewer", role="interviewer", text="Hello Priya"),
        Utterance(speaker="Priya Raman", role="candidate", text="Yes I agree"),
        Utterance(speaker="System", role="system", text="The candidate agreed to the AI interview, recording and transcription."),
        Utterance(speaker="AI Interviewer", role="interviewer", text=q.text, question_id=q.id),
        Utterance(speaker="AI Interviewer (spoken)", role="interviewer", text=q.text, question_id=q.id),
        Utterance(speaker="Priya Raman", role="candidate", text="secret answer words", question_id=q.id),
    ):
        store.add_utterance("s1", u)
    out = run(settings.data_dir)
    assert out.returncode == 0, out.stderr
    assert "U1 live transcript with MIA attached: YES (2 lines)" in out.stdout
    assert "U2 agent speech in live transcript:   YES" in out.stdout
    assert "U3 agent confirmed consent:           YES" in out.stdout
    assert "q1=100%" in out.stdout
    assert "secret answer words" not in out.stdout
    assert "secret answer words" in run(settings.data_dir, "s1", "--text").stdout


def test_no_sessions(settings, store):
    out = run(settings.data_dir)
    assert out.returncode == 1 and "No sessions yet" in out.stdout
