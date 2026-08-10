# Repository guidance

## Agent skills

### Issue tracker

Issues and specs are tracked in GitHub Issues for this repository. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default five-role triage vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context service. Read `CONTEXT.md` and relevant decisions under `docs/adr/` before changing production behavior. See `docs/agents/domain.md`.

## Safety

- Treat this repository as public: never commit credentials, access tokens, cookies, or authorization headers.
- Monthly advertising syncs must fail closed: an advertising API error is unknown, not zero.
- Manual production repairs require a narrow seller/month scope, a dry-run summary, a rollback snapshot, and read-back verification.
- Stage files explicitly; do not use `git add .` or `git add -A`.
