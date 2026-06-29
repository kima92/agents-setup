# TestRail Skill

A self-contained Python CLI for TestRail's REST API v2 — test-management CRUD
(projects, suites, sections, cases, runs, tests, results, plans, milestones,
users, metadata) plus a `report-playwright` workflow that pushes Playwright e2e
results into TestRail runs.

Modelled on the sibling `slack` and `bitbucket` skills: env-var auth, a
`curl`-with-`urllib`-fallback transport, JSON on stdout, errors on stderr, no
third-party dependencies.

## Setup

```bash
export TESTRAIL_URL="https://yourcompany.testrail.io"
export TESTRAIL_USER="you@company.com"
export TESTRAIL_API_KEY="…"   # My Settings → API Keys (or account password)

python3 .claude/skills/testrail/scripts/testrail_api.py test-auth
```

## Layout

```
testrail/
├── SKILL.md                     # when-to-use + full command guide
├── README.md                    # this file
├── scripts/
│   └── testrail_api.py          # the CLI (run with: python3 … <command>)
└── references/
    └── api_endpoints.md         # per-endpoint request/response detail
```

## Highlights

- **`report-playwright`** — parse a Playwright JSON report and bulk-record
  results, mapping specs to TestRail cases by `C1234` markers. Final-retry wins,
  flaky-but-passing is reported as passed (configurable), skipped omitted by
  default. Run with `--dry-run` to preview the mapping **without credentials**.
- **Full surface** — everything the original `pw-testrail` MCP exposed (projects,
  suites, cases, runs, results) plus sections, tests, plans, milestones, users,
  metadata, bulk operations, and delete/close lifecycle commands.
- **Robust transport** — automatic pagination (`_links.next` and legacy bare
  arrays), credentials kept out of argv, helpful 401/400 guidance.

See `SKILL.md` for the command tables and worked examples.
