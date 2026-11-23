"""Pre-flight check before a live AI interview test .

Runs against a running Recruence (the desktop app, a source copy or a cloud backend) and checks everything a live
Google Meet interview needs, without launching a bot or sending email:

  python scripts/live_preflight.py --url http://127.0.0.1:8765 --email you@example.com --role "AI Engineer"

The password is asked for at the prompt (or read from RECRUENCE_PASSWORD) and is never printed or stored. The only
costs are the AI connection test's tiny requests (a few tokens per model). Exit code 0 when every check passes.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from urllib.parse import urlsplit

import httpx


OK, FAIL, WARN = "PASS", "FAIL", "WARN"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to a code page without "→"
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8765", help="Recruence's address (the desktop app: http://127.0.0.1:8765)")
    ap.add_argument("--email", required=True, help="an installation admin's account")
    ap.add_argument("--role", required=True, help="the role the test interview is for (its exact title)")
    ap.add_argument("--forwarded", action="store_true", help="add proxy headers (a cloud backend reached directly, for testing)")
    args = ap.parse_args()
    target = urlsplit(args.url)
    if target.scheme != "https" and target.hostname not in ("127.0.0.1", "localhost", "::1"):
        print("Use the https:// address: the password would otherwise travel unencrypted.")
        return 2
    password =os.environ.get("RECRUENCE_PASSWORD") or getpass.getpass(f"Password for {args.email}: ")
    headers = {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https"} if args.forwarded else {}
    c = httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=60)
    results: list[tuple[str, str, str]] = []

    def check(name: str, status: str, detail: str) -> None:
        results.append((name, status, detail))

    # 1. The app answers, and live meetings are configured.
    try:
        health = c.get("/api/health").json()
    except httpx.HTTPError as e:
        print(f"Recruence didn't answer at {args.url}: {type(e).__name__}")
        return 2
    ms = health.get("meetstream", {})
    check("App and live meetings", OK if ms.get("ready") else FAIL, "ready" if ms.get("ready") else ms.get("reason", "not ready"))

    # 2. Sign in.
    r = c.post("/api/auth/account-login", json={"email": args.email, "password": password})
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", "")
        except ValueError:
            detail = "not a Recruence answer: is this the right address?"
        print(f"Sign-in failed ({r.status_code}): {detail}")
        return 2
    cookie = r.headers.get("set-cookie", "").split(";")[0]
    if cookie:
        c.headers["Cookie"] = cookie  # a Secure cookie isn't resent over http by the client: carry it by hand

    # 3. The interview's voice setup, as configured (separate parts or one realtime model: either is fine).
    cfg = c.get("/api/config")
    if cfg.status_code != 200:
        print("This account can't read the installation settings: use an installation admin.")
        return 2
    cfg = cfg.json()
    voice = cfg.get("voice") or {}
    fields = {f["key"]: f for s in cfg.get("sections", []) for f in s.get("fields", [])}
    check("Voice setup ready", OK if voice.get("ready") else FAIL, voice.get("label") or voice.get("problem") or "")
    check("Voice mode", OK, "separate parts" if voice.get("mode") == "pipeline" else "one realtime model")
    talk = (fields.get("mia_llm_model") or {}).get("value", "") or (fields.get("gemini_model") or {}).get("value", "")
    if voice.get("mode") == "pipeline":
        check("Conversation model", OK, talk or "the AI provider's model")
    if voice.get("meetstream_keys"):
        check("Keys in MeetStream → Integrations", WARN, "can't be read from here; confirm these exist: " + ", ".join(voice["meetstream_keys"]))

    # 4. AI quota: a tiny request to each model the interview and its evaluation use.
    provider = (health.get("llm", {}).get("name") or "").split(":")[0]
    if provider in ("gemini", "claude", "openai", "xai", "ollama"):
        model = (fields.get(f"{'claude' if provider == 'claude' else provider}_model") or {}).get("value", "")
        r = c.post("/api/config/test-ai", json={"provider": provider, "model": model}).json()
        check(f"AI quota ({provider})", OK if r.get("ok") else FAIL, r.get("message", ""))
    else:
        check("AI provider", FAIL, f"no AI provider in use ({health.get('llm', {}).get('name')})")

    # 5. MeetStream key and the Public URL (where MeetStream's webhooks and the candidate's link go).
    for name, key in (("MeetStream key", "meetstream"), ("Public URL reaches this app", "public_url")):
        r = c.post(f"/api/settings/check/{key}", json={}).json()
        check(name, OK if r.get("ok") else FAIL, r.get("message", ""))

    # 6. Email (invitation and the interview code).
    ready = c.get("/api/settings/readiness").json()
    email = next((i for i in ready.get("items", []) if i.get("key") == "email"), {})
    check("Email set up", OK if email.get("status") == "ok" else FAIL, email.get("detail", ""))

    # 7. The role: published, approved questions, an interview room, AI-led, questions shown in the chat.
    roles = c.get("/api/roles").json()
    found = None
    for item in roles:
        d = c.get(f"/api/roles/{item['id']}").json()
        role = d.get("role", d)
        if role["setup"]["role_title"].strip().lower() == args.role.strip().lower():
            found = role
            break
    if not found:
        check("Role", FAIL, f"no role called {args.role!r} in this workspace")
    else:
        h, o = found["hiring"], found["hiring"].get("options", {})
        check("Role published", OK if h.get("published") else FAIL, "taking applications" if h.get("published") else "a draft: publish it")
        check("Questions approved", OK if found.get("plan_approved_at") else FAIL, f"{len((found.get('plan') or {}).get('questions', []))} questions")
        room = h.get("meeting_link", "")
        check("Interview room", OK if "meet.google.com/" in room else (WARN if room else FAIL),
              "Google Meet room set" if "meet.google.com/" in room else (f"not Google Meet: {room.split('/')[2] if '//' in room else room}" if room else "no room"))
        check("AI-led", OK if o.get("mode", "ai") == "ai" else FAIL, o.get("mode", "ai"))
        check("Questions in the meeting chat", OK if o.get("questions_in_chat", True) else WARN,
              "on" if o.get("questions_in_chat", True) else "off: B-1 needs it on (role setup → Interview mode)")
        check("Format", OK, o.get("format", "voice"))

    width = max(len(n) for n, _, _ in results)
    for name, status, detail in results:
        print(f"{status:4}  {name:{width}}  {detail}")
    failed = [n for n, s, _ in results if s == FAIL]
    print("\nAll checks passed: ready for the live test." if not failed else f"\n{len(failed)} check(s) failed: fix them before the live test.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
