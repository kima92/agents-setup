---
name: clarity
description: Read Microsoft Clarity web-analytics data — dashboard metrics, session recordings, and product documentation — through a self-contained Python CLI that ports Microsoft's official Clarity MCP server (no MCP runtime, no Node, stdlib only). Use whenever the user wants Clarity analytics (page views, users, sessions, top browsers/OS/countries/pages, scroll depth, Core Web Vitals, JS errors, smart events, traffic sources), wants to pull or filter Clarity session recordings (by device, browser, OS, country/city, URL, rage/dead clicks, duration, smart events, etc.), or asks a "how do I …" question about setting up or using Clarity (install code, masking, funnels, heatmaps, SDKs, consent). Trigger on any mention of Microsoft Clarity, "Clarity dashboard/analytics", "session recordings", "clarity.microsoft.com", or Clarity setup/troubleshooting — even when the API or MCP is not named. Not for Google Analytics, Hotjar, FullStory, or other analytics tools.
---

# Microsoft Clarity

A dependency-free Python CLI (`scripts/clarity.py`) that drives the same three
Microsoft-hosted endpoints the official
[Clarity MCP server](https://github.com/microsoft/clarity-mcp-server) calls:

| Command | Endpoint (POST) | What it does |
|---|---|---|
| `analytics "<query>"` | `/mcp/dashboard/query` | Analytics dashboard data from a **natural-language** query |
| `recordings [filters]` | `/mcp/recordings/sample` | Sample of **session recordings** matching structured filters |
| `docs "<query>"` | `/mcp/documentation/query` | Snippets from **Clarity documentation** |
| `whoami` | — | Show the resolved token and, if it's a JWT, its claims |

It authenticates with a Clarity **API token** and speaks only the Python
standard library (`urllib`, `json`) — no `pip install`, no Node, no MCP host.

## Auth (read this first)

Every call needs a Clarity API token. Resolution order:

1. `--token <JWT>`
2. `CLARITY_API_TOKEN` environment variable
3. `~/.clarity/token.json` → `{"api_token": "<JWT>"}`

**Getting the token:** in Clarity, open the project → **Settings → Data
Export → "Generate new API token"**. The token is a JWT **scoped to that one
project**, so there is no project-id argument — the token *is* the project. If
you have several projects, keep a token per project and pass the right one with
`--token`. Never ask the user to paste it into the shell where it lands in
history; prefer the env var or the token file. `whoami` confirms which token is
in use without printing it in full.

If a call returns HTTP 401/403, the token is missing, expired, or for a
different project — regenerate it the same way.

## Running

```bash
python3 scripts/clarity.py <command> [options]
```

When installed, the path is typically `.claude/skills/clarity/scripts/clarity.py`.

## Analytics — the natural-language dashboard

`/dashboard/query` is an LLM-backed endpoint: you hand it one focused,
single-purpose question **with an explicit time range** and it returns the
matching metric. This is deliberately not a structured-parameter API, so the
quality of the answer depends on the query being narrow.

```bash
python3 scripts/clarity.py analytics "Page views count for the last 7 days"
python3 scripts/clarity.py analytics "Top browsers in the last 3 days"
python3 scripts/clarity.py analytics "Average session duration for desktop users this week"
python3 scripts/clarity.py analytics "Top JavaScript errors for PC in the last 30 days"
```

Guidelines that make these queries land (mirrors Microsoft's own guidance):

- **One metric per query.** Ask for page views *or* session duration, not both.
  If the user wants several things, run several `analytics` calls.
- **Always include a time range.** "last 7 days", "this week", "in June 2026".
  If the user didn't give one, ask them before guessing.
- **Be specific** about device, page URL, country, or segment when relevant.
- Avoid "analyze everything" / "all metrics" — those return vague or empty data.

The response shape is defined by the server (not by this skill). Read the JSON
it returns and summarize it for the user; don't assume a fixed schema.

## Session recordings

`/recordings/sample` returns a sample of recordings matching a filter set. A
date window is **required**; if you pass neither `--start/--end` nor `--days`,
it defaults to the **last 2 days** (the MCP's own default).

```bash
# 10 newest sessions (default window = last 2 days)
python3 scripts/clarity.py recordings --count 10

# Longest mobile sessions from the US over the last week
python3 scripts/clarity.py recordings --device Mobile --country "United States" \
  --days 7 --sort SessionDuration_DESC --count 20

# Sessions that had rage clicks or JS errors in a fixed window
python3 scripts/clarity.py recordings --rage-clicks \
  --start 2026-06-01 --end 2026-06-30T23:59:59Z

# Sessions that visited the checkout page and fired the Purchase smart event
python3 scripts/clarity.py recordings --url-contains "/checkout" --smart-event Purchase
```

Convenience filters (build the common cases): `--device`, `--browser`, `--os`,
`--country`, `--city`, `--url-contains`, `--clicked-text`, `--smart-event`,
`--rage-clicks`, `--dead-clicks`, `--js-errors`, `--min-duration`,
`--max-duration` (minutes).

Sort options (`--sort`): `SessionStart_DESC` (default, newest first),
`SessionStart_ASC`, `SessionDuration_ASC/DESC`, `SessionClickCount_ASC/DESC`,
`PageCount_ASC/DESC`. `--count` max is 250 (default 100).

Add `--summary` for a compact per-session digest (timestamp, player link, page
URLs, event counts) instead of the raw nested JSON — the full response runs
~200 lines *per session*, which buries the answer when you are hunting for one.

### Recent sessions are not queryable yet (ingestion lag)

**A session visible in the Clarity web UI can still be missing from this API.**
Measured directly: a session that started 11:36 UTC and ran 5m20s was *not*
returned by `--url-contains <its sale id>` at 11:56, and *was* returned by the
identical query at 12:16 — while being visible in the web UI the whole time.
So budget **roughly 30–45 minutes** after a session ends before the API can see it.

The consequence: **an empty result for a recent session is not evidence it
doesn't exist.** Before concluding anything from a miss, check the session's age.
If it is under ~an hour old, wait and re-run the same query rather than
concluding absence or hunting for a filter bug. The web UI is the source of
truth for anything fresh.

### Unfiltered sampling is capped, and the cap is the recent edge

Separately, an **unfiltered** `/recordings/sample` returns the most recent
sessions and stops at `--count`, so on a busy project `--days 30 --count 250`
comes back covering minutes, not days. This does **not** apply to filtered
queries — a `--url-contains` filter searches the whole window server-side and
reaches sessions far older than that recent slice.

The CLI warns on stderr when a cap-sized result covers far less than the
requested window. Treat that warning as "this unfiltered sample is only the
recent edge", not as "filters can't reach back" — they can.

**Anything not covered by a flag** — entry/exit URLs, UTM source/medium/campaign,
channel, Core Web Vitals ranges, scroll depth, product/e-commerce filters,
user type, session intent — goes through the raw escape hatch:

```bash
python3 scripts/clarity.py recordings --days 7 --filters-json \
  '{"channel":["PaidSearch"],"scrollDepth":{"min":75,"max":100},"userType":"NewUser"}'
# or from a file:
python3 scripts/clarity.py recordings --filters-file /tmp/filters.json
```

The full `FiltersType` schema (every field, operator, and allowed value) is in
`README.md`. Convenience flags and raw filters merge, so you can combine them.

## Documentation

`/documentation/query` answers "how do I use Clarity" questions from the official
docs (setup, masking, funnels, heatmaps, SDKs, consent, troubleshooting, API
reference, …). One topic per query.

```bash
python3 scripts/clarity.py docs "How do I set up masking for sensitive fields?"
python3 scripts/clarity.py docs "Install Clarity on a Shopify store"
python3 scripts/clarity.py docs "What is a smart event and how do I create one?"
```

## Gotchas

- **Undocumented endpoints.** These `clarity.microsoft.com/mcp/*` endpoints back
  Microsoft's own MCP server but aren't a published REST contract; Microsoft can
  change request/response shapes. If a call starts failing unexpectedly, check
  the upstream repo for changes and re-verify against `README.md`.
- **Analytics is NL, recordings is structured.** Don't try to pass filters to
  `analytics` (put the constraints in the sentence) or free text to `recordings`
  (use flags / `--filters-json`).
- **Token = project.** No project id anywhere; switching projects means switching
  tokens.
- **Response schemas are server-defined.** Parse and summarize what comes back;
  don't hard-code field names in downstream logic.

## Files

- `scripts/clarity.py` — the CLI + importable `ClarityClient`-style functions
  (import `post`, `build_filters`, etc. for custom scripting).
- `README.md` — full reverse-engineered reference: endpoints, headers, the
  complete `FiltersType` schema, sort enum, and setup notes.
