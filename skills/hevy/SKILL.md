---
name: hevy
description: Read, export, and update your Hevy (hevy.com) workout-tracking data through the app's private web API as a self-contained Python CLI — no Hevy Pro / API key required. Use whenever the user wants to see their recent workouts or lifting history, get a single workout's exercise/set breakdown, export their whole Hevy history to JSON/analysis, check a workout count or streak, or read and edit their routines (workout templates) — e.g. "show my last few Hevy workouts", "what did I lift on leg day", "export all my Hevy data", "bump the rest times on my Pull routine", "how many workouts have I logged". Trigger on any mention of Hevy, or of workouts/routines/sets/reps/PRs/gym logs in the context of the Hevy app, even when the private API isn't named. Not for other fitness apps (Strong, Strava, Garmin) or for editing already-completed workouts (Hevy's web API only lets you edit routines, not logged sessions).
---

# Hevy

Drives Hevy's **private web API** (`api.hevyapp.com`) — the same endpoints the
hevy.com web app calls — from one dependency-free Python CLI
(`scripts/hevy.py`). This works **without a Hevy Pro subscription or public API
key**: it authenticates as the web client using a bearer token lifted once from
a logged-in browser, then keeps that token alive by refreshing it automatically.

It follows the same conventions as the sibling `testrail`/`zendesk` skills:
token/credential auth kept out of argv, JSON on stdout, errors on stderr, Python
stdlib only (no `pip install`).

## How auth works (read this first)

Hevy has two APIs. The **public** one needs a Pro "API key" and is rate-limited;
this skill deliberately does **not** use it. Instead it uses the **web/internal**
API, which authenticates with:

- `x-api-key: shelobs_hevy_web` — a static web-client key (baked into the CLI)
- `authorization: Bearer <access_token>` — a per-user token, ~15 min TTL
- a `refresh_token` that mints new access tokens (and rotates itself)

The access token is short-lived, but the CLI refreshes it via
`POST /auth/refresh_token` before every run and writes the rotated tokens back to
`~/.hevy/token.json`, so **you seed once and never log in again**. Login itself
is protected by Google reCAPTCHA and is intentionally *not* scripted.

## Prerequisites

- Python 3.9+ (standard library only)
- A one-time token seed in `~/.hevy/token.json` (see below)

### Seeding the token (one time)

If `~/.hevy/token.json` is missing, the CLI errors with instructions. Two ways
to create it:

**Automatic — via the `agent-browser` skill** (preferred when available):

```bash
# 1. Open a headed browser session named "hevy" and have the USER log in by hand
agent-browser --session hevy --headed open https://hevy.com/login
# (the user types their credentials + solves reCAPTCHA in the window)

# 2. Lift the readable auth cookie into ~/.hevy/token.json
bash scripts/seed-token.sh
```

Never type the user's Hevy password yourself — let them log in in the browser.

**Manual:** log into hevy.com, copy the URL-decoded `auth2.0-token` cookie
(DevTools → Application → Cookies), and write `~/.hevy/token.json` (chmod 600)
with `access_token`, `refresh_token`, `expires_at`, `username`, `user_id`. See
`README.md` for the exact shape.

If refresh ever fails (password change, long inactivity → refresh token
revoked), re-seed the same way.

## Commands

Run with Python 3. When installed, the path is typically
`.claude/skills/hevy/scripts/hevy.py`; from the skill dir it's `scripts/hevy.py`.

```bash
python3 scripts/hevy.py <command> [options]
```

| Command | What it does |
|---|---|
| `whoami` | Account info + token expiry |
| `count` | Total number of logged workouts |
| `workouts [--limit N] [--days N] [--json]` | Recent workouts, newest-first. `--days N` filters to the last N days; otherwise `--limit N` (default 10). `--json` prints raw nested data. |
| `workout get <id> [--json]` | One workout in full: every exercise with its sets (reps × weight, RPE, PRs 🏆). `--json` for raw. |
| `export [--out FILE]` | Fetch **all** workouts (auto-paged) → JSON file. Default `hevy_workouts_YYYYMMDD.json`. Use this for any analysis/stats over history. |
| `routine get <id>` | Fetch a routine (template) as JSON |
| `routine update <id> --file payload.json` | Update a routine from a JSON file (accepts the full object from `routine get` or a trimmed shape — it normalizes) |
| `token status` / `token refresh` | Inspect or force-refresh the token |

### Typical flows

**"Show my recent workouts" / "how've my lifts been":**
```bash
python3 scripts/hevy.py workouts --days 30
```
Newest-first; each line is date, name, exercise/set counts, duration, volume, id.

**"What exactly did I do in that workout":** grab the `id` from the list, then
```bash
python3 scripts/hevy.py workout get <id>
```

**"Analyze my training" / "export everything":**
```bash
python3 scripts/hevy.py export --out ~/hevy_all.json
```
Then read/aggregate the JSON. Each workout is `{name, start_time, end_time,
estimated_volume_kg, exercises:[{title, exercise_type, rest_seconds,
sets:[{reps, weight_kg, rpe, duration_seconds, distance_meters, indicator}]}]}`.
Times are unix seconds (UTC). Editing completed workouts is **not** possible via
this API — read/export only.

**"Change my routine" (e.g. adjust rest times, add a set, edit reps):**
```bash
python3 scripts/hevy.py routine get <routine_id> > /tmp/r.json
# edit /tmp/r.json — routine.exercises[].sets[].reps / weight_kg, rest_seconds, notes, ...
python3 scripts/hevy.py routine update <routine_id> --file /tmp/r.json
```
Get a routine's id from a workout's `routine_id` field, or from `routine get`.

## Gotchas

- **`user_workouts_paged` caps `limit` at 5** (higher → HTTP 400). The CLI pages
  around this automatically; don't raise it.
- **Completed workouts are read-only.** Hevy's web app exposes no edit endpoint
  for logged sessions — only routines (`PUT /routine/{id}`) are editable. If the
  user asks to fix a logged set, explain this limitation.
- **Undocumented endpoints.** Hevy can change them at any time; if a call starts
  4xx-ing unexpectedly, re-capture from the web app (DevTools → Network). The
  full endpoint reference and capture notes are in `README.md`.

## Files

- `scripts/hevy.py` — the CLI + importable `HevyClient` class (use it as a
  library for custom analysis: `from hevy import HevyClient`)
- `scripts/seed-token.sh` — one-time token seeder (needs `agent-browser`)
- `README.md` — full reverse-engineered API reference (headers, endpoints, data
  model) and setup details
