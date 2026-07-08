# arial-test-executor

Agent skill that triggers ArialTest e2e executions through the Chikabase REST
API — the programmatic twin of the frontend "New Execution" form.

- **Runs inside Chika Brain sessions with zero setup**: auth and workspace come
  from the injected `CHIKABASE_BASE_URL` / `CHIKABASE_USER_ACCESS_KEY` /
  `CHIKABASE_WORKSPACE_ID` env vars.
- **Env defaults for free**: uses the server's `env-template` endpoint (the
  user's last-used values merged over `e2e/.env.example`), overridable per run
  with `--merge-env KEY=VALUE`.
- **Pinned artifacts attached automatically**: resolves the Brain activity from
  the worktree branch and sends its pinned artifact ids.

See [SKILL.md](SKILL.md) for the full command reference. The CLI is a single
stdlib-only Python script at `scripts/arial_test_executor.py`.

```bash
python3 scripts/arial_test_executor.py run --branch payment-flow \
  --merge-env APP_URL=https://test5.payme.io --wait
```
