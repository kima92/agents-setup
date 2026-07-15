---
name: arial-test-executor
description: Trigger ArialTest e2e test executions through the Chikabase REST API — the programmatic equivalent of the "New Execution" form. Use whenever the user (or a Brain preset) wants to run, launch, queue, or check ArialTest / arial-test / e2e / Playwright test executions from an agent session — "run the arial tests", "start an e2e execution against test5", "run tests with APP_URL=...", "kick off a test run for this branch", "what's the status of execution 42". It auto-fills everything the form does: env vars come from the server template (the user's last-used values merged over e2e/.env.example, overridable per-run with --merge-env KEY=VALUE), and the Brain conversation's pinned artifacts are auto-detected and attached. Works out of the box inside Chika Brain agent sessions via the injected CHIKABASE_* env vars — no extra credentials needed. Trigger on any mention of ArialTest, arial-test, "new execution", running e2e tests against an environment/URL, or checking/waiting on a test execution. Pairs with the `testrail` skill for reporting results.
---

# ArialTest Executor

This skill creates and starts ArialTest executions via the Chikabase API from
a single Python CLI (`scripts/arial_test_executor.py`). It reproduces what the
frontend "New Execution" modal does — create the execution, then start it —
plus the two conveniences the modal gets for free and an agent otherwise
doesn't:

1. **Env defaults**: the server's `env-template` endpoint returns
   `e2e/.env.example` overlaid with the values this user most recently ran
   with in this workspace. The CLI fetches that and applies your
   `--merge-env KEY=VALUE` overrides on top, so a run only needs to state
   what *changes* (e.g. `--merge-env APP_URL=https://test5.payme.io`).
2. **Pinned artifacts**: the Brain conversation's pinned artifacts (test
   plans, user stories, requirements) are auto-detected and sent as the
   execution's `artifactIds`, exactly like the form's artifact picker.

## Prerequisites

Three environment variables, all injected automatically into Chika Brain
agent sessions (no setup needed there):

- `CHIKABASE_BASE_URL` — the server root every route is concatenated onto,
  e.g. `https://chikabase.payme.io/api` for the hosted proxy or
  `http://localhost:3000` for a direct local backend (defaults to the latter)
- `CHIKABASE_USER_ACCESS_KEY` — bearer token minted per run for the session's
  user; all requests are JWT-authenticated with it
- `CHIKABASE_WORKSPACE_ID` — default workspace (override with
  `--workspace-id`)

No third-party Python libraries. Transport is `curl` (token passed via a temp
config file so it never appears in argv) with a stdlib `urllib` fallback.
Verify credentials quickly with:

```bash
python3 scripts/arial_test_executor.py test-auth
```

## The headline command — `run`

```bash
S=scripts/arial_test_executor.py

# Minimal: branch suffix is the only required input.
# Env = server template (last-used values), artifacts = pinned in this session.
# Prefer passing the Brain Activity ID from your system prompt (see below).
python3 $S run --branch payment-flow --activity-id 123

# Point the run at a different environment and app branch:
python3 $S run --branch payment-flow \
  --merge-env APP_URL=https://test5.payme.io \
  --tested-branch feature/payment-flow \
  --prompt "Focus on the withdrawal edge cases"

# Block until it finishes and report pass/fail:
python3 $S run --branch payment-flow --wait

# See exactly what would be sent, without creating anything:
python3 $S run --branch payment-flow --merge-env APP_URL=https://test5.payme.io --dry-run
```

What it does, in order:

1. Fetches `GET /arial-test/env-template?workspaceId=…` (skip with
   `--no-env-template`) and folds in every `--merge-env KEY=VALUE`
   (repeatable; overrides win).
2. Resolves the Brain activity and its pinned artifact IDs (see below).
3. `POST /arial-test/executions` with `branchName: arial-test/<suffix>`
   (the prefix is added for you; passing an already-prefixed name is fine),
   `artifactIds`, `brainActivityId`, optional `userPrompt`/`testedBranch`,
   and the env map under `metadata.envVariables`.
4. `POST /arial-test/executions/<id>/start` to trigger the agent.
5. Prints a JSON summary (execution id, status, env keys). With `--wait` it
   polls until the status is `passed`/`failed`/`cancelled` (default timeout
   3600s, `--timeout`/`--poll-interval` to tune) and prints the outcome.

## How pinned-artifact auto-detection works

**Check your system prompt first**: Brain sessions state the current **Brain
Activity ID** in their context (next to the workspace info). If you see it,
pass it explicitly — `run --activity-id <id>` — that's exact and costs no
extra API calls.

Without the flag, the CLI falls back to walking the worktree chain: Brain
agent worktrees run on a branch named `activity-<id>-<timestamp>`, where
`<id>` is the *processor* activity whose metadata points at the owning Brain
activity — so current git branch → `GET /activities/<id>` →
`metadata.brainActivityId` → `GET /brain/activities/<id>/pinned-artifacts`.

When any link is missing (not in a Brain worktree, detached HEAD), `run`
stops and tells you the three escape hatches:

- `--activity-id <brainActivityId>` — use a known Brain activity directly
- `--artifact-ids 1,2,3` — name the artifacts explicitly
- `--no-artifacts` — send none

Inspect before running:

```bash
python3 $S pinned-artifacts            # ids only, auto-detected
python3 $S pinned-artifacts --resolve  # + title/type/filePath per artifact
```

## Env vars — inspecting and overriding

```bash
# What would a run use right now? (sensitive values masked)
python3 $S env-template

# Preview with overrides applied; --show-secrets reveals masked values
python3 $S env-template --merge-env APP_URL=https://test5.payme.io --show-secrets
```

Values whose key matches password/secret/token/pat/key are masked as
`********` in `env-template` and `--dry-run` output so they don't land in the
transcript — the real values are always sent to the API regardless. Note the
server remembers each run's env as the next run's defaults, so a one-off
`--merge-env` becomes sticky for the user's future runs.

## Other commands

| Command | Description |
|---------|-------------|
| `run --branch <suffix>` | Create + start an execution (flags above) |
| `status --id N [--wait] [--summary]` | Get one execution; `--wait` polls to completion |
| `list [--status queued\|running\|passed\|failed\|cancelled]` | Executions in the workspace, newest first |
| `env-template [--merge-env K=V] [--show-secrets]` | Merged env defaults a run would use |
| `pinned-artifacts [--activity-id N] [--resolve]` | Pinned artifact ids for the Brain activity |
| `test-auth` | Verify the CHIKABASE_* credentials work |

## Error handling

JSON on stdout, clear messages on stderr with non-zero exit:

- **Configuration Error: Missing … CHIKABASE_USER_ACCESS_KEY** — not in a
  Brain session and no token exported. Outside Brain, export the trio
  manually.
- **Chikabase API error 401** — token expired (they're minted per run) or
  invalid. Nothing to refresh client-side; re-run inside a live session.
- **Chikabase API error 400** — usually an invalid git branch name in
  `--branch`/`--tested-branch`; the message names the offending field.
- **Chikabase API error 404 on env-template** — the workspace has no
  Bitbucket knowledge source; pass the right `--workspace-id`.
- **Could not resolve the Brain activity …** — see the escape hatches above.
