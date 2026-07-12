# clarity-cli

A tiny, **dependency-free** Python client for
[Microsoft Clarity](https://clarity.microsoft.com). It ports Microsoft's official
[Clarity MCP server](https://github.com/microsoft/clarity-mcp-server) to a plain
CLI: same three Microsoft-hosted endpoints, same auth, **no MCP runtime, no
Node, Python standard library only**.

- **Analytics** dashboard data from natural-language queries
- **Session recordings** sampling with the full Clarity filter set
- **Documentation** search over the official Clarity docs

> Read-only. Every endpoint is one of Clarity's own `readOnlyHint` MCP tools —
> nothing here writes or deletes.

## Install

```bash
npx skills add https://github.com/kima92/agents-setup --skill clarity
```

## Requirements

- Python 3.9+ (standard library only — no `pip install` needed)
- A Clarity **API token** (a JWT; see below)

## Setup (one time)

1. Sign in to [clarity.microsoft.com](https://clarity.microsoft.com) and open the
   project you want to query.
2. Go to **Settings → Data Export → Generate new API token** and copy the token.
   It is a JWT **scoped to that single project**.
3. Make it available to the CLI in any one of these ways (checked in this order):

   ```bash
   # (a) per-command flag
   python3 scripts/clarity.py --token "<JWT>" analytics "Page views last 7 days"

   # (b) environment variable
   export CLARITY_API_TOKEN="<JWT>"

   # (c) token file (chmod 600 recommended)
   mkdir -p ~/.clarity && printf '{"api_token":"%s"}\n' "<JWT>" > ~/.clarity/token.json
   ```

There is **no project-id setting** — the token identifies the project. For
multiple projects, keep one token per project and select with `--token`.

`python3 scripts/clarity.py whoami` shows which token is resolved and (since it's
a JWT) decodes its claims (project id, expiry) without printing the whole token.

## Commands

```bash
python3 scripts/clarity.py analytics "<natural-language query>" [--timezone IANA]
python3 scripts/clarity.py recordings [filters…] [--sort …] [--count N] [--days N | --start … --end …]
python3 scripts/clarity.py docs "<question>"
python3 scripts/clarity.py whoami
```

Global: `--token <JWT>` (before the subcommand). All commands print the server's
JSON response to stdout.

## How it maps to the MCP server

Every request is a `POST` to `https://clarity.microsoft.com/mcp/…` with headers
`Content-Type: application/json` and `Authorization: Bearer <token>`.

| CLI command | MCP tool | Endpoint | Request body |
|---|---|---|---|
| `analytics` | `query-analytics-dashboard` | `/mcp/dashboard/query` | `{"query", "timezone"}` |
| `recordings` | `list-session-recordings` | `/mcp/recordings/sample` | `{"sortBy", "start", "end", "filters", "count"}` |
| `docs` | `query-documentation-resources` | `/mcp/documentation/query` | `{"query"}` |

Notes on faithful behavior:

- **`sortBy` is sent as a number** (the `SortOptionsEnum` value), not the string.
- **`start`/`end`** are UTC ISO 8601 with milliseconds (`2024-01-31T23:59:59.000Z`),
  exactly like JS `Date.toISOString()`. They are sent both at the top level and
  mirrored into `filters.date`.
- **Default window** for recordings is the **last 2 days** when no dates are given.
- **`timezone`** for analytics defaults to the local machine's IANA zone (the MCP
  uses `Intl.DateTimeFormat().resolvedOptions().timeZone`); override with
  `--timezone`.

## Sort options (`--sort`)

| Value | Meaning |
|---|---|
| `SessionStart_DESC` | newest first (default) |
| `SessionStart_ASC` | oldest first |
| `SessionDuration_ASC` / `_DESC` | shortest / longest sessions |
| `SessionClickCount_ASC` / `_DESC` | fewest / most clicks |
| `PageCount_ASC` / `_DESC` | fewest / most page views |

`--count` maximum is **250** (default **100**).

## Filters reference (`recordings`)

Convenience flags cover the common filters. Everything else goes through
`--filters-json '<obj>'` or `--filters-file <path>`, which merge on top of the
flags. The full `FiltersType` object (from the MCP's schema) is:

**URL filters** — each is an array of `{"url": "...", "operator": "..."}` where
operator ∈ `contains, startsWith, endsWith, excludes, isExactly, isExactlyNot,
matchesRegex, excludesRegex`:
- `visitedUrls`, `entryUrls`, `exitUrls`
- `referringUrl` (plain string)

**User / segment:**
- `userType`: `NewUser` | `ReturningUser`
- `sessionIntent`: `Low Intention` | `Medium Intention` | `High Intention`

**Location:** `country[]`, `city[]`, `state[]` (full names, e.g. `"United States"`).

**Device / tech:**
- `deviceType[]`: `Mobile, Tablet, PC, Email, Other`
- `browser[]`: `Bot, MiuiBrowser, Chrome, CoralWebView, Edge, Other, Firefox, IE,
  Unknown, Headless, MobileApp, Opera, OperaMini, Safari, Samsung,
  SamsungInternet, Sogou, UCBrowser, YandexBrowser, QQBrowser`
- `os[]`: `BlackBerry, Android, ChromeOS, iOS, Linux, MacOS, Other, Windows,
  WindowsMobile`

**Marketing:**
- `source[]`, `medium[]`, `campaign[]` (UTM strings)
- `channel[]`: `OrganicSearch, Direct, Email, Display, Social, PaidSearch, Other,
  Affiliate, Referral, Video, Audio, SMS, AITools, PaidAITools`

**Events / errors:**
- `smartEvents[]` — user-defined or Clarity auto events (`Purchase, ContactUs,
  SubmitForm, AddToCart, SignUp, BeginCheckout, Download, Login, Search, …`)
- `javascriptErrors[]`, `clickErrors[]` — message patterns; **`""` matches any**.

**Interaction booleans** (`true` to require presence): `enteredTextPresent`,
`selectedTextPresent`, `resizeEventPresent`, `cursorMovement`, `deadClickPresent`,
`rageClickPresent`, `excessiveScrollPresent`, `quickbackClickPresent`.
Plus `clickedText` (string, partial match).

**Range filters** — each is `{"min": <n|null>, "max": <n|null>}`:
- Durations (minutes): `visiblePageDuration`, `hiddenPageDuration`, `pageDuration`,
  `sessionDuration`
- Engagement: `scrollDepth` (0–100), `pagesCount`, `pageClickEventCount`,
  `sessionClickEventCount`
- Performance: `performanceScore` (0–100), `largestContentfulPaint` (s),
  `cumulativeLayoutShift` (s), `firstInputDelay` (ms)
- Product: `productRating`, `productRatingsCount`, `productPrice`

**Product / e-commerce:** `productName` (string), `productBrand[]`,
`checkoutAbandonmentStep[]`, `productPurchases` (bool), `productAvailability` (bool).

**Date (required):**
```json
"date": { "start": "2026-06-01T00:00:00.000Z", "end": "2026-06-30T23:59:59.999Z" }
```
The CLI fills this for you from `--start/--end` or `--days`; you only need it in
raw JSON if you bypass those flags.

### Raw filter example

```bash
python3 scripts/clarity.py recordings --days 7 --sort SessionDuration_DESC \
  --filters-json '{
    "deviceType": ["Mobile"],
    "channel": ["PaidSearch"],
    "visitedUrls": [{"url": "/pricing", "operator": "contains"}],
    "scrollDepth": {"min": 75, "max": 100},
    "rageClickPresent": true
  }'
```

## Analytics query tips

`/dashboard/query` is natural-language and LLM-backed. Keep queries **single
metric + explicit time range**:

- ✅ `"Page views count for the last 7 days"`
- ✅ `"Top JavaScript errors for PC in January 2026"`
- ✅ `"Average session duration for desktop users this week"`
- ❌ `"Analyze user behavior"` (no metric, no range)
- ❌ `"Show page views, session duration and conversions for all devices"` (split it up)

## Troubleshooting

- **HTTP 401/403** — token missing, expired, or for a different project.
  Regenerate under Settings → Data Export.
- **Empty / vague analytics result** — the query was too broad or lacked a time
  range. Narrow it and add a range.
- **Unexpected 4xx/5xx after it used to work** — these `/mcp/*` endpoints are
  Microsoft-internal to their MCP server and can change without notice; check the
  [upstream repo](https://github.com/microsoft/clarity-mcp-server).

## Files

- `scripts/clarity.py` — the CLI (importable functions: `post`, `build_filters`,
  `resolve_token`, `iso_ms`, …).
- `SKILL.md` — the skill instructions loaded by the agent.
