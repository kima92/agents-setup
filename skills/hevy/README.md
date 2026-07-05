# hevy-cli

A tiny, **dependency-free** Python client for [Hevy](https://hevy.com)'s private
web API. Uses the same `api.hevyapp.com` endpoints the hevy.com web app calls, so
it works **without a Pro API key**.

- Read your recent workouts / export your whole history
- Read & update **routines** (workout templates)
- Auto-refreshing auth — seed the token once, never log in again

> Personal-use tool against **your own** account. It just replays the requests
> the web app already makes on your behalf.

## Install

```bash
npx skills add https://github.com/kima92/agents-setup --skill hevy
```

## Requirements

- Python 3.9+ (standard library only — no `pip install` needed)
- A browser logged into hevy.com for the one-time token seed
  (the `seed-token.sh` helper uses [`agent-browser`](https://www.npmjs.com/package/agent-browser))

## Setup (one time)

Hevy's login is protected by Google reCAPTCHA, so we don't script the
username/password login. Instead we lift the auth token out of a browser session
**once**; after that the client refreshes it automatically forever.

### Option A — automatic, via agent-browser

```bash
# 1. Log into hevy.com by hand in an agent-browser session named "hevy"
agent-browser --session hevy --headed open https://hevy.com/login
#    (type your credentials in the window that opens)

# 2. Lift the token into ~/.hevy/token.json
./seed-token.sh
```

### Option B — manual

1. Log into hevy.com in any browser.
2. DevTools → Application → Cookies → copy the `auth2.0-token` cookie value
   (URL-decode it — it's a JSON blob).
3. Create `~/.hevy/token.json` (chmod 600) with:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": "2026-07-02T15:34:34.555Z",
  "username": "yourname",
  "user_id": "..."
}
```

The access token is short-lived (~15 min), but the client refreshes it using the
`refresh_token` (which itself rotates and is persisted back to the file) before
every run, so you only ever seed once.

## Usage

```bash
python3 hevy.py whoami                 # account info + token expiry
python3 hevy.py count                  # total workout count

python3 hevy.py workouts --limit 5     # N most-recent workouts (summary)
python3 hevy.py workouts --days 30     # workouts in the last 30 days
python3 hevy.py workouts --days 7 --json   # raw JSON (full nested exercises/sets)

python3 hevy.py workout get <workout_id>       # one workout, full exercise/set breakdown
python3 hevy.py workout get <workout_id> --json

python3 hevy.py export                  # dump ALL workouts -> hevy_workouts_YYYYMMDD.json
python3 hevy.py export --out mine.json

python3 hevy.py routine get <routine_id> > r.json   # fetch a routine
# edit r.json (title, exercises[].sets[].reps / weight_kg, rest_seconds, notes ...)
python3 hevy.py routine update <routine_id> --file r.json

python3 hevy.py token status            # show expiry / validity
python3 hevy.py token refresh           # force a refresh
```

`routine update` accepts either the full object from `routine get` or a trimmed
`{title, exercises, ...}` — it normalizes to the shape the API expects.

## Using it as a library

```python
from hevy import HevyClient

c = HevyClient()
for w in c.iter_workouts(max_count=10):
    print(w["name"], w["estimated_volume_kg"])

r = c.get_routine("25fdb66f-...")
r["exercises"][0]["sets"][0]["reps"] = 8
c.update_routine(r["id"], r)
```

## API notes (reverse-engineered)

Every request carries these headers:

| Header | Value |
|---|---|
| `x-api-key` | `shelobs_hevy_web` (static web-client key) |
| `Hevy-Platform` | `web` |
| `authorization` | `Bearer <access_token>` |
| `x-client-time` | unix epoch seconds |

Endpoints:

| Method | Path | Notes |
|---|---|---|
| `POST` | `/auth/refresh_token` | body `{refresh_token}` + `Bearer <access_token>` header → `{access_token, refresh_token, expires_at}` (refresh token **rotates**) |
| `GET` | `/user/account` | account info |
| `GET` | `/workout_count` | `{workout_count}` |
| `GET` | `/user_workouts_paged?username&limit&offset` | `{workouts:[...]}`. **`limit` max is 5** (higher → HTTP 400) |
| `GET` | `/workout/{id}` | single workout (full object, unwrapped) |
| `GET` | `/routine/{id}` | `{routine:{...}}` |
| `GET` | `/routine_with_short_id/{shortId}` | by short id |
| `PUT` | `/routine/{id}?sendSyncEventToMobileApp=true` | body `{routine:{title, exercises:[{exercise_template_id, rest_seconds, sets, notes}], folder_id, index, program_id, notes, coach_force_rpe_enabled}}` |
| `POST` | `/routine?sendSyncEventToMobileApp=true` | create routine (same body shape) |

**Data model:** `workout → exercises[] → sets[]`. A set has
`{reps, weight_kg, rpe, duration_seconds, distance_meters, indicator, index, ...}`.

## Limitations

- **Completed workouts are read-only** — Hevy's web app doesn't expose an edit
  endpoint for logged sessions (only routines are editable on web). You can read
  and export them, but not modify them through this client. Editing routines
  (templates) works fully.
- `user_workouts_paged` caps at 5 per page; the client pages automatically.
- Uses undocumented endpoints — Hevy could change them at any time.
