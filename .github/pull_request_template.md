## What and why

## How it was tested
- [ ] `pytest -q` passes (and with `TEST_DATABASE_URL` on PostgreSQL if the schema changed)
- [ ] `node --check` passes for changed scripts
- [ ] Checked in the browser (which pages)
- [ ] Verified live (meeting platform, provider) / not verified live

## Checklist
- [ ] No API keys, tokens, real candidate data or local paths in the change
- [ ] Docs updated (README, `.env.example`, `docs/DEPLOYMENT.md`) where behaviour or settings changed
- [ ] New public routes are token-protected and listed in `app/security.py` and the tunnel rules
