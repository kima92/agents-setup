---
name: testrail
description: Interact with TestRail (test-management) through its REST API v2 as a self-contained Python CLI. Use whenever the user wants to read or manage TestRail projects, suites, sections, test cases, runs, tests, results, plans, or milestones — listing/creating/updating cases, opening or closing runs, recording pass/fail results, or looking up case/run/project IDs. Critically, use this to REPORT PLAYWRIGHT (e2e) TEST RESULTS to TestRail: the `report-playwright` command ingests a Playwright JSON report and pushes results into a run, mapping specs to cases by their `C1234` markers. Trigger on any mention of TestRail, "test case management", "test run", "report results to TestRail", "sync e2e results", "create a TestRail run", or a TestRail case/run reference (e.g. C1234, R567) — even when the API isn't named explicitly. Pairs naturally with the `arial-test` Playwright e2e skill.
---

# TestRail

This skill drives TestRail's REST API v2 from a single Python CLI
(`scripts/testrail_api.py`). It covers the full test-management surface —
projects, suites, sections, cases, runs, tests, results, plans, milestones,
users, and metadata — plus a `report-playwright` workflow that closes the loop
between the `arial-test` Playwright e2e suite and TestRail.

It mirrors (and substantially extends) the `pw-testrail` MCP server as a plain
CLI, so nothing needs an MCP server running. It follows the same conventions as
the sibling `slack` and `bitbucket` skills: env-var auth, a `curl`-with-`urllib`
fallback transport, JSON on stdout, and errors on stderr.

## Prerequisites

These environment variables must be set:

- `TESTRAIL_URL` — instance base URL, e.g. `https://yourcompany.testrail.io`
- `TESTRAIL_USER` — TestRail account email
- `TESTRAIL_API_KEY` — an API key (TestRail → **My Settings → API Keys**) or the
  account password

Auth is HTTP Basic (`user:api_key`). If you get **401 "Authentication failed:
invalid or missing user/password"**, the key is wrong/expired or the API is
disabled — regenerate the key under *My Settings → API Keys* and confirm
*Administration → Site Settings → API → Enable API* is on. Verify quickly with:

```bash
python3 .claude/skills/testrail/scripts/testrail_api.py test-auth
```

No third-party Python libraries are needed. The script talks to the API with
`curl` by default (capturing the HTTP status via `-w "%{http_code}"`, credentials
passed through a temp config file so they never appear in argv) and falls back to
Python's stdlib `urllib` when `curl` is unavailable.

## The TestRail URL quirk (handled for you)

TestRail's API path is `<base>/index.php?/api/v2/<endpoint>`. Because the `?`
already sits before `/api/v2`, additional query parameters are appended with `&`
rather than `?`. The script builds these URLs correctly — you never assemble
them by hand. List endpoints also transparently follow TestRail's `_links.next`
pagination (and still work against older instances that return bare arrays).

## Status and priority IDs

TestRail uses numeric IDs. The script accepts friendly names where it can, but
these are the built-in mappings you'll see in output:

| Result status | ID | Priority | ID |
|---------------|----|----------|----|
| Passed        | 1  | Low      | 1  |
| Blocked       | 2  | Medium   | 2  |
| Untested      | 3* | High     | 3  |
| Retest        | 4  | Critical | 4  |
| Failed        | 5  |          |    |

\* *Untested* is a state, not something you can post via `add_result`. Custom
statuses get higher IDs — run `list-statuses` to see your instance's set.

## Available Commands

Execute with Python 3:

```bash
python3 .claude/skills/testrail/scripts/testrail_api.py <command> [options]
```

### Discovery & metadata

| Command | Description |
|---------|-------------|
| `test-auth` | Verify credentials by listing visible projects |
| `list-projects` / `get-project` | Projects (filter with `--active`/`--completed`) |
| `list-suites` / `get-suite` / `add-suite` | Test suites in a project |
| `list-sections` / `add-section` | Sections (folders) within a suite |
| `list-statuses` / `list-priorities` / `list-case-types` / `list-templates` | Instance metadata (IDs ↔ labels) |
| `list-users` / `get-user` / `get-user-by-email` | Users |

### Test cases

| Command | Description |
|---------|-------------|
| `list-cases` | List/search cases in a project (filter by suite, section, title) |
| `get-case` | Get one case by ID |
| `add-case` | Create a case in a section (title, steps, priority, refs) |
| `update-case` | Update a single case |
| `update-cases` | Bulk-update many cases in a suite at once |
| `delete-case` | Delete a case (**irreversible**) |

### Runs, tests & results

| Command | Description |
|---------|-------------|
| `list-runs` / `get-run` | Test runs in a project |
| `add-run` | Create a run (all cases, or a `--case-ids` subset) |
| `update-run` / `close-run` / `delete-run` | Modify, archive (**irreversible**), or remove a run |
| `list-tests` / `get-test` | The case instances inside a run |
| `add-result` | Record a result against a `test_id` |
| `add-result-for-case` | Record a result against a `case_id` in a run |
| `add-results` | Bulk-record results from a JSON file |
| `get-results` / `get-results-for-case` / `get-results-for-run` | Read historical results |

### Plans & milestones

| Command | Description |
|---------|-------------|
| `list-plans` / `get-plan` / `add-plan` / `add-plan-entry` / `close-plan` | Test plans (groups of runs) |
| `list-milestones` / `get-milestone` / `add-milestone` | Milestones |

### Workflows

| Command | Description |
|---------|-------------|
| `report-playwright` | Parse a Playwright JSON report and push results to a run (creating the run if needed) |

## Reporting Playwright e2e results — the headline workflow

This is the reason the skill lives next to `arial-test`. After a Playwright run
produces a JSON report, `report-playwright` maps each spec to its TestRail
case(s) and records the outcome in one bulk call.

### 1. Produce a JSON report from Playwright

Add (or enable) the JSON reporter so a machine-readable report lands on disk:

```bash
# one-off, without editing config
npx playwright test --reporter=json > /tmp/pw-report.json

# or in playwright.config.ts:
#   reporter: [["list"], ["json", { outputFile: "test-results/report.json" }]]
```

### 2. Mark specs with their TestRail case IDs

Put the case ID as `C<number>` in the test (or `describe`) title. The spec's own
title wins; a `describe`-level ID is used only when the spec itself has none — so
you don't accidentally stamp one describe's ID onto every child spec.

```ts
test("C1235 rejects amounts below the minimum", async () => { /* ... */ });
test("accepts amounts at the minimum [C1236]", async () => { /* ... */ });
```

### 3. Report

```bash
S=.claude/skills/testrail/scripts/testrail_api.py

# ALWAYS dry-run first — it needs no credentials and shows the exact mapping,
# which cases got which status, and which specs had no C-marker (unmapped).
python3 $S report-playwright --report /tmp/pw-report.json --project-id 5 --dry-run

# Create a fresh run scoped to exactly the mapped cases, then report into it:
python3 $S report-playwright --report /tmp/pw-report.json \
  --project-id 5 --run-name "INFRA-3991 e2e $(date +%F)" --suite-id 12

# Or report into an existing run and close it when done:
python3 $S report-playwright --report /tmp/pw-report.json --run-id 678 --close-run
```

How it decides the outcome (so the numbers are trustworthy):

- **Final retry wins.** A test that fails then passes on retry is *flaky but
  passing* in Playwright; it's reported as passed and tagged `(flaky)` in the
  comment — not as a failure. Use `--flaky-status retest` to surface flakes as
  *Retest* instead.
- **Failures carry context.** `timedOut`/`interrupted`/`failed` → *Failed*, with
  the (ANSI-stripped, de-duplicated) error message in the result comment.
- **Skipped specs are omitted by default.** Add `--include-skipped`
  (optionally `--skipped-status blocked`) to record them.
- **A spec with several tests** (projects / parametrization) takes the most
  severe final status across them.

The command prints the `run_id`, run URL, a summary (mapped results, distinct
cases, unmapped specs), and the list of unmapped spec titles so you can spot
specs that are missing a `C####` marker.

### Bulk results from any source

If you already have results in hand (not from Playwright), post them directly:

```bash
# results.json: [{"case_id":1235,"status_id":5,"comment":"...","elapsed":"4s"}, ...]
python3 $S add-results --run-id 678 --results-file results.json
echo '{"results":[{"case_id":1,"status_id":1}]}' | python3 $S add-results --run-id 678 --results-file -
```

## Common command examples

```bash
S=.claude/skills/testrail/scripts/testrail_api.py

# Find IDs you need
python3 $S list-projects
python3 $S list-suites --project-id 5
python3 $S list-sections --project-id 5 --suite-id 12
python3 $S list-cases --project-id 5 --suite-id 12 --filter "withdrawal" --limit 50

# Create a case with steps (JSON array of {content, expected} or plain strings)
python3 $S add-case --section-id 88 --title "Withdrawal below minimum is rejected" \
  --priority-id 3 --refs "INFRA-3991" \
  --steps '[{"content":"POST /withdrawals with amount < min","expected":"422 returned"}]'

# Open a run for a subset of cases, record a couple of results, then close it
python3 $S add-run --project-id 5 --name "Smoke" --suite-id 12 --case-ids 1235,1236
python3 $S add-result-for-case --run-id 678 --case-id 1235 --status failed --comment "422 expected, got 200" --elapsed "4s"
python3 $S add-result-for-case --run-id 678 --case-id 1236 --status passed --elapsed "3s"
python3 $S close-run --run-id 678

# Read everything that happened in a run
python3 $S get-results-for-run --run-id 678
```

`--status` takes a name (`passed`/`blocked`/`retest`/`failed`); `--status-id`
takes a raw number if you use custom statuses.

## Workflow guidelines

### Reporting an automated test run (the usual path)

1. Run Playwright with `--reporter=json`.
2. `report-playwright --dry-run` to confirm the spec→case mapping and catch any
   specs missing a `C####` marker.
3. Re-run without `--dry-run`, supplying `--run-id` (existing run) or
   `--project-id`/`--run-name` (create one). Add `--close-run` to finalize.

### Authoring/curating cases

1. `list-projects` → `list-suites` → `list-sections` to find where the case
   belongs (you need the **section** ID to create a case).
2. `add-case` with steps and `--refs` linking the Jira key.
3. `list-cases --filter` to find existing cases before creating duplicates.

### Finding IDs

You almost always start by resolving a numeric ID: project → suite → section →
case, or project → run → test. Use the `list-*` commands; they paginate
automatically, so `--limit` is about how much you want back, not API page size.

## Error handling

The script prints JSON on success and a clear message on stderr (non-zero exit)
on failure:

- **Configuration Error: Missing required environment variable** — set the
  `TESTRAIL_*` vars (see Prerequisites).
- **TestRail API error 401** — bad/expired key or API disabled; the message
  includes the fix. `report-playwright --dry-run` still works without creds.
- **TestRail API error 400 … Field :results is a required field / unknown** —
  usually a result for a `case_id` that isn't part of the run, or an empty
  results set. Add the cases to the run (`--case-ids` / `update-run`) first.
- **TestRail API error 429** — rate limited; pause and retry.

## Additional reference

For per-endpoint request/response detail, IDs, and TestRail's pagination and
custom-field model, see `references/api_endpoints.md`.
