"""A human-led role (Interview mode: Human-led, i.e. Meeting Analyst): a member of the hiring team interviews at a time
they arrange, so the candidate's link waits for that time instead of starting anything, and every text says a person
leads it."""

from datetime import timedelta

from app.models import utcnow
from tests.test_api import TUNNEL, wait_for
from tests.test_gateway import invited, verified_client


def test_a_human_led_interview_waits_for_the_time_the_team_arranges(settings):
    app, c, store, sent, interview, token = invited(settings, options={"mode": "analyst"})
    try:
        assert interview.options.mode == "analyst" and interview.assigned == "automatic"  # the role's mode, copied
        invitation = wait_for(lambda: [e for e in sent if "/i/" in e.body])[0]
        assert "a member of our hiring team" in invitation.body and "AI interviewer" not in invitation.body
        assert "arrange the time" in invitation.body and "note-taking assistant" in invitation.body

        d = verified_client(app, token, sent)
        view = d.get(f"/api/public/interview/{token}", headers=TUNNEL).json()
        assert view["mode"] == "analyst" and view["state"] == "awaiting_time" and view["meeting_link"] is None
        r = d.post(f"/api/public/interview/{token}/start", json={"acknowledged": True}, headers=TUNNEL)
        assert r.status_code == 409 and "hiring team leads this interview" in r.json()["detail"]
        assert app.state.manager.live == {}  # nothing was started

        # The recruiter arranges the time (Run the interview, on the interview page): the link then opens at it.
        when = utcnow() + timedelta(minutes=5)
        launched = c.post(f"/api/interviews/{interview.id}/launch", json={
            "meeting_link": "https://meet.google.com/abc-defg-hij", "consent_attested": True, "mode": "analyst",
            "scheduled_at": when.isoformat()})
        assert launched.status_code == 200 and launched.json()["session_id"] is None
        joined = d.post(f"/api/public/interview/{token}/start", json={"acknowledged": True}, headers=TUNNEL).json()
        assert joined["state"] == "ready_to_join" and joined["meeting_link"] == "https://meet.google.com/abc-defg-hij"
        assert app.state.manager.live == {}  # the scheduler sends the silent assistant at the time
    finally:
        c.__exit__(None, None, None)


def test_an_ai_led_interview_still_starts_from_the_link(settings):
    app, c, store, sent, interview, token = invited(settings)
    try:
        d = verified_client(app, token, sent)
        view = d.get(f"/api/public/interview/{token}", headers=TUNNEL).json()
        assert view["mode"] == "ai" and view["state"] == "open"
    finally:
        c.__exit__(None, None, None)


def test_the_question_count_follows_the_edited_plan(settings):
    """Seen in a full run: a question added on the role's Questions step, but the email said "4 questions"."""
    from app.mailer import questions_asked
    from app.recruiting import invitation_email
    app, c, store, sent, interview, token = invited(settings)
    try:
        i = store.get_interview(interview.id)
        planned = i.setup.question_count
        extra = i.plan.questions[0].model_copy(update={"id": "q99", "text": "Tell me about a slow API you made faster."})
        i = store.save_interview(i.model_copy(update={"plan": i.plan.model_copy(update={"questions": [*i.plan.questions, extra]})}))
        assert questions_asked(i) == len(i.plan.questions) == planned + 1
        assert f"{planned + 1} questions" in invitation_email(i, app.state.mailer.settings, token).body
        d = verified_client(app, token, sent)
        assert d.get(f"/api/public/interview/{token}", headers=TUNNEL).json()["question_count"] == planned + 1
    finally:
        c.__exit__(None, None, None)
