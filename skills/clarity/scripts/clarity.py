#!/usr/bin/env python3
"""
clarity.py — a self-contained CLI port of Microsoft's Clarity MCP server
(https://github.com/microsoft/clarity-mcp-server).

It drives the same three Microsoft-hosted endpoints the MCP server calls:

    POST https://clarity.microsoft.com/mcp/dashboard/query      (analytics, NL query)
    POST https://clarity.microsoft.com/mcp/recordings/sample    (session recordings, filters)
    POST https://clarity.microsoft.com/mcp/documentation/query  (docs search, NL query)

Auth is a Bearer "API token" generated in Clarity → Settings → Data Export.
Everything here is Python standard library only — no pip install, no MCP runtime.

Conventions match the sibling hevy/testrail/zendesk skills: token kept out of
argv, JSON on stdout, errors on stderr, exit non-zero on failure.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Non-ASCII output (Hebrew, Arabic, emoji, ...) must survive the trip to the
# terminal or a pipe even when the ambient locale is C/POSIX, where stdout
# would otherwise default to ASCII and raise UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # pragma: no cover - pre-3.7 / odd stream
        pass

API_BASE_URL = "https://clarity.microsoft.com/mcp"
ANALYTICS_DASHBOARD_URL = f"{API_BASE_URL}/dashboard/query"
SESSION_RECORDINGS_URL = f"{API_BASE_URL}/recordings/sample"
DOCUMENTATION_URL = f"{API_BASE_URL}/documentation/query"

TOKEN_FILE = Path.home() / ".clarity" / "token.json"

# The wire protocol sends sortBy as the numeric enum value (SortOptionsEnum[name]
# in the original TypeScript). Keep this mapping exact.
SORT_OPTIONS = {
    "SessionStart_DESC": 0,
    "SessionStart_ASC": 1,
    "SessionDuration_ASC": 2,
    "SessionDuration_DESC": 3,
    "SessionClickCount_ASC": 4,
    "SessionClickCount_DESC": 5,
    "PageCount_ASC": 6,
    "PageCount_DESC": 7,
}

# Enum choices mirrored from the MCP's zod schema, so we can validate before the
# round-trip and give useful errors instead of an opaque server 400.
DEVICE_TYPES = ["Mobile", "Tablet", "PC", "Email", "Other"]
BROWSERS = ["Bot", "MiuiBrowser", "Chrome", "CoralWebView", "Edge", "Other",
            "Firefox", "IE", "Unknown", "Headless", "MobileApp", "Opera",
            "OperaMini", "Safari", "Samsung", "SamsungInternet", "Sogou",
            "UCBrowser", "YandexBrowser", "QQBrowser"]
OPERATING_SYSTEMS = ["BlackBerry", "Android", "ChromeOS", "iOS", "Linux",
                     "MacOS", "Other", "Windows", "WindowsMobile"]


def eprint(*args):
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def resolve_token(cli_token=None):
    """Precedence: --token > CLARITY_API_TOKEN env > ~/.clarity/token.json."""
    if cli_token:
        return cli_token.strip()
    env = os.environ.get("CLARITY_API_TOKEN")
    if env:
        return env.strip()
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            fail(f"Could not read {TOKEN_FILE}: {e}")
        tok = data.get("api_token") or data.get("token") or data.get("access_token")
        if tok:
            return tok.strip()
    fail(
        "No Clarity API token found. Provide one via (in order of precedence):\n"
        "  --token <JWT>\n"
        "  export CLARITY_API_TOKEN=<JWT>\n"
        f"  {TOKEN_FILE}  ->  {{\"api_token\": \"<JWT>\"}}\n\n"
        "Generate the token in Clarity: open your project → Settings → Data Export\n"
        "→ 'Generate new API token'. It is scoped to that one project."
    )


def decode_jwt_payload(token):
    """Best-effort decode of a JWT's payload for `whoami`. Never verifies."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # pad
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def post(url, token, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        hint = ""
        if e.code in (401, 403):
            hint = ("\nThe token is missing, expired, or scoped to a different "
                    "project. Re-generate it in Clarity → Settings → Data Export.")
        fail(f"{url} returned HTTP {e.code} {e.reason}.{hint}\n{detail}".rstrip())
    except urllib.error.URLError as e:
        fail(f"Could not reach {url}: {e.reason}")

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Endpoint shapes are server-defined and may return plain text.
        return {"raw": raw}


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def iso_ms(dt):
    """Match JS Date.toISOString(): 2024-01-01T00:00:00.000Z (UTC, millis, Z)."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def local_iana_timezone():
    """Best-effort local IANA tz name (what the MCP passes via Intl). Fallback UTC."""
    tz = os.environ.get("TZ")
    if tz:
        return tz
    # Linux: /etc/localtime is usually a symlink into .../zoneinfo/<Area>/<City>
    localtime = Path("/etc/localtime")
    try:
        if localtime.is_symlink():
            target = os.readlink(localtime)
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    etc_tz = Path("/etc/timezone")
    try:
        if etc_tz.exists():
            name = etc_tz.read_text().strip()
            if name:
                return name
    except OSError:
        pass
    return "UTC"


def fail(msg, code=1):
    eprint(f"error: {msg}")
    sys.exit(code)


def emit(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Filters (session recordings)
# --------------------------------------------------------------------------- #
def build_filters(args):
    """Assemble the FiltersType object from convenience flags + raw overrides.

    Convenience flags cover the common cases; --filters-json / --filters-file are
    the escape hatch for the full schema (see README for every field)."""
    filters = {}

    # Raw base first, so convenience flags can layer on top.
    if args.filters_file:
        filters.update(_load_json_arg(Path(args.filters_file).read_text(), "--filters-file"))
    if args.filters_json:
        filters.update(_load_json_arg(args.filters_json, "--filters-json"))

    if args.device:
        _validate("--device", args.device, DEVICE_TYPES)
        filters["deviceType"] = args.device
    if args.browser:
        _validate("--browser", args.browser, BROWSERS)
        filters["browser"] = args.browser
    if args.os:
        _validate("--os", args.os, OPERATING_SYSTEMS)
        filters["os"] = args.os
    if args.country:
        filters["country"] = args.country
    if args.city:
        filters["city"] = args.city
    if args.url_contains:
        filters["visitedUrls"] = [{"url": u, "operator": "contains"} for u in args.url_contains]
    if args.clicked_text:
        filters["clickedText"] = args.clicked_text
    if args.smart_event:
        filters["smartEvents"] = args.smart_event
    if args.rage_clicks:
        filters["rageClickPresent"] = True
    if args.dead_clicks:
        filters["deadClickPresent"] = True
    if args.js_errors:
        # empty string matches ANY javascript error (per the schema)
        filters["javascriptErrors"] = [""]
    if args.min_duration is not None or args.max_duration is not None:
        filters["sessionDuration"] = {"min": args.min_duration, "max": args.max_duration}

    return filters


def _validate(flag, values, allowed):
    bad = [v for v in values if v not in allowed]
    if bad:
        fail(f"{flag}: invalid value(s) {bad}. Allowed: {', '.join(allowed)}")


def _load_json_arg(text, label):
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        fail(f"{label} is not valid JSON: {e}")
    if not isinstance(obj, dict):
        fail(f"{label} must be a JSON object (the filters map), got {type(obj).__name__}")
    return obj


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_analytics(args):
    token = resolve_token(args.token)
    tz = args.timezone or local_iana_timezone()
    result = post(ANALYTICS_DASHBOARD_URL, token, {"query": args.query, "timezone": tz})
    emit(result)


def cmd_docs(args):
    token = resolve_token(args.token)
    result = post(DOCUMENTATION_URL, token, {"query": args.query})
    emit(result)


def cmd_recordings(args):
    token = resolve_token(args.token)

    # Resolve the required date window. Precedence: explicit --start/--end, then
    # --days back from now, else the MCP default of "last 2 days".
    now = datetime.now(timezone.utc)
    if args.end:
        end_dt = _parse_dt(args.end, "--end")
    else:
        end_dt = now
    if args.start:
        start_dt = _parse_dt(args.start, "--start")
    elif args.days is not None:
        start_dt = end_dt - timedelta(days=args.days)
    else:
        start_dt = end_dt - timedelta(days=2)  # MCP default

    filters = build_filters(args)
    # The server also reads filters.date; mirror the top-level window into it so
    # both code paths agree (the reference server passes both).
    filters.setdefault("date", {"start": iso_ms(start_dt), "end": iso_ms(end_dt)})

    if args.count > 250:
        fail("--count max is 250")

    body = {
        "sortBy": SORT_OPTIONS[args.sort],
        "start": iso_ms(start_dt),
        "end": iso_ms(end_dt),
        "filters": filters,
        "count": args.count,
    }
    result = post(SESSION_RECORDINGS_URL, token, body)
    _warn_on_truncated_window(result, start_dt, end_dt, args.count)
    if args.summary:
        emit_recordings_summary(result)
    else:
        emit(result)


def _session_times(result):
    """Parsed timestamps of a /recordings/sample response, oldest first."""
    if not isinstance(result, list):
        return []
    out = []
    for r in result:
        ts = r.get("timestamp") if isinstance(r, dict) else None
        if not ts:
            continue
        try:
            out.append(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return sorted(out)


def _warn_on_truncated_window(result, start_dt, end_dt, count):
    """The endpoint samples the most RECENT sessions and silently ignores the rest
    of the window. On a busy project a '30 day' query can come back covering only
    minutes — which reads as 'no such session' when it really means 'never looked'.
    Say so on stderr rather than letting the caller conclude absence."""
    times = _session_times(result)
    if not times:
        return
    requested = (end_dt - start_dt).total_seconds()
    covered = (times[-1] - times[0]).total_seconds()
    hit_cap = isinstance(result, list) and len(result) >= count
    # Only meaningful when the result set is cap-sized; a short-of-cap response
    # means the filter really did exhaust the matches.
    if hit_cap and requested > 0 and covered < requested * 0.5:
        eprint(
            f"warning: got {len(result)} sessions (= --count cap) spanning only "
            f"{_human_span(covered)} ({times[0]:%Y-%m-%d %H:%M} → {times[-1]:%Y-%m-%d %H:%M} UTC), "
            f"but you asked for {_human_span(requested)}.\n"
            "         /recordings/sample returns the most recent matches and stops at the cap, "
            "so older sessions in your window were never searched.\n"
            "         An empty or short result here is NOT proof the session doesn't exist. "
            "Narrow the filters or query smaller windows with --start/--end."
        )


def _human_span(seconds):
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def emit_recordings_summary(result):
    """One block per session instead of the full nested JSON — the raw response is
    ~200 lines per session, which buries the answer when you are looking for one."""
    if not isinstance(result, list):
        emit(result)
        return
    if not result:
        print("no sessions matched")
        return
    for r in result:
        timeline = r.get("timeline") or []
        events = [e for p in timeline for e in (p.get("timelineEvents") or [])]
        kinds = {}
        for e in events:
            kinds[e.get("eventtype", "?")] = kinds.get(e.get("eventtype", "?"), 0) + 1
        print(f"{r.get('timestamp')}  {r.get('totalDuration')} "
              f"({r.get('activeDuration')} active)  "
              f"{r.get('pagesCount')} page(s)  {r.get('sessionClickCount')} click(s)")
        print(f"  player: {r.get('link')}")
        for p in timeline:
            print(f"  [{p.get('start')}] {p.get('url')}")
            title = p.get("displayTitle")
            if title:
                print(f"           title: {title}")
        if kinds:
            print("  events: " + ", ".join(f"{k}×{v}" for k, v in sorted(kinds.items())))
        print()
    print(f"— {len(result)} session(s)")


def _parse_dt(s, flag):
    txt = s.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        fail(f"{flag}: could not parse '{s}'. Use ISO 8601, e.g. 2024-01-31 or "
             "2024-01-31T23:59:59Z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def cmd_whoami(args):
    token = resolve_token(args.token)
    payload = decode_jwt_payload(token)
    out = {"token_source": _token_source(args.token), "token_prefix": token[:12] + "…"}
    if payload:
        # surface the fields Clarity tokens commonly carry, without assuming all exist
        for k in ("pid", "projectId", "sub", "exp", "iat", "aud", "iss"):
            if k in payload:
                out[k] = payload[k]
        if "exp" in payload:
            out["expires_utc"] = datetime.fromtimestamp(payload["exp"], timezone.utc).isoformat()
        out["jwt_payload"] = payload
    else:
        out["note"] = "token is not a decodable JWT; cannot introspect (it may still be valid)"

    # Clarity tokens carry no project id in their claims, but the token IS scoped to
    # one project — so a query that "finds nothing" is often just the wrong project.
    # Recover the id from a player link (…/player/<projectId>/<sessionId>/…).
    if not args.no_probe:
        out["project_id"] = _probe_project_id(token) or "unknown (no recent sessions to sample)"
        out["project_hint"] = ("Compare this against the project id in your "
                               "clarity.microsoft.com URL. If they differ, this token "
                               "cannot see that project's data — generate a token there.")
    emit(out)


def _post_quiet(url, token, body):
    """post() variant that returns None instead of printing and exiting. Used by
    whoami's probe, where a failure is informational, not fatal."""
    saved, sys.stderr = sys.stderr, open(os.devnull, "w")
    try:
        return post(url, token, body)
    except SystemExit:
        return None
    finally:
        sys.stderr.close()
        sys.stderr = saved


def _probe_project_id(token):
    """Best-effort: sample one recent recording and read the project id off its link."""
    now = datetime.now(timezone.utc)
    start, end = iso_ms(now - timedelta(days=3)), iso_ms(now)
    try:
        # The server 500s unless filters carries its own date window, so mirror
        # exactly what cmd_recordings sends rather than posting bare filters.
        result = _post_quiet(SESSION_RECORDINGS_URL, token, {
            "sortBy": SORT_OPTIONS["SessionStart_DESC"],
            "start": start,
            "end": end,
            "filters": {"date": {"start": start, "end": end}},
            "count": 1,
        })
    except SystemExit:
        return None
    if not isinstance(result, list):
        return None
    for r in result:
        link = r.get("link") if isinstance(r, dict) else None
        if link and "/player/" in link:
            tail = link.split("/player/", 1)[1].split("/")
            if tail and tail[0]:
                return tail[0]
    return None


def _token_source(cli_token):
    if cli_token:
        return "--token"
    if os.environ.get("CLARITY_API_TOKEN"):
        return "CLARITY_API_TOKEN env"
    if TOKEN_FILE.exists():
        return str(TOKEN_FILE)
    return "unknown"


# --------------------------------------------------------------------------- #
# Argparse
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="clarity.py",
        description="CLI port of the Microsoft Clarity MCP server (analytics, "
                    "session recordings, documentation).",
    )
    p.add_argument("--token", help="Clarity API token (else env / token file).")
    sub = p.add_subparsers(dest="command", required=True)

    # analytics
    a = sub.add_parser(
        "analytics",
        help="Query the analytics dashboard with a natural-language question.",
        description="POST /dashboard/query. Use SIMPLE, single-purpose queries "
                    "with an explicit time range, e.g. 'Top browsers last 3 days'.",
    )
    a.add_argument("query", help="Natural-language query, e.g. 'Page views last 7 days'.")
    a.add_argument("--timezone", help="IANA tz (default: local machine tz).")
    a.set_defaults(func=cmd_analytics)

    # docs
    d = sub.add_parser(
        "docs",
        help="Search Microsoft Clarity documentation (natural language).",
        description="POST /documentation/query. Focus on one topic per query.",
    )
    d.add_argument("query", help="Documentation question, e.g. 'How do I set up masking?'")
    d.set_defaults(func=cmd_docs)

    # recordings
    r = sub.add_parser(
        "recordings",
        help="List session recordings matching filters.",
        description="POST /recordings/sample. The date window is required; it "
                    "defaults to the last 2 days if you pass neither --start/--end "
                    "nor --days.",
    )
    r.add_argument("--count", type=int, default=100, help="Samples to return (max 250, default 100).")
    r.add_argument("--sort", default="SessionStart_DESC", choices=list(SORT_OPTIONS),
                   help="Sort order (default SessionStart_DESC = newest first).")
    r.add_argument("--days", type=int, help="Look back N days from now (window helper).")
    r.add_argument("--start", help="Window start, ISO 8601 (UTC). Overrides --days.")
    r.add_argument("--end", help="Window end, ISO 8601 (UTC). Default: now.")
    # convenience filters
    r.add_argument("--device", nargs="+", metavar="TYPE", help=f"deviceType in: {', '.join(DEVICE_TYPES)}")
    r.add_argument("--browser", nargs="+", metavar="NAME", help="browser (e.g. Chrome Safari Edge)")
    r.add_argument("--os", nargs="+", metavar="OS", help=f"os in: {', '.join(OPERATING_SYSTEMS)}")
    r.add_argument("--country", nargs="+", metavar="C", help="country names, e.g. 'United States'")
    r.add_argument("--city", nargs="+", metavar="C", help="city names")
    r.add_argument("--url-contains", nargs="+", metavar="URL", dest="url_contains",
                   help="visitedUrls with the 'contains' operator")
    r.add_argument("--clicked-text", dest="clicked_text", help="clickedText partial match")
    r.add_argument("--smart-event", nargs="+", dest="smart_event", metavar="EVENT",
                   help="smartEvents, e.g. Purchase AddToCart SubmitForm")
    r.add_argument("--rage-clicks", action="store_true", dest="rage_clicks",
                   help="only sessions with rage clicks")
    r.add_argument("--dead-clicks", action="store_true", dest="dead_clicks",
                   help="only sessions with dead clicks")
    r.add_argument("--js-errors", action="store_true", dest="js_errors",
                   help="only sessions with any JavaScript error")
    r.add_argument("--min-duration", type=float, dest="min_duration",
                   help="min session duration (minutes)")
    r.add_argument("--max-duration", type=float, dest="max_duration",
                   help="max session duration (minutes)")
    r.add_argument("--filters-json", dest="filters_json",
                   help="Raw FiltersType JSON object (escape hatch for any field).")
    r.add_argument("--filters-file", dest="filters_file",
                   help="Path to a JSON file holding the FiltersType object.")
    r.add_argument("--summary", action="store_true",
                   help="Print a compact per-session digest (timestamp, player link, "
                        "URLs, event counts) instead of the full nested JSON.")
    r.set_defaults(func=cmd_recordings)

    # whoami
    w = sub.add_parser("whoami", help="Show the resolved token source, its claims, and the project it is scoped to.")
    w.add_argument("--no-probe", action="store_true", dest="no_probe",
                   help="Skip the recordings probe used to discover the project id.")
    w.set_defaults(func=cmd_whoami)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
