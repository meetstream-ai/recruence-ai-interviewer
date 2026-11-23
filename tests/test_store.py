from datetime import timedelta

from app.models import AnswerNote, EvaluationReport, Utterance, utcnow
from app.store import Store


def report(session_id, interview_id):
    now = utcnow()
    return EvaluationReport(
        session_id=session_id, interview_id=interview_id, role_title="r", candidate_name="c", platform="Google Meet",
        started_at=now, ended_at=now, duration_seconds=0, questions_planned=3, questions_asked=1, follow_ups_asked=0,
        end_reason="completed", consent_given=True, generated_by="x", notes=[], transcript=[],
    )


def test_interview_round_trip_and_listing(store, setup):
    i = store.create_interview(setup)
    assert store.get_interview(i.id).setup.candidate_name == "Priya Raman"
    store.save_interview(i.model_copy(update={"status": "ready"}))
    [row] = store.list_interviews()
    assert row["id"] == i.id and row["status"] == "ready" and row["recommendation"] is None


def test_session_data_survives_reopen(settings, setup):
    s = Store(settings.database)
    i = s.create_interview(setup)
    s.create_session("s1", i.id, "Google Meet")
    s.add_utterance("s1", Utterance(speaker="AI", role="interviewer", text="Q1", question_id="q1"))
    s.add_utterance("s1", Utterance(speaker="Priya", role="candidate", text="Answer", question_id="q1"))
    s.upsert_note("s1", AnswerNote(question_id="q1", note="", follow_ups=0))
    s.upsert_note("s1", AnswerNote(question_id="q1", note="Clear example.", follow_ups=1))
    s.push_report(report("s1", i.id))
    s.save_interview(i.model_copy(update={"latest_session_id": "s1"}))
    s.close()

    s2 = Store(settings.database)
    assert [u.text for u in s2.transcript("s1")] == ["Q1", "Answer"]
    assert s2.notes("s1") == [AnswerNote(question_id="q1", note="Clear example.", follow_ups=1)]
    assert s2.get_report("s1").session_id == "s1"
    s2.close()


def test_delete_cascades_and_returns_bots(store, setup):
    i = store.create_interview(setup)
    store.create_session("s1", i.id, "Zoom")
    store.update_session("s1", bot_id="bot-1", state="completed")
    store.add_utterance("s1", Utterance(speaker="x", role="candidate", text="secret"))
    store.push_report(report("s1", i.id))
    assert store.delete_interview(i.id) == ["bot-1"]
    assert store.get_interview(i.id) is None
    assert store.transcript("s1") == [] and store.get_report("s1") is None and store.get_session("s1") is None


def test_expired_only_finished_and_old(store, setup):
    old_done = store.save_interview(store.create_interview(setup).model_copy(update={"status": "completed"}))
    store.save_interview(store.create_interview(setup).model_copy(update={"status": "ready"}))
    later = utcnow() + timedelta(days=31)
    assert store.expired_interviews(30, later) == [old_done.id]
    assert store.expired_interviews(30) == []
    assert store.expired_interviews(0, later) == []


def test_due_and_unfinished(store, setup):
    i = store.create_interview(setup)
    store.save_interview(i.model_copy(update={"status": "scheduled", "scheduled_at": utcnow() + timedelta(minutes=5)}))
    assert store.due_interviews(utcnow()) == []
    assert [x.id for x in store.due_interviews(utcnow() + timedelta(minutes=6))] == [i.id]
    store.create_session("s1", i.id, "Google Meet")
    store.create_session("s2", i.id, "Google Meet")
    store.update_session("s2", state="completed")
    assert [s["id"] for s in store.unfinished_sessions()] == ["s1"]


def test_search_and_recommendation_filter(store, setup):
    a = store.create_interview(setup.model_copy(update={"candidate_name": "Ana Silva"}))
    b = store.create_interview(setup.model_copy(update={"candidate_name": "Sam Okafor"}))
    for i in (a, b):
        store.create_session(f"s-{i.id}", i.id, "Google Meet")
        store.save_interview(i.model_copy(update={"latest_session_id": f"s-{i.id}"}))
    store.push_report(report(f"s-{a.id}", a.id))  # no evaluation: recommendation stays empty
    assert [s["candidate_name"] for s in store.list_interviews(q="ana")] == ["Ana Silva"]
    assert {s["candidate_name"] for s in store.list_interviews(recommendation="none")} == {"Ana Silva", "Sam Okafor"}


def test_audit_has_no_candidate_content_columns(store, setup):
    i = store.create_interview(setup)
    store.audit("interview_created", i.id)
    [row] = store.audit_log()
    assert set(row) == {"id", "ts", "action", "interview_id", "session_id", "detail", "actor", "workspace_id"}  # actor: the recruiter, not the candidate
    assert row["actor"] == ""  # no request: the app itself


def test_audit_actor_column_added_to_old_databases(tmp_path, setup):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT NOT NULL, "
               "interview_id TEXT, session_id TEXT, detail TEXT NOT NULL DEFAULT '')")
    db.execute("INSERT INTO audit_events (ts, action) VALUES ('2026-10-01T00:00:00', 'old_event')")
    db.commit()
    db.close()
    store = Store(path)
    from app.store import CURRENT_ACTOR
    token = CURRENT_ACTOR.set("ana@example.com (recruiter)")
    try:
        store.audit("interview_created", "i1")
    finally:
        CURRENT_ACTOR.reset(token)
    assert [(r["action"], r["actor"]) for r in store.audit_log()] == [("interview_created", "ana@example.com (recruiter)"), ("old_event", "")]


def test_old_sqlite_database_is_upgraded(tmp_path, setup):
    """A file from before schema versioning: no role_id, recommendation or note-order columns."""
    import sqlite3

    from app.models import LLMEvaluation
    from app.store import SCHEMA_VERSION

    path = tmp_path / "before.db"
    s = Store(path)
    role = s.create_role(setup)
    i = s.create_interview(setup, role_id=role.id)
    s.create_session("s1", i.id, "Google Meet")
    s.save_interview(i.model_copy(update={"latest_session_id": "s1"}))
    s.upsert_note("s1", AnswerNote(question_id="q2", note="second asked first", follow_ups=0))
    s.upsert_note("s1", AnswerNote(question_id="q1", note="then this", follow_ups=1))
    ev = LLMEvaluation(recommendation="yes", confidence="medium", summary="s", competency_scores=[], strengths=[],
                       areas_for_improvement=[], suggested_next_round_focus=[])
    s.push_report(report("s1", i.id).model_copy(update={"evaluation": ev}))
    s.close()
    db = sqlite3.connect(path)  # take the file back to the first schema
    db.executescript("""
        DROP INDEX ix_interviews_role_id; DROP TABLE schema_version;
        ALTER TABLE interviews DROP COLUMN role_id; ALTER TABLE reports DROP COLUMN recommendation;
        ALTER TABLE notes DROP COLUMN position;
    """)
    db.close()

    s = Store(path)
    assert s.ping() == SCHEMA_VERSION
    assert s.role_candidate_counts() == {role.id: 1}
    assert [x.id for x in s.interviews_for_role(role.id)] == [i.id]
    assert [r["recommendation"] for r in s.list_interviews()] == ["yes"]
    assert [n.question_id for n in s.notes("s1")] == ["q2", "q1"]
    s.close()
    assert Store(path).ping() == SCHEMA_VERSION  # opening again changes nothing


def test_database_urls_and_description(tmp_path):
    from app.store import database_url, describe

    assert database_url("postgres://u:p@db:5432/mia") == "postgresql+psycopg://u:p@db:5432/mia"
    assert database_url("postgresql://u:p@db/mia") == "postgresql+psycopg://u:p@db/mia"
    assert database_url("postgresql+psycopg://u:p@db/mia") == "postgresql+psycopg://u:p@db/mia"
    assert database_url(":memory:") == "sqlite://"
    assert database_url(tmp_path / "x.db").startswith("sqlite:///")
    assert describe("postgresql://mia:s3cret@db.internal:5432/mia") == "PostgreSQL at db.internal:5432/mia as mia"
    assert "s3cret" not in describe("postgresql://mia:s3cret@db.internal:5432/mia")
    assert describe(tmp_path / "x.db").startswith("SQLite, ")


def test_unsupported_database_is_refused():
    import pytest

    with pytest.raises(ValueError, match="SQLite .* or PostgreSQL"):
        Store("mysql://u:p@localhost/mia")


def test_copy_tool_moves_everything_and_refuses_a_full_target(tmp_path, store, setup):
    import pytest

    from scripts.copy_db import copy

    i = store.create_interview(setup)
    store.create_session("s1", i.id, "Google Meet")
    store.add_utterance("s1", Utterance(speaker="AI", role="interviewer", text="Q1", question_id="q1"))
    store.upsert_note("s1", AnswerNote(question_id="q1", note="n", follow_ups=0))
    store.push_report(report("s1", i.id))
    store.audit("interview_created", i.id)
    store.set_booking_token(i.id, "booking-token-1234567890")
    store.save_setting("bot_name", '"Hiring Assistant"', secret=False)
    source = tmp_path / "source.db"
    src = Store(source)
    from app.store import database_owner, installations, metadata, schema_version
    import sqlalchemy as sa
    tables = [x for x in metadata.sorted_tables if x not in (schema_version, database_owner, installations)]  # each database has its own
    with store._engine.connect() as a, src._engine.begin() as b:  # the fixture's database may be PostgreSQL
        for t in tables:
            rows = [dict(r._mapping) for r in a.execute(sa.select(t))]
            if rows:
                b.execute(t.insert(), rows)
    src.close()
    target = tmp_path / "target.db"
    counts = copy(str(source), str(target))
    # Every table goes, not only the interview data: booking links and saved settings too (they were left behind before).
    assert {k: v for k, v in counts.items() if v} == {"interviews": 1, "sessions": 1, "utterances": 1, "notes": 1, "reports": 1,
                                                      "audit_events": 1, "bookings": 1, "app_settings": 1}
    assert {"applications", "invite_tokens", "app_state"} <= set(counts)
    t = Store(target)
    assert t.get_interview(i.id).setup.candidate_name == "Priya Raman" and [u.text for u in t.transcript("s1")] == ["Q1"]
    t.audit("next_event")
    assert [r["action"] for r in t.audit_log()] == ["next_event", "interview_created"]
    t.close()
    with pytest.raises(SystemExit, match="already holds data"):
        copy(str(source), str(target))


def test_activity_lists_what_happened_since(store, setup):
    from datetime import datetime, timezone

    old = store.create_interview(setup)
    store.save_interview(old.model_copy(update={"created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}))
    new = store.create_interview(setup)
    store.create_session("s1", new.id, "Google Meet")
    store.update_session("s1", state="completed", ended_at=utcnow())
    store.create_session("s2", new.id, "Google Meet")
    store.save_interview(store.get_interview(new.id).model_copy(update={"decision": "advance", "decided_at": utcnow()}))
    a = store.activity(utcnow() - timedelta(days=7))
    assert len(a["created"]) == 1 and len(a["run"]) == 2 and len(a["completed"]) == 1 and len(a["decided"]) == 1
    assert all(t.endswith("+00:00") for t in a["run"])
