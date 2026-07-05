#!/usr/bin/env python3
"""
hevy.py — a tiny, dependency-free client for Hevy's private web API.

Talks to the same `api.hevyapp.com` endpoints the hevy.com web app uses, so it
works WITHOUT a Pro API key. Auth is a bearer access token (short-lived, ~a few
hours) plus a rotating refresh token; both live in ~/.hevy/token.json. The
client auto-refreshes the access token when it is close to expiry and persists
the rotated tokens back to disk, so after the initial seed you never have to log
in (or solve reCAPTCHA) again.

Seeding the token (one time): log into hevy.com in a browser, then run the
companion `seed-token.sh` (uses agent-browser) OR paste the token JSON manually
(see README.md).

Endpoints (reverse-engineered from the web app):
  POST /auth/refresh_token                          refresh access token
  GET  /user/account                                account info
  GET  /workout_count                               total workout count
  GET  /user_workouts_paged?username&limit&offset   paged workout list
  GET  /routine/{id}                                single routine
  PUT  /routine/{id}?sendSyncEventToMobileApp=true  update routine
  POST /routine?sendSyncEventToMobileApp=true       create routine

@see README.md for the full capture notes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://api.hevyapp.com"
API_KEY = "shelobs_hevy_web"          # static web-client key, from x-api-key header
PLATFORM = "web"                        # Hevy-Platform header
TOKEN_PATH = os.path.expanduser("~/.hevy/token.json")

# Refresh the access token if it expires within this many seconds.
REFRESH_SKEW_SECONDS = 120


class HevyError(RuntimeError):
    pass


class HevyClient:
    """
    Thin wrapper over the private Hevy API with transparent token refresh.

    Token state is a dict: {access_token, refresh_token, expires_at, username, user_id}
    persisted to TOKEN_PATH. `expires_at` is an ISO-8601 string (UTC).
    """

    def __init__(self, token_path: str = TOKEN_PATH) -> None:
        self.token_path = token_path
        self.token = self._load_token()

    # ---- token persistence -------------------------------------------------

    def _load_token(self) -> dict:
        if not os.path.exists(self.token_path):
            raise HevyError(
                "No token found at %s.\n"
                "Seed it once: log into hevy.com in a browser, then run "
                "./seed-token.sh (or paste token JSON manually, see README)."
                % self.token_path
            )
        with open(self.token_path) as f:
            tok = json.load(f)
        for key in ("access_token", "refresh_token", "expires_at"):
            if not tok.get(key):
                raise HevyError("Token file %s is missing '%s'." % (self.token_path, key))
        return tok

    def _save_token(self) -> None:
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        with open(self.token_path, "w") as f:
            json.dump(self.token, f, indent=2)
        os.chmod(self.token_path, 0o600)

    # ---- auth --------------------------------------------------------------

    @staticmethod
    def _expires_epoch(iso: str) -> float:
        # Handles "2026-07-02T15:34:34.555Z" and offset forms.
        iso = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(iso).timestamp()

    def _seconds_to_expiry(self) -> float:
        return self._expires_epoch(self.token["expires_at"]) - time.time()

    def ensure_fresh(self, force: bool = False) -> None:
        if force or self._seconds_to_expiry() <= REFRESH_SKEW_SECONDS:
            self.refresh()

    def refresh(self) -> None:
        # POST /auth/refresh_token requires the (possibly-expired) access token
        # in the Authorization header AND the refresh token in the body.
        data = self._request(
            "POST",
            "/auth/refresh_token",
            body={"refresh_token": self.token["refresh_token"]},
            authorized=True,
            skip_refresh=True,
        )
        self.token["access_token"] = data["access_token"]
        self.token["refresh_token"] = data["refresh_token"]  # rotates every refresh
        self.token["expires_at"] = data["expires_at"]
        if data.get("user_id"):
            self.token["user_id"] = data["user_id"]
        self._save_token()

    # ---- low-level HTTP ----------------------------------------------------

    def _headers(self, authorized: bool, has_body: bool) -> dict:
        h = {
            "x-api-key": API_KEY,
            "Hevy-Platform": PLATFORM,
            "x-client-time": str(time.time()),
            "Accept": "application/json",
        }
        if authorized:
            h["authorization"] = "Bearer " + self.token["access_token"]
        if has_body:
            h["Content-Type"] = "application/json"
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        authorized: bool = True,
        skip_refresh: bool = False,
    ) -> dict:
        if authorized and not skip_refresh:
            self.ensure_fresh()

        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers=self._headers(authorized, body is not None),
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise HevyError("%s %s -> HTTP %s: %s" % (method, path, e.code, detail[:300]))
        except urllib.error.URLError as e:
            raise HevyError("%s %s -> network error: %s" % (method, path, e.reason))
        return json.loads(raw) if raw else {}

    # ---- API methods -------------------------------------------------------

    @property
    def username(self) -> str:
        u = self.token.get("username")
        if not u:
            u = self.account().get("username")
            self.token["username"] = u
            self._save_token()
        return u

    def account(self) -> dict:
        return self._request("GET", "/user/account")

    def workout_count(self) -> int:
        return self._request("GET", "/workout_count").get("workout_count", 0)

    def workouts_page(self, offset: int, limit: int) -> list[dict]:
        data = self._request(
            "GET",
            "/user_workouts_paged",
            params={"username": self.username, "limit": limit, "offset": offset},
        )
        return data.get("workouts", [])

    # Hevy caps this endpoint at limit=5; larger values 400.
    MAX_PAGE_SIZE = 5

    def iter_workouts(self, max_count: int | None = None, page_size: int = MAX_PAGE_SIZE):
        """Yield workouts newest-first, paging until exhausted or max_count hit."""
        page_size = min(page_size, self.MAX_PAGE_SIZE)
        offset, yielded = 0, 0
        while True:
            page = self.workouts_page(offset, page_size)
            if not page:
                return
            for w in page:
                yield w
                yielded += 1
                if max_count is not None and yielded >= max_count:
                    return
            offset += len(page)
            if len(page) < page_size:
                return

    def get_workout(self, workout_id: str) -> dict:
        return self._request("GET", "/workout/%s" % workout_id)

    def get_routine(self, routine_id: str) -> dict:
        return self._request("GET", "/routine/%s" % routine_id)["routine"]

    @staticmethod
    def _trim_routine(routine: dict) -> dict:
        # The API accepts only the shape the web app sends on save. Passing the
        # full routine object (from GET) with extra fields is rejected, so trim.
        return {
            "title": routine["title"],
            "exercises": [
                {
                    "exercise_template_id": e["exercise_template_id"],
                    "rest_seconds": e.get("rest_seconds"),
                    "sets": e["sets"],
                    "notes": e.get("notes", ""),
                    **({"superset_id": e["superset_id"]} if e.get("superset_id") else {}),
                }
                for e in routine["exercises"]
            ],
            "folder_id": routine.get("folder_id"),
            "index": routine.get("index", 0),
            "program_id": routine.get("program_id"),
            "notes": routine.get("notes"),
            "coach_force_rpe_enabled": routine.get("coach_force_rpe_enabled", False),
        }

    def update_routine(self, routine_id: str, routine: dict) -> dict:
        # Accepts either the trimmed shape or a full routine object (from GET).
        return self._request(
            "PUT",
            "/routine/%s" % routine_id,
            params={"sendSyncEventToMobileApp": "true"},
            body={"routine": self._trim_routine(routine)},
        )

    def create_routine(self, routine: dict) -> dict:
        return self._request(
            "POST",
            "/routine",
            params={"sendSyncEventToMobileApp": "true"},
            body={"routine": routine},
        )


# ---- formatting helpers ----------------------------------------------------

def _fmt_workout(w: dict) -> str:
    start = w.get("start_time")
    end = w.get("end_time")
    when = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if start else "?"
    dur = "%dm" % round((end - start) / 60) if (start and end) else "?"
    n_ex = len(w.get("exercises", []))
    n_sets = sum(len(e.get("sets", [])) for e in w.get("exercises", []))
    vol = w.get("estimated_volume_kg")
    vol_s = " %gkg" % vol if vol else ""
    return "%s  %-24.24s  %2d ex / %2d sets  %-4s%s  id=%s" % (
        when, w.get("name", ""), n_ex, n_sets, dur, vol_s, w.get("id", ""),
    )


def _fmt_set(s: dict, ex_type: str) -> str:
    ind = s.get("indicator", "normal")
    tag = {"warmup": "W", "drop": "D", "failure": "F"}.get(ind, "")
    tag = ("%s " % tag) if tag else ""
    reps = s.get("reps")
    wt = s.get("weight_kg")
    dur = s.get("duration_seconds")
    dist = s.get("distance_meters")
    rpe = s.get("rpe")
    parts = []
    if wt:
        parts.append("%gkg" % wt)
    if reps is not None:
        parts.append("x %d" % reps)
    if dur:
        parts.append("%ds" % dur)
    if dist:
        parts.append("%gm" % dist)
    body = " ".join(parts) or "-"
    rpe_s = "  @RPE %g" % rpe if rpe else ""
    prs = s.get("personalRecords") or s.get("prs") or []
    pr_s = "  \U0001f3c6" if prs else ""
    return "    %s%s%s%s" % (tag, body, rpe_s, pr_s)


def _fmt_workout_detail(w: dict) -> str:
    lines = []
    start, end = w.get("start_time"), w.get("end_time")
    when = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if start else "?"
    dur = "%d min" % round((end - start) / 60) if (start and end) else "?"
    lines.append("%s   %s" % (w.get("name", "(unnamed)"), when))
    n_sets = sum(len(e.get("sets", [])) for e in w.get("exercises", []))
    lines.append("%s  |  %d exercises / %d sets  |  vol %gkg  |  workout #%s"
                 % (dur, len(w.get("exercises", [])), n_sets,
                    w.get("estimated_volume_kg", 0), w.get("nth_workout", "?")))
    if w.get("description"):
        lines.append("note: %s" % w["description"])
    lines.append("id: %s   short_id: %s" % (w.get("id", ""), w.get("short_id", "")))
    lines.append("")
    for i, e in enumerate(w.get("exercises", []), 1):
        title = e.get("title", "?")
        rest = e.get("rest_seconds")
        rest_s = "  (rest %ds)" % rest if rest else ""
        lines.append("%d. %s%s" % (i, title, rest_s))
        if e.get("notes"):
            lines.append("    # %s" % e["notes"])
        for s in e.get("sets", []):
            lines.append(_fmt_set(s, e.get("exercise_type", "")))
    return "\n".join(lines)


def _within_days(w: dict, days: int) -> bool:
    start = w.get("start_time")
    if not start:
        return False
    return (time.time() - start) <= days * 86400


# ---- CLI -------------------------------------------------------------------

def cmd_whoami(client: HevyClient, args) -> None:
    a = client.account()
    print("username : %s" % a.get("username"))
    print("email    : %s" % a.get("email"))
    print("user_id  : %s" % a.get("id"))
    print("member   : since %s" % a.get("created_at"))
    print("token exp: %s (%.1f h left)" % (client.token["expires_at"], client._seconds_to_expiry() / 3600))


def cmd_count(client: HevyClient, args) -> None:
    print(client.workout_count())


def cmd_workouts(client: HevyClient, args) -> None:
    rows = []
    for w in client.iter_workouts(max_count=None if args.days else args.limit):
        if args.days and not _within_days(w, args.days):
            break  # list is newest-first, so we can stop
        rows.append(w)
    if args.json:
        json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return
    scope = "last %d days" % args.days if args.days else "last %d" % args.limit
    print("# %d workouts (%s)" % (len(rows), scope))
    for w in rows:
        print(_fmt_workout(w))


def cmd_export(client: HevyClient, args) -> None:
    out = args.out or "hevy_workouts_%s.json" % datetime.now().strftime("%Y%m%d")
    total = client.workout_count()
    all_w = []
    for w in client.iter_workouts(page_size=5):
        all_w.append(w)
        print("\r  fetched %d/%d" % (len(all_w), total), end="", file=sys.stderr, flush=True)
    print("", file=sys.stderr)
    with open(out, "w") as f:
        json.dump(all_w, f, indent=2, ensure_ascii=False)
    print("wrote %d workouts -> %s" % (len(all_w), out))


def cmd_workout_get(client: HevyClient, args) -> None:
    w = client.get_workout(args.id)
    if args.json:
        json.dump(w, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return
    print(_fmt_workout_detail(w))


def cmd_routine_get(client: HevyClient, args) -> None:
    r = client.get_routine(args.id)
    json.dump(r, sys.stdout, indent=2, ensure_ascii=False)
    print()


def cmd_routine_update(client: HevyClient, args) -> None:
    with open(args.file) as f:
        payload = json.load(f)
    # Accept either a bare routine object or a {"routine": {...}} wrapper.
    routine = payload.get("routine", payload)
    resp = client.update_routine(args.id, routine)
    print("updated routine %s" % args.id)
    if resp:
        json.dump(resp, sys.stdout, indent=2, ensure_ascii=False)
        print()


def cmd_token(client: HevyClient, args) -> None:
    if args.action == "status":
        left = client._seconds_to_expiry()
        print("expires_at : %s" % client.token["expires_at"])
        print("valid_for  : %.1f h (%s)" % (left / 3600, "OK" if left > 0 else "EXPIRED"))
        print("username   : %s" % client.token.get("username"))
    elif args.action == "refresh":
        client.refresh()
        print("refreshed; new expiry %s" % client.token["expires_at"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hevy", description="Unofficial Hevy web-API client")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="show account info").set_defaults(func=cmd_whoami)
    sub.add_parser("count", help="total workout count").set_defaults(func=cmd_count)

    w = sub.add_parser("workouts", help="list recent workouts")
    w.add_argument("--days", type=int, help="only workouts from the last N days")
    w.add_argument("--limit", type=int, default=10, help="max workouts when --days not given (default 10)")
    w.add_argument("--json", action="store_true", help="print raw JSON")
    w.set_defaults(func=cmd_workouts)

    wk = sub.add_parser("workout", help="single-workout detail")
    wksub = wk.add_subparsers(dest="waction", required=True)
    wkg = wksub.add_parser("get", help="show one workout with all exercises/sets")
    wkg.add_argument("id")
    wkg.add_argument("--json", action="store_true", help="print raw JSON")
    wkg.set_defaults(func=cmd_workout_get)

    e = sub.add_parser("export", help="dump ALL workouts to a JSON file")
    e.add_argument("--out", help="output path (default hevy_workouts_YYYYMMDD.json)")
    e.set_defaults(func=cmd_export)

    r = sub.add_parser("routine", help="routine read/update")
    rsub = r.add_subparsers(dest="raction", required=True)
    rg = rsub.add_parser("get", help="fetch a routine as JSON")
    rg.add_argument("id")
    rg.set_defaults(func=cmd_routine_get)
    ru = rsub.add_parser("update", help="update a routine from a JSON file")
    ru.add_argument("id")
    ru.add_argument("--file", required=True, help="JSON payload (routine object or {routine:{...}})")
    ru.set_defaults(func=cmd_routine_update)

    t = sub.add_parser("token", help="token status / manual refresh")
    t.add_argument("action", choices=["status", "refresh"])
    t.set_defaults(func=cmd_token)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = HevyClient()
        args.func(client, args)
    except HevyError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
