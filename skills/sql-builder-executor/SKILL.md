---
name: sql-builder-executor
description: Build SQL from natural language (or run SQL you already have) against a Chikabase workspace database and poll for the rows — the programmatic twin of the Chikabase SQL-builder / "Answer" screen. Use whenever the user (or a Brain preset) wants to query the workspace database from an agent session: "how many active users signed up last month", "get me the SQL for total refunds per merchant", "run this SELECT against the workspace db", "what does this query return", "generate SQL that joins payments and merchants and export it to CSV", "execute attempt 42 and show the results". It handles the whole async chain — turn the question into SQL via the AI agent (or accept ready-made SQL directly), execute it against the workspace's primary database, poll the execution to completion, and print the result rows (with optional CSV export). Works out of the box inside Chika Brain agent sessions via the injected CHIKABASE_* env vars; outside them, a one-time email/password `login` mints a token. Trigger on any request to query, count, aggregate, join, or pull data from the workspace database, to convert a question into SQL, or to run/execute/analyze a SQL statement and see its output — even when the user doesn't say the words "SQL builder".
---

# SQL Builder + Executor

This skill drives the Chikabase **Answer** API (the SQL-builder screen) from a
single Python CLI (`scripts/sql_executor.py`). It reproduces what the frontend
does end-to-end:

1. **Build the SQL** — either turn a natural-language question into SQL with the
   AI agent (`POST /api/answer/generate`, async) or register SQL you already
   have (`POST /api/answer/direct`, instant).
2. **Execute it** against the workspace's database
   (`POST /api/answer/history/:attemptId/execute`, async).
3. **Poll** the execution to completion and fetch the rows
   (`GET /api/answer/execution/:id/status` → `.../result`).

The two async hops are the reason this is a CLI and not a couple of curls: SQL
generation and query execution each return an id you have to poll, and the
result endpoint answers `202` until the query finishes. The CLI does that
waiting for you and hands back JSON.

## Prerequisites

Everything comes from the environment — the skill takes no credentials on the
command line. Auth mirrors the sibling `arial-test-executor` skill: the same JWT
bearer token, all injected automatically into Chika Brain agent sessions (no
setup needed there):

- `CHIKABASE_BASE_URL` — API base, e.g. `http://localhost:3000/api` or
  `https://chikabase.payme.io/api` (defaulted to the localhost value when unset)
- `CHIKABASE_USER_ACCESS_KEY` — bearer token; every request is JWT-authenticated
  with it. Injected in Brain sessions.
- `CHIKABASE_WORKSPACE_ID` — default workspace (override with `--workspace-id`)

Outside a Brain session (no injected token), set credentials instead and the CLI
logs in for you, caches the minted token (mode `600`, at `~/.chikabase/token` or
`CHIKABASE_TOKEN_FILE`), and silently re-logs-in when it expires:

- `CHIKABASE_USER_EMAIL` / `CHIKABASE_USER_PASS` — used for auto-login

Verify whichever path applies with `test-auth` (it also lists the workspace's
databases). `login` forces a fresh token if you want to pre-warm the cache.

No third-party Python libraries. Transport is `curl` (token passed via a temp
config file so it never appears in argv) with a stdlib `urllib` fallback.

### Deployment layout (usually automatic)

The answer routes are reachable two ways: directly against the backend (Brain
sessions, local dev) or through the public `/api` ingress
(`chikabase.payme.io/api`), which rewrites paths. The CLI defaults to the direct
convention and, on a mismatch, flips to the ingress convention on the first call
and pins it — so both "just work". Force it with `CHIKABASE_API_LAYOUT=direct`
or `CHIKABASE_API_LAYOUT=nginx` if you ever need to skip the auto-detection.

## The headline command — `run`

`run` does the whole chain and prints the rows. Give it exactly one of `--query`
(natural language), `--sql` (ready-made), or `--attempt-id` (an existing
attempt).

```bash
S=scripts/sql_executor.py

# Natural language → SQL → execute → rows
python3 $S run --query "how many merchants onboarded in the last 30 days" --workspace-id 3

# Run SQL you already have (skips the AI agent entirely)
python3 $S run --sql "SELECT status, count(*) FROM payments GROUP BY status"

# Re-run the SQL from an attempt you generated earlier
python3 $S run --attempt-id 128

# Validate first, cap the inline rows, and dump the full set to CSV
python3 $S run --query "top 20 merchants by refund volume this quarter" \
  --analyze --max-rows 20 --csv /tmp/refunds.csv

# Build the SQL but don't run it (review before executing)
python3 $S run --query "monthly active users trend" --no-execute
```

What `run` does, in order:

1. **Get an executable attempt.** `--sql` → `POST /api/answer/direct` (instant,
   already holds the SQL). `--query` → `POST /api/answer/generate` then poll the
   attempt until `succeeded`/`failed`. `--attempt-id` → skip straight to
   execution. If a `--query` actually looks like SQL (or the server rejects it as
   "ready-made SQL"), the CLI automatically re-routes it to `/direct` so the run
   still succeeds instead of erroring.
2. **(optional) `--analyze`** — `POST .../analyze` runs EXPLAIN/validation and
   the report is included in the output.
3. **Execute** — `POST .../execute` with `databaseSourceId` (defaults to the
   workspace's primary database) returns an `executionId`.
4. **Poll** `GET .../execution/<id>/status` until it reaches
   `succeeded`/`failed`/`timeout`/`cancelled`.
5. **Fetch rows** on success and print `rowCount`, field names, timing, and up to
   `--max-rows` rows (default 50). `--csv PATH` writes the complete result set.

Status updates print to stderr while polling; the final JSON is on stdout.

## Picking the database

A workspace can have more than one database source. `run`/`execute` default to
the workspace **primary** database; target a specific one with
`--database-source-id`. List what's available first:

```bash
python3 $S databases --workspace-id 3
```

## Step-by-step commands

Use these when you want control over each hop instead of the one-shot `run`
(e.g. generate now, review the SQL, execute later).

| Command | Description |
|---------|-------------|
| `run` | End-to-end build → execute → poll → rows (flags above) |
| `generate --query "…" [--wait] [--agent langraph\|claude] [--mermaid]` | Create an NL→SQL attempt; `--wait` polls until the SQL is ready |
| `direct --sql "…" [--title …]` | Register ready-made SQL as an executable attempt |
| `attempt --id N [--wait] [--summary]` | Fetch an attempt (status, `generatedSql`, entities, response) |
| `analyze --attempt-id N [--database-source-id N]` | EXPLAIN/validate the attempt's SQL (synchronous) |
| `execute --attempt-id N [--wait] [--csv PATH] [--database-source-id N]` | Run the attempt's SQL; `--wait` polls and prints the rows |
| `execution --id N [--wait] [--result] [--csv PATH]` | Poll an execution; `--result` fetches rows once `succeeded` |
| `databases [--workspace-id N]` | List the workspace's database sources |
| `history [--status …] [--q text] [--limit N]` | Recent SQL attempts in the workspace |
| `login` | Force a fresh login from `CHIKABASE_USER_EMAIL`/`PASS` and cache the token |
| `test-auth [--workspace-id N]` | Verify credentials and list the workspace databases |

A typical manual flow:

```bash
python3 $S generate --query "refunds per merchant last quarter" --wait   # -> attemptId, generatedSql
python3 $S analyze --attempt-id 128                                       # optional EXPLAIN
python3 $S execute --attempt-id 128 --wait --csv /tmp/out.csv            # -> rows + CSV
```

## Agent choice for NL→SQL

`--agent langraph` (default, server-side `gpt-5`) or `--agent claude`
(`claude-sonnet-4`). Both produce a `generatedSql` on the attempt; pick `claude`
if the user asks for it or the langraph result was weak. `--mermaid` additionally
asks for an entity diagram, surfaced on the attempt as `mermaidDiagram`.

## Statuses to expect

- **Attempt** (generation): `queued` → `running` → `succeeded` | `failed`.
  Ready-made `direct` attempts are born `succeeded`.
- **Execution**: `pending` → `analyzing` → `executing` →
  `succeeded` | `failed` | `timeout` | `cancelled`.

`run`/`--wait` treat the terminal states as done and stop polling. On `failed`,
the CLI surfaces the attempt or execution `error` string.

## Error handling

JSON on stdout, clear messages on stderr with a non-zero exit:

- **Configuration Error: no access token** — not in a Brain session and no token
  cached/exported. Run `login`, or export `CHIKABASE_USER_ACCESS_KEY`.
- **Chikabase API error 401** — token expired/invalid. In Brain, re-run inside a
  live session; otherwise `login` again.
- **Chikabase API error 400 "ready-made SQL"** — only surfaces from bare
  `generate`; `run` auto-falls back to `/direct`. Use `direct`/`run --sql` for
  SQL you already have.
- **No database configured for workspace** — the workspace has no database
  source; check `databases` and pass the right `--workspace-id`.
- **Timed out waiting for attempt/execution** — generation or the query ran past
  the timeout; re-check with `attempt --id N` / `execution --id N`, or raise
  `--attempt-timeout` / `--execution-timeout`.
