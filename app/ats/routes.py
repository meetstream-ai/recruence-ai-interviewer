"""HTTP for external ATS integrations: the public webhook, Settings → Recruitment sources, and candidate search.

Integrations belong to a workspace like roles do. Reading them is open to the workspace's members (credentials are
never returned, only whether each is set); creating, changing, testing and deleting them is for its admins.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.ats import ADAPTERS
from app.ats.base import url_problem
from app.ats.generic import DEFAULT_FIELDS
from app.ats.service import IngestError, IntegrationService, public_view
from app.models import RecruiterNote, utcnow
from app.store import Store

MAX_WEBHOOK_BYTES = 2 * 1024 * 1024
from app.ats.service import RESULT_STAGES as STAGES  # noqa: E402


class IntegrationBody(BaseModel):
    provider: Optional[str] = None  # required when creating
    name: str = Field(default="", max_length=80)
    enabled: Optional[bool] = None
    settings: dict[str, Any] = Field(default_factory=dict)
    # A value sets the credential, "" keeps the saved one, null removes it.
    secrets: dict[str, Optional[str]] = Field(default_factory=dict)


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


def _check_settings(provider: str, s: dict[str, Any], role_ok) -> None:
    for key in ("callback_url", "api_base"):
        if s.get(key):
            problem = url_problem(str(s[key]))
            if problem:
                raise HTTPException(422, f"{key.replace('_', ' ').capitalize()}: {problem}")
    for key in ("role_map", "status_map", "field_map"):
        if key in s and (not isinstance(s[key], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in s[key].items())):
            raise HTTPException(422, f"{key} must map text to text")
    for k, v in (s.get("role_map") or {}).items():
        if len(k) > 100 or not role_ok(v):
            raise HTTPException(422, f"Role mapping: {v or '?'} isn't a role in this workspace")
    if bad := [k for k in (s.get("status_map") or {}) if k not in STAGES]:
        raise HTTPException(422, f"Status mapping: unknown Recruence stage {bad[0]} (use {', '.join(STAGES)})")
    if any(len(v) > 100 for v in (s.get("status_map") or {}).values()):
        raise HTTPException(422, "Status mapping: labels can be at most 100 characters")
    if bad := [k for k in (s.get("field_map") or {}) if k not in DEFAULT_FIELDS]:
        raise HTTPException(422, f"Field mapping: unknown Recruence field {bad[0]}")
    for key in ("stage_name", "stage_id", "on_behalf_of_user_id", "perform_as_user_id"):
        if len(str(s.get(key, ""))) > 200:
            raise HTTPException(422, f"{key} is too long")


def register(app: FastAPI, svc: IntegrationService, store: Store, base_url) -> None:
    def admin(request: Request) -> None:
        user = getattr(request.state, "user", None)
        if not (user and user.role == "admin"):
            raise HTTPException(403, "Only the workspace's admins can manage recruitment sources")

    def row_or_404(integration_id: str) -> dict:
        row = store.integration(integration_id)
        if row is None:
            raise HTTPException(404, "Integration not found")
        return row

    # ---------- public: the provider's webhook ----------

    @app.post("/webhooks/ats/{token}")
    async def ats_webhook(token: str, request: Request) -> dict:
        if not 16 <= len(token) <= 64:
            raise HTTPException(404, "Unknown webhook")
        if int(request.headers.get("content-length") or 0) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Webhook too large")
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Webhook too large")
        try:
            return await svc.ingest(token, dict(request.headers), body)
        except IngestError as e:
            raise HTTPException(e.status, str(e)) from None

    # ---------- Settings → Recruitment sources ----------

    @app.get("/api/integrations/providers")
    def providers() -> list[dict]:
        return [{"provider": a.provider, "label": a.label, "capabilities": a.capabilities, "secret_fields": list(a.secret_fields),
                 "setting_fields": list(a.setting_fields), "verified": a.verified,
                 "status_note": "" if a.verified else a.UNVERIFIED_NOTE} for a in ADAPTERS.values()]

    @app.get("/api/integrations")
    def list_integrations() -> dict:
        rows = store.integrations()
        return {"integrations": [public_view(r, base_url()) for r in rows], "stages": list(STAGES), "fields": list(DEFAULT_FIELDS),
                "base_url": base_url()}

    @app.post("/api/integrations", status_code=201)
    def create_integration(body: IntegrationBody, request: Request) -> dict:
        admin(request)
        if body.provider not in ADAPTERS:
            raise HTTPException(422, f"Choose a provider: {', '.join(ADAPTERS)}")
        _check_settings(body.provider, body.settings, lambda rid: store.get_role(rid) is not None)
        try:
            row = svc.save(None, body.provider, body.name, body.settings, body.secrets)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        store.audit("integration_created", detail=f"{row['id']}: {row['provider']}")
        return public_view(row, base_url())

    @app.put("/api/integrations/{integration_id}")
    def update_integration(integration_id: str, body: IntegrationBody, request: Request) -> dict:
        admin(request)
        row = row_or_404(integration_id)
        _check_settings(row["provider"], body.settings, lambda rid: store.get_role(rid) is not None)
        try:
            row = svc.save(row, row["provider"], body.name or row["data"].get("name", ""), body.settings, body.secrets)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        if body.enabled is not None:
            store.update_integration(integration_id, enabled=body.enabled)
            row = store.integration(integration_id)
        store.audit("integration_updated", detail=f"{integration_id}: {', '.join(sorted({*body.settings, *body.secrets})) or 'name/state'}")
        return public_view(row, base_url())

    @app.delete("/api/integrations/{integration_id}")
    def delete_integration(integration_id: str, request: Request) -> dict:
        admin(request)
        row_or_404(integration_id)
        store.delete_integration(integration_id)  # its log goes with it; applications keep their external ids
        store.audit("integration_deleted", detail=integration_id)
        return {"deleted": True}

    @app.post("/api/integrations/{integration_id}/test")
    async def test_integration(integration_id: str, request: Request) -> dict:
        admin(request)
        return await svc.test(row_or_404(integration_id))

    @app.get("/api/integrations/{integration_id}/events")
    def integration_log(integration_id: str) -> dict:
        row_or_404(integration_id)
        return {"events": store.integration_events(integration_id, 100)}

    @app.post("/api/integrations/{integration_id}/retry")
    async def retry_integration(integration_id: str, request: Request) -> dict:
        admin(request)
        row_or_404(integration_id)
        return {"sent": await svc.retry_failed(integration_id)}

    # ---------- candidates (every source) ----------

    @app.get("/api/candidates")
    def candidates(q: str = "", role: str = "", status: str = "", source: str = "", sync: str = "") -> dict:
        """Everyone who applied, as people: one row per email address with each of their applications."""
        titles = {r.id: r.setup.role_title for r in store.list_roles()}
        needle = q.strip().lower()[:100]
        people: dict[str, dict] = {}
        for a in store.all_applications():
            if a.demo or (role and a.role_id != role) or (status and a.status != status) or (source and a.source != source):
                continue
            if sync and (a.sync.status if a.sync else "") != sync:
                continue
            c = a.candidate
            if needle and needle not in " ".join((c.full_name, c.email, c.current_role, c.location, titles.get(a.role_id, ""))).lower():
                continue
            interview = store.get_interview(a.interview_id) if a.interview_id and a.status == "invited" else None
            key = c.email.strip().lower()
            person = people.setdefault(key, {"email": c.email, "full_name": c.full_name, "location": c.location, "current_role": c.current_role,
                                             "applications": []})
            person["applications"].append({
                "id": a.id, "role_id": a.role_id, "role_title": titles.get(a.role_id, "(role deleted)"), "status": a.status,
                "source": a.source, "created_at": a.created_at.isoformat(), "interview_id": a.interview_id,
                "score": a.screening.score if a.screening else None,
                # Invited, but the email with the link didn't go out (and hasn't since): the candidate doesn't know yet.
                "invitation_undelivered": bool(interview and interview.invitation and interview.invitation.undelivered),
                "external": a.external.model_dump() if a.external else None, "sync": a.sync.model_dump(mode="json") if a.sync else None})
        rows = sorted(people.values(), key=lambda p: max(x["created_at"] for x in p["applications"]), reverse=True)
        return {"candidates": rows[:500], "total": len(rows), "roles": [{"id": k, "role_title": v} for k, v in titles.items()]}

    @app.post("/api/applications/{application_id}/notes", status_code=201)
    def add_note(application_id: str, body: NoteBody, request: Request) -> dict:
        a = store.get_application(application_id)
        if a is None:
            raise HTTPException(404, "Application not found")
        if a.demo:
            raise HTTPException(409, "This is sample data")
        user = getattr(request.state, "user", None)
        note = RecruiterNote(at=utcnow(), by=(user.name if user else "recruiter")[:100], text=body.text.strip())
        saved = store.save_application(a.model_copy(update={"notes": [*a.notes, note][-200:]}))
        store.audit("application_note_added", detail=a.id)  # the note's text stays on the application
        return {"application": saved.model_dump(mode="json")}
