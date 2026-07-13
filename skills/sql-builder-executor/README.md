# sql-builder-executor

Agent skill that drives the Chikabase **Answer / SQL-builder** API — the
programmatic twin of the frontend SQL-builder screen.

- **Builds SQL two ways**: natural language → SQL via the AI agent
  (`POST /api/answer/generate`), or run SQL you already have
  (`POST /api/answer/direct`).
- **Executes against the workspace database** and polls the async execution to
  completion (`POST /api/answer/history/:id/execute` →
  `GET /api/answer/execution/:id/status` → `.../result`).
- **Prints the rows** (with optional full-set CSV export).
- **Zero-setup inside Chika Brain sessions**: auth + workspace come from the
  injected `CHIKABASE_BASE_URL` / `CHIKABASE_USER_ACCESS_KEY` /
  `CHIKABASE_WORKSPACE_ID` env vars (same bearer auth as `arial-test-executor`).
  Outside them, set `CHIKABASE_USER_EMAIL` / `CHIKABASE_USER_PASS` and the CLI
  auto-logs-in and caches the token. All credentials come from the environment.
- **Layout-aware**: works both directly against the backend and through the
  public `/api` ingress (auto-detected, override with `CHIKABASE_API_LAYOUT`).

See [SKILL.md](SKILL.md) for the full command reference. The CLI is a single
stdlib-only Python script at `scripts/sql_executor.py`.

```bash
# Natural language, end to end
python3 scripts/sql_executor.py run \
  --query "how many merchants onboarded in the last 30 days" --workspace-id 3

# Ready-made SQL, exported to CSV
python3 scripts/sql_executor.py run \
  --sql "SELECT status, count(*) FROM payments GROUP BY status" --csv /tmp/out.csv
```
