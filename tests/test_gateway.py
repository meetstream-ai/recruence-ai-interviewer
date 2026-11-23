"""The interview access gateway (app/gateway.py): hashed links, a code to the application's email, one verified
device, the time window, flags for the hiring team, and the company's name instead of the product's."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import re
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import gateway
from app.models import utcnow
from app.store import invitation_links, invite_tokens
from tests.test_api import TUNNEL, wait_for
from tests.test_recruiting import apply, client, published_role

WEB = Path(__file__).resolve().parent.parent / "app" / "web"


def invited(settings, account=False, **hiring):
    """An applicant invited automatically: (app, client, store, sent emails, interview, link token). With account, the
    recruiter has an account (and so a workspace, whose settings apply to the link)."""
    sent = []
    app, c = client(replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com"), mail=lambda s, e: sent.append(e))
    c.__enter__()
    if account:
        c.post("/api/auth/register", json={"name": "Olive Owner", "email": "olive@example.com", "password": "owner-password-1",
                                           "workspace_name": "Lumen Labs"})
    rid, _, key = published_role(c, **hiring)
    apply(c, key)
    row = wait_for(lambda: (p := c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]) and p[0]["interview_id"] and p[0])
    store = app.state.store
    interview = store.get_interview(row["interview_id"])
    token = c.get(f"/api/interviews/{interview.id}/invitation").json()["url"].rsplit("/", 1)[1]
    return app, c, store, sent, interview, token


def code_from(sent):
    return re.search(r"(?<![0-9])([0-9]{6})(?![0-9])", sent[-1].body).group(1)


def verified_client(app, token, sent):
    d = TestClient(app)
    d.post(f"/api/public/interview/{token}/code", headers=TUNNEL)
    assert d.post(f"/api/public/interview/{token}/verify", json={"code": code_from(sent)}, headers=TUNNEL).status_code == 200
    return d


def test_links_are_stored_only_as_a_hash_and_an_encrypted_copy(settings):
    app, c, store, sent, interview, token = invited(settings)
    try:
        assert len(token) >= 32 and interview.invitation.token == "" and interview.invitation.token_enc
        with store._engine.connect() as conn:
            assert conn.execute(sa.select(invitation_links.c.token_hash)).scalar() == gateway.token_hash(token)
            assert conn.execute(sa.select(sa.func.count()).select_from(invite_tokens)).scalar() == 0
            dump = " ".join(str(r) for t in ("interviews", "invitation_links", "audit_events") for r in conn.execute(sa.text(f"SELECT * FROM {t}")))
        assert token not in dump  # nowhere in clear
        assert f"/i/{token}" in sent[0].body and c.get(f"/i/{token}").status_code == 200 and c.get(f"/interview/{token}").status_code == 200
        detail = c.get(f"/api/interviews/{interview.id}").json()["interview"]["invitation"]
        assert token not in str(detail)
    finally:
        c.__exit__(None, None, None)


def test_older_plain_links_are_moved_and_keep_working(settings):
    app, c, store, sent, interview, token = invited(settings)
    c.__exit__(None, None, None)
    # As an older version left it: the link in clear, in the interview and in invite_tokens.
    old = "legacy-link-0123456789abcdefghijk"
    store.save_interview(interview.model_copy(update={"invitation": interview.invitation.model_copy(update={"token": old, "token_enc": ""})}))
    with store._engine.begin() as conn:
        conn.execute(invitation_links.delete())
        conn.execute(invite_tokens.insert().values(token=old, interview_id=interview.id))
    assert store.interview_for_invite(old).id == interview.id  # still found before it moves
    app2, c2 = client(replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com"))
    with c2:
        moved = app2.state.store.get_interview(interview.id)
        assert moved.invitation.token == "" and moved.invitation.token_enc
        assert c2.get(f"/api/public/interview/{old}", headers=TUNNEL).json()["state"] == "open"  # the candidate's link is unchanged
        assert c2.get(f"/api/interviews/{interview.id}/invitation").json()["url"].endswith(f"/i/{old}")
        with app2.state.store._engine.connect() as conn:
            assert conn.execute(sa.select(sa.func.count()).select_from(invite_tokens)).scalar() == 0


def test_codes_expire_are_used_once_and_wrong_ones_are_flagged(settings):
    app, c, store, sent, interview, token = invited(settings)
    try:
        d = TestClient(app)
        url = f"/api/public/interview/{token}"
        assert d.post(f"{url}/verify", json={"code": "123456"}, headers=TUNNEL).status_code == 422  # none sent yet
        assert store.get_interview(interview.id).flags == []  # nothing was sent, so that's no wrong code to flag
        d.post(f"{url}/code", headers=TUNNEL)
        first = code_from(sent)
        d.post(f"{url}/code", headers=TUNNEL)  # a new code replaces the earlier one
        if code_from(sent) != first:
            assert d.post(f"{url}/verify", json={"code": first}, headers=TUNNEL).status_code == 422
        d2 = TestClient(app)
        d2.post(f"{url}/code", headers=TUNNEL)
        assert d2.post(f"{url}/code", headers=TUNNEL).status_code == 429  # at most three sends in fifteen minutes
        latest = code_from(sent)
        wrong = "000000" if latest != "000000" else "111111"
        for _ in range(gateway.FLAG_AFTER_FAILURES - 1):
            assert d2.post(f"{url}/verify", json={"code": wrong}, headers=TUNNEL).status_code == 422
        assert "code_failures" not in [f.kind for f in store.get_interview(interview.id).flags]  # a typo or two isn't flagged
        assert d2.post(f"{url}/verify", json={"code": wrong}, headers=TUNNEL).status_code == 422
        assert "code_failures" in [f.kind for f in store.get_interview(interview.id).flags]
        for _ in range(gateway.MAX_TRIES):
            d2.post(f"{url}/verify", json={"code": wrong}, headers=TUNNEL)
        assert d2.post(f"{url}/verify", json={"code": latest}, headers=TUNNEL).status_code == 422  # used up: even the right one
        # Expiry, and one use only.
        later = gateway.send_code(store, "x" * 32, now=utcnow() - timedelta(minutes=gateway.CODE_MINUTES + 1))
        assert gateway.check_code(store, "x" * 32, later) is False
        fresh = gateway.send_code(store, "y" * 32)
        assert gateway.check_code(store, "y" * 32, fresh) is True and gateway.check_code(store, "y" * 32, fresh) is False
    finally:
        c.__exit__(None, None, None)


def test_one_device_at_a_time_and_the_same_device_reconnects(settings):
    app, c, store, sent, interview, token = invited(settings)
    try:
        url = f"/api/public/interview/{token}"
        mine = verified_client(app, token, sent)
        # A second device with a fresh code is refused while the first is active, and flagged.
        friend = TestClient(app)
        friend.post(f"{url}/code", headers=TUNNEL)
        assert friend.post(f"{url}/verify", json={"code": code_from(sent)}, headers=TUNNEL).status_code == 409
        assert friend.post(f"{url}/start", json={"acknowledged": True}, headers=TUNNEL).status_code == 409
        assert [f.kind for f in store.get_interview(interview.id).flags] == ["second_device"]
        started = mine.post(f"{url}/start", json={"acknowledged": True}, headers=TUNNEL).json()
        assert started["state"] == "in_progress" and started["meeting_link"]
        again = mine.get(url, headers=TUNNEL).json()  # a reload or a new tab on the same device
        assert again["meeting_link"] == started["meeting_link"] and again["verification"]["verified"] is True
        assert friend.get(url, headers=TUNNEL).json()["meeting_link"] is None
        # The hiring team reviews the flag: kept on record, marked reviewed.
        flags = c.post(f"/api/interviews/{interview.id}/flags/resolve").json()["flags"]
        assert flags[0]["resolved_at"] and flags[0]["kind"] == "second_device"
        audit = [e["action"] for e in store.audit_log(100, interview.id)]
        assert {"gateway_code_sent", "gateway_verified", "gateway_second_device_blocked", "interview_flagged", "interview_started_by_candidate"} <= set(audit)
    finally:
        c.__exit__(None, None, None)


def test_the_time_window_and_rescheduling(settings):
    app, c, store, sent, interview, token = invited(settings)
    try:
        start = utcnow() + timedelta(hours=2)
        r = c.post(f"/api/interviews/{interview.id}/invitation/resend",
                   json={"window_start": start.isoformat(), "window_end": (start + timedelta(hours=1)).isoformat()}).json()
        new = r["url"].rsplit("/", 1)[1]
        assert c.get(f"/api/public/interview/{token}", headers=TUNNEL).status_code == 404  # the old link is gone (revoked)
        view = c.get(f"/api/public/interview/{new}", headers=TUNNEL).json()
        assert view["state"] == "not_yet" and view["opens_at"].startswith(start.isoformat()[:16])
        d = verified_client(app, new, sent)
        assert d.post(f"/api/public/interview/{new}/start", json={"acknowledged": True}, headers=TUNNEL).status_code == 409
        assert c.post(f"/api/interviews/{interview.id}/invitation/resend",
                      json={"window_start": start.isoformat(), "window_end": start.isoformat()}).status_code == 422
        # Cancelling closes the link.
        c.post(f"/api/interviews/{interview.id}/cancel", json={})
        assert c.get(f"/api/public/interview/{new}", headers=TUNNEL).json()["state"] == "closed"
    finally:
        c.__exit__(None, None, None)


def test_verification_can_be_switched_off_but_the_device_is_still_bound(settings):
    app, c, store, sent, interview, token = invited(settings, account=True)
    try:
        assert TestClient(app).get(f"/api/public/interview/{token}", headers=TUNNEL).json()["verification"]["required"] is True  # the default
        r = c.put("/api/workspaces/current", json={"gateway_verification": "none", "company_name": "Lumen Labs", "support_contact": "jobs@lumen.example"})
        assert r.status_code == 200 and r.json()["gateway_verification"] == "none"
        view = TestClient(app).get(f"/api/public/interview/{token}", headers=TUNNEL).json()
        assert view["verification"]["required"] is False and view["company"] == "Lumen Labs" and view["contact"] == "jobs@lumen.example"
        first = TestClient(app)
        assert first.post(f"/api/public/interview/{token}/start", json={"acknowledged": True}, headers=TUNNEL).json()["state"] == "in_progress"
        assert TestClient(app).post(f"/api/public/interview/{token}/start", json={"acknowledged": True}, headers=TUNNEL).status_code == 409
    finally:
        c.__exit__(None, None, None)


def test_candidates_see_the_company_not_the_product(settings):
    app, c, store, sent, interview, token = invited(settings)
    try:
        for f in ("interview.html", "interview.js", "book.html", "book.js"):
            text = (WEB / f).read_text(encoding="utf-8")
            assert "Recruence" not in text and "MeetStream" not in text, f
        for email in sent:
            assert "Recruence" not in email.subject + email.body and "MeetStream" not in email.subject + email.body
        view = c.get(f"/api/public/interview/{token}", headers=TUNNEL).json()
        assert "Recruence" not in str({k: v for k, v in view.items() if k != "bot_name"})
        assert gateway.masked("priya@example.com") == "p••••@example.com" and gateway.masked("") == ""
    finally:
        c.__exit__(None, None, None)


def test_company_name_from_the_workspace_signs_the_emails(settings):
    from app.mailer import Email  # noqa: F401
    from app.recruiting import invitation_email
    app, c, store, sent, interview, token = invited(settings)
    try:
        email = invitation_email(interview, app.state.manager.settings, token, "Lumen Labs")
        assert email.subject.endswith("at Lumen Labs") and "The Lumen Labs hiring team" in email.body and f"/i/{token}" in email.body
    finally:
        c.__exit__(None, None, None)


def test_the_emailed_link_opens_on_the_public_address(settings):
    """The address in the invitation, through a tunnel, with no sign-in: the page and everything it loads."""
    import re
    app, c, _, _, _, token = invited(settings)
    try:
        d = TestClient(app)
        page = d.get(f"/i/{token}", headers=TUNNEL)
        assert page.status_code == 200 and "<html" in page.text.lower()
        for asset in re.findall(r'(?:src|href)="(/[^"#]+)"', page.text):
            assert d.get(asset, headers=TUNNEL).status_code == 200, asset
        assert d.get(f"/api/public/interview/{token}", headers=TUNNEL).status_code == 200
    finally:
        c.__exit__(None, None, None)
