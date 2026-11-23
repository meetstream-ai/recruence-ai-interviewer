from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.llm.offline import OfflineAI
from app.models import CompetencyInput, InterviewSetup, utcnow
from app.planning import build_plan
from app.store import Store, metadata
from tests.fakes import MEET_LINK


# Run the whole suite against another database (e.g. PostgreSQL) instead of a SQLite file per test:
#   TEST_DATABASE_URL=postgresql://mia:mia@localhost:5432/mia_test pytest
# Every table in that database is dropped before each test, so never point it at real data.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture(autouse=True)
def _empty_test_database():
    if TEST_DATABASE_URL:
        s = Store(TEST_DATABASE_URL)
        with s._engine.begin() as c:
            metadata.drop_all(c)
        s.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=TEST_DATABASE_URL,
        log_level="WARNING",
        meetstream_api_key="ms-key",
        public_base_url="https://tunnel.example.com",
        scheduler_interval_seconds=3600,
        goodbye_grace_seconds=0.01,
        postcall_transcript=False,  # enabled explicitly in the post-call tests
        check_public_url=False,  # no DNS in tests; covered explicitly in test_manager
        allow_shared_sqlite=True,  # most tests share workspaces on SQLite; tests/test_shared_database.py checks the rule
        # An explicit voice setup (as an installation that saved one has); tests/test_providers.py covers the rest.
        mia_stt_provider="xai", mia_llm_provider="xai", mia_llm_model="grok-4.20-0309-non-reasoning", mia_tts_provider="xai", mia_voice="ara",
    )


@pytest.fixture
def store(settings: Settings):
    s = Store(settings.database)
    yield s
    s.close()


@pytest.fixture
def setup() -> InterviewSetup:
    return InterviewSetup(
        role_title="Backend Engineer",
        level="Senior",
        job_description="Build and run Python APIs on PostgreSQL. Own services in production and mentor engineers.",
        candidate_name="Priya Raman",
        resume_text="Seven years building Python services at a payments company. Led a migration to PostgreSQL.",
        interview_type="mixed",
        competencies=[
            CompetencyInput(name="System design", description="Designs reliable services and weighs trade-offs."),
            CompetencyInput(name="Ownership", description="Takes responsibility for outcomes in production."),
        ],
        question_count=3,
        duration_minutes=20,
    )


@pytest.fixture
async def ready_interview(store: Store, setup: InterviewSetup):
    """An interview with an approved template plan, consent attested and a Meet link."""
    interview = store.create_interview(setup)
    plan = build_plan(setup, await OfflineAI().generate_plan(setup), "template (no AI)")
    return store.save_interview(
        interview.model_copy(
            update={
                "plan": plan,
                "plan_approved_at": utcnow(),
                "status": "ready",
                "meeting_link": MEET_LINK,
                "consent_attested_at": utcnow(),
            }
        )
    )


async def settle(predicate, timeout: float = 3.0) -> None:
    """Wait for background tasks to reach a condition."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)
