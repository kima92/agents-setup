#!/usr/bin/env python3
"""
Airtable Web API CLI Tool

A command-line interface for Airtable's REST API (https://api.airtable.com/v0):
discovering bases and their schema, reading and filtering records, reading
record comments, and -- when writes are explicitly enabled -- creating,
updating and upserting records and posting comments.

This tool is intentionally non-destructive: it has no delete command at all.
Airtable's API deletes records permanently and irreversibly, so the capability
is simply absent rather than guarded. The strongest "remove" it performs is
clearing a cell value through an update.

It also handles, once, the things every ad-hoc Airtable script has to redo:
URL-encoding formulas and sort params, looping over `offset` pagination,
splitting writes into the API's 10-record batches, pacing requests under the
5 requests/second/base limit (and backing off the 30-second penalty after a
429), and checking field names against the table schema before a write.

HTTP transport: uses `curl` by default (capturing the status via
`-w "%{http_code}"`) and falls back to Python's stdlib `urllib` when curl is
not on PATH. No third-party libraries are required.

Authentication is a Bearer personal access token (PAT). Legacy `key...` API
keys were deprecated in February 2024 and no longer work.

Environment Variables Required:
    AIRTABLE_API_KEY   - A personal access token (starts with "pat..."), created
                         at https://airtable.com/create/tokens .
                         AIRTABLE_PAT and AIRTABLE_TOKEN are accepted as aliases.

Optional:
    AIRTABLE_BASE_ID   - Default base id ("app...") so --base can be omitted.
    AIRTABLE_READ_ONLY - Read-only mode, ON by default. While enabled, every
                         write command is refused and no mutating request is
                         ever sent. Set it to 0/false/no/off to allow writes.
"""

import argparse
import csv
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

# Non-ASCII output (Hebrew, Arabic, emoji, ...) must survive the trip to the
# terminal or a pipe even when the ambient locale is C/POSIX, where stdout
# would otherwise default to ASCII and raise UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # pragma: no cover - pre-3.7 / odd stream
        pass


API_ROOT = "https://api.airtable.com/v0"

# Airtable's batch endpoints accept at most 10 records per request, and list
# endpoints return at most 100 per page. Both are hard server-side caps.
MAX_BATCH = 10
MAX_PAGE_SIZE = 100

# 5 requests/second/base. Exceeding it returns 429 and locks the base out for
# 30 seconds, so it is much cheaper to pace requests than to hit the wall.
RATE_LIMIT_PER_SEC = 5
RATE_LIMIT_WINDOW = 1.05          # small margin over 1s for clock jitter
RATE_LIMIT_PENALTY = 30.0
RATE_LIMIT_MAX_RETRIES = 2

# Airtable rejects URLs longer than 16,000 characters. A long filterByFormula
# is the usual cause; past this threshold we switch to the POST listRecords
# endpoint, which carries the same parameters in a JSON body.
MAX_URL_LENGTH = 15000

# Field types Airtable computes itself. Writing to one always fails, so naming
# one in a create/update is a mistake worth catching before the request goes out.
COMPUTED_FIELD_TYPES = {
    "formula", "rollup", "count", "lookup", "multipleLookupValues",
    "createdTime", "lastModifiedTime", "createdBy", "lastModifiedBy",
    "autoNumber", "button", "externalSyncSource",
}


# --- Errors ---

class ApiError(Exception):
    """Raised when the Airtable API returns a non-2xx status."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class TransportError(Exception):
    """Raised when the request could not be sent at all (curl/urllib failure)."""


class ConfigurationError(Exception):
    """Raised when required configuration (env vars, arguments) is missing."""


class ReadOnlyError(Exception):
    """Raised when a write is attempted while AIRTABLE_READ_ONLY is enabled."""


# --- Read-only mode ---

# Commands that mutate Airtable data; refused while read-only mode is on.
WRITE_COMMANDS = {
    "create-records",
    "update-records",
    "upsert-records",
    "add-comment",
}

# Values that turn read-only mode OFF. Anything else (including unset) leaves it
# ON: the default is read-only, so a typo fails safe.
_READ_ONLY_OFF = {"0", "false", "no", "off"}


def is_read_only():
    """Whether write operations are disabled. Defaults to True (read-only)."""
    return os.environ.get("AIRTABLE_READ_ONLY", "1").strip().lower() not in _READ_ONLY_OFF


# --- Identifier helpers ---

def _id_of(prefix, value):
    """Return value if it is a bare Airtable id with this prefix, else None.

    Airtable ids are exactly a 3-character prefix plus 14 alphanumerics
    (appSW9R5uCNmRmfl6). Matching that exactly keeps a base *named* something
    like "applications" from being mistaken for an id.
    """
    s = (value or "").strip()
    return s if re.fullmatch(prefix + r"[A-Za-z0-9]{14}", s) else None


def ids_from_url(value):
    """Pull base/table/view/record ids out of a pasted airtable.com URL.

    People copy the URL out of the browser rather than hunting for ids, and a
    grid URL (airtable.com/appX/tblY/viwZ) already contains everything we need.
    Returns a dict with whichever of base/table/view/record were present.
    """
    s = (value or "").strip()
    if "airtable.com" not in s:
        return {}
    found = {}
    for key, prefix in (("base", "app"), ("table", "tbl"), ("view", "viw"), ("record", "rec")):
        m = re.search(r"/(" + prefix + r"[A-Za-z0-9]{14})", s)
        if m:
            found[key] = m.group(1)
    return found


# --- HTTP transport (curl by default, urllib fallback) ---

def _curl_cfg_quote(value):
    """Escape a value for use inside a double-quoted curl config entry."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _curl_request(method, url, headers, data):
    """Perform a request with curl, returning (status_code, response_text).

    Headers (including the bearer token), URL and body are passed via a temp
    config file (`-K`) and temp files so nothing sensitive leaks into argv.
    The HTTP status is captured with `-w "%{http_code}"`; a non-2xx status is
    returned to the caller rather than raised here.
    """
    cfg_fd, cfg_path = tempfile.mkstemp(prefix="at-curl-", suffix=".cfg")
    out_fd, out_path = tempfile.mkstemp(prefix="at-curl-", suffix=".out")
    os.close(out_fd)
    data_path = None
    try:
        cfg_lines = [
            'request = "{}"'.format(_curl_cfg_quote(method)),
            'url = "{}"'.format(_curl_cfg_quote(url)),
            'output = "{}"'.format(_curl_cfg_quote(out_path)),
            'write-out = "%{http_code}"',
            "silent",
            "show-error",
        ]
        for key, value in headers.items():
            cfg_lines.append('header = "{}: {}"'.format(_curl_cfg_quote(key), _curl_cfg_quote(value)))

        if data is not None:
            data_fd, data_path = tempfile.mkstemp(prefix="at-curl-", suffix=".dat")
            with os.fdopen(data_fd, "w", encoding="utf-8") as f:
                f.write(data)
            cfg_lines.append('data-binary = "@{}"'.format(_curl_cfg_quote(data_path)))

        with os.fdopen(cfg_fd, "w", encoding="utf-8") as f:
            f.write("\n".join(cfg_lines) + "\n")

        result = subprocess.run(["curl", "-K", cfg_path], capture_output=True, text=True)
        if result.returncode != 0:
            raise TransportError("curl request failed: {}".format(
                result.stderr.strip() or "exit code {}".format(result.returncode)))

        status = int(result.stdout.strip() or "0")
        with open(out_path, "r", encoding="utf-8") as f:
            text = f.read()
        return status, text
    finally:
        for path in (cfg_path, out_path, data_path):
            if path and os.path.exists(path):
                os.unlink(path)


def _urllib_request(method, url, headers, data):
    """Perform a request with stdlib urllib, returning (status_code, response_text)."""
    body = data.encode("utf-8") if isinstance(data, str) else data
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.getcode(), resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # Non-2xx responses surface here; return them like curl would.
        return e.code, e.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise TransportError("urllib request failed: {}".format(e.reason))


def http_request(method, url, headers=None, data=None):
    """Send an HTTP request via curl (default) or urllib (fallback).

    Returns (status_code, response_text). Does not raise on HTTP error status
    codes -- the caller inspects status_code. Raises TransportError when the
    request cannot be delivered at all.
    """
    headers = dict(headers or {})
    if shutil.which("curl"):
        return _curl_request(method, url, headers, data)
    return _urllib_request(method, url, headers, data)


# --- Rate limiting ---

class RatePacer:
    """Keeps request rate under Airtable's per-base ceiling.

    The limit is enforced per base, so each base gets its own window; metadata
    calls share a single "meta" bucket. Sleeping for a few hundred milliseconds
    here is far cheaper than tripping the 429, which locks the base for 30s.
    """

    def __init__(self, per_sec=RATE_LIMIT_PER_SEC, window=RATE_LIMIT_WINDOW):
        self.per_sec = per_sec
        self.window = window
        self._recent = {}

    def wait(self, key):
        stamps = self._recent.setdefault(key, deque())
        now = time.monotonic()
        while stamps and now - stamps[0] > self.window:
            stamps.popleft()
        if len(stamps) >= self.per_sec:
            delay = self.window - (now - stamps[0])
            if delay > 0:
                time.sleep(delay)
            now = time.monotonic()
            while stamps and now - stamps[0] > self.window:
                stamps.popleft()
        stamps.append(time.monotonic())


# --- Formula building ---

def formula_literal(value):
    """Render a Python value as an Airtable formula literal.

    Strings are double-quoted -- Airtable accepts either quote style, and
    double quotes keep apostrophes (common in names) from needing escapes.
    """
    if isinstance(value, bool):
        return "TRUE()" if value else "FALSE()"
    if isinstance(value, (int, float)):
        return repr(value)
    s = str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def field_ref(name):
    """Render a field reference for use inside a formula."""
    if "}" in name:
        raise ConfigurationError(
            "Field name {!r} contains '}}', which cannot be escaped in an Airtable field "
            "reference. Use --formula with a FIELD()-free alternative, or reference the "
            "field by a view/fields filter instead.".format(name))
    return "{" + name + "}"


# Longest operators first so "!=" is not read as "=" and ">=" not as ">".
_WHERE_OPS = ["!=", ">=", "<=", "~", "=", ">", "<"]

# ISO-ish dates get date-aware treatment below.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")


def _date_clause(ref, op, value):
    """Compare a date/datetime field the way Airtable actually wants.

    Plain arithmetic operators on date fields are unreliable, and `=` against a
    datetime never matches because the stored value carries a time. Airtable's
    own IS_BEFORE / IS_AFTER / IS_SAME are exact, so route date-looking values
    through them -- this is the filter people ask for most and get wrong most.
    """
    lit = formula_literal(value)
    if op == "=":
        return "IS_SAME({}, {}, 'day')".format(ref, lit)
    if op == "!=":
        return "NOT(IS_SAME({}, {}, 'day'))".format(ref, lit)
    if op == ">":
        return "IS_AFTER({}, {})".format(ref, lit)
    if op == "<":
        return "IS_BEFORE({}, {})".format(ref, lit)
    if op == ">=":
        return "OR(IS_AFTER({0}, {1}), IS_SAME({0}, {1}, 'day'))".format(ref, lit)
    return "OR(IS_BEFORE({0}, {1}), IS_SAME({0}, {1}, 'day'))".format(ref, lit)


def parse_where(clause):
    """Turn a "Field=Value" style clause into an Airtable formula fragment.

    Supported operators, chosen to cover what people actually ask for without
    making them learn Airtable's formula language:
        Field=Value    equals (Field= with nothing after it means "is empty")
        Field!=Value   not equal
        Field~Value    contains, case-insensitive
        Field>Value    greater than (also >=, <, <=)
    """
    for op in _WHERE_OPS:
        idx = clause.find(op)
        if idx > 0:
            name = clause[:idx].strip()
            value = clause[idx + len(op):].strip()
            if not name:
                break
            ref = field_ref(name)
            if op == "=" and value == "":
                return "{} = BLANK()".format(ref)
            if op == "!=" and value == "":
                return "NOT({} = BLANK())".format(ref)
            if op == "~":
                # &"" coerces numbers/dates to text so FIND() does not error on them.
                return "FIND(LOWER({}), LOWER({} & \"\")) > 0".format(
                    formula_literal(value), ref)
            if _DATE_RE.match(value):
                return _date_clause(ref, op, value)
            if op in (">", ">=", "<", "<="):
                try:
                    literal = formula_literal(float(value) if "." in value else int(value))
                except ValueError:
                    literal = formula_literal(value)
                return "{} {} {}".format(ref, op, literal)
            return "{} {} {}".format(ref, op, formula_literal(value))
    raise ConfigurationError(
        "Could not parse --where {!r}. Expected FIELD=VALUE (or !=, ~, >, >=, <, <=), "
        "e.g. --where 'Status=Todo'.".format(clause))


def build_formula(raw, where_clauses):
    """Combine --formula and any --where clauses into one formula (AND'd)."""
    parts = []
    if raw:
        parts.append(raw if raw.strip().startswith("(") else "({})".format(raw))
    for clause in where_clauses or []:
        parts.append("({})".format(parse_where(clause)))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "AND({})".format(", ".join(parts))


# --- Client ---

class AirtableClient:
    """Thin wrapper over the Airtable Web API."""

    def __init__(self):
        token = ""
        for name in ("AIRTABLE_API_KEY", "AIRTABLE_PAT", "AIRTABLE_TOKEN"):
            token = os.environ.get(name, "").strip()
            if token:
                break
        if not token:
            raise ConfigurationError(
                "Missing required environment variable: AIRTABLE_API_KEY (a personal "
                "access token from https://airtable.com/create/tokens; AIRTABLE_PAT and "
                "AIRTABLE_TOKEN are also accepted).")
        if token.startswith("key"):
            raise ConfigurationError(
                "AIRTABLE_API_KEY looks like a legacy API key ('key...'), which Airtable "
                "deprecated in February 2024. Create a personal access token ('pat...') at "
                "https://airtable.com/create/tokens .")

        self.token = token
        self.read_only = is_read_only()
        self.default_base = os.environ.get("AIRTABLE_BASE_ID", "").strip() or None
        self.pacer = RatePacer()
        self._schema_cache = {}

    # -- low-level --

    def _request(self, method, path, params=None, body=None, base_key="meta", is_write=None):
        """Send one API request, pacing it and retrying a 429 once the lockout clears.

        `is_write` defaults to "any method that is not a GET", but a couple of
        Airtable's read endpoints are POSTs (listRecords). Those pass
        is_write=False explicitly: the read-only guard is about whether data
        changes, not about the HTTP verb.
        """
        if is_write is None:
            is_write = method.upper() not in ("GET", "HEAD")
        if self.read_only and is_write:
            raise ReadOnlyError(
                "Read-only mode is on (AIRTABLE_READ_ONLY): refusing to send {} {}. "
                "Set AIRTABLE_READ_ONLY=0 to allow write operations.".format(method, path))

        url = "{}{}".format(API_ROOT, path if path.startswith("/") else "/" + path)
        if params:
            clean = [(k, v) for k, v in params if v is not None] if isinstance(params, list) \
                else [(k, v) for k, v in params.items() if v is not None]
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        headers = {"Authorization": "Bearer {}".format(self.token), "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False)

        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            self.pacer.wait(base_key)
            status, text = http_request(method, url, headers=headers, data=data)
            if status == 429 and attempt < RATE_LIMIT_MAX_RETRIES:
                sys.stderr.write(
                    "Rate limited by Airtable (429). Waiting {:.0f}s for the per-base "
                    "lockout to clear, then retrying...\n".format(RATE_LIMIT_PENALTY))
                sys.stderr.flush()
                time.sleep(RATE_LIMIT_PENALTY)
                continue
            break

        if status == 204 or not text:
            payload = {}
        else:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {"raw": text}

        if status < 200 or status >= 300:
            raise ApiError(self._error_message(status, method, url, payload, text),
                           status_code=status)
        return payload

    @staticmethod
    def _error_message(status, method, url, payload, text):
        """Build an error message, adding the hint that usually explains this status.

        Airtable's error bodies are terse ({"error": "NOT_FOUND"}), and the two
        most common failures -- a token that has not been granted access to the
        base, and a mistyped table name -- look identical to auth problems
        unless you know the per-base access model. Say so here, once.
        """
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or err.get("type") or ""
            err_type = err.get("type") or ""
        elif isinstance(err, str):
            detail, err_type = err, err
        else:
            detail, err_type = "", ""
        detail = detail or text[:500]

        hint = ""
        if status == 401:
            hint = ("\nHint: the token is missing, malformed, or revoked. Personal access "
                    "tokens start with 'pat'; legacy 'key...' keys no longer work.")
        elif status == 403 or err_type in ("NOT_AUTHORIZED", "INVALID_PERMISSIONS"):
            hint = ("\nHint: a 403 on one base while others work almost always means the "
                    "token's Access list does not include this base, or it lacks the needed "
                    "scope (data.records:read/write, schema.bases:read). Both are edited at "
                    "https://airtable.com/create/tokens .")
        elif status == 404 or err_type in ("NOT_FOUND", "TABLE_NOT_FOUND", "MODEL_ID_NOT_FOUND"):
            hint = ("\nHint: check the base id, table name/id and record id. Run the "
                    "`schema` command to see the exact table and field names.")
        elif err_type == "INVALID_MULTIPLE_CHOICE_OPTIONS":
            hint = ("\nHint: the select option does not exist yet. Either use one of the "
                    "existing choices (visible in `schema`) or pass --typecast to let "
                    "Airtable create it.")
        elif err_type == "UNKNOWN_FIELD_NAME":
            hint = "\nHint: field names are case- and space-sensitive. Run `schema` to confirm."
        elif status == 429:
            hint = ("\nHint: Airtable allows 5 requests/second/base and locks the base for "
                    "30s after a 429. Retries were exhausted; wait and re-run.")
        elif status == 422:
            hint = ("\nHint: 422 means the payload shape is wrong for these field types -- "
                    "e.g. a linked record needs an array of record ids. See "
                    "references/field_types.md.")

        return "Airtable API error (HTTP {}) on {} {}: {}{}".format(
            status, method, url, detail, hint)

    # -- resolution --

    def resolve_base(self, value):
        """Resolve a base reference (id, name, or pasted URL) to an 'app...' id."""
        value = (value or "").strip()
        if not value:
            if not self.default_base:
                raise ConfigurationError(
                    "No base specified. Pass --base (an 'app...' id, a base name, or a "
                    "pasted airtable.com URL) or set AIRTABLE_BASE_ID.")
            value = self.default_base

        from_url = ids_from_url(value).get("base")
        if from_url:
            return from_url
        as_id = _id_of("app", value)
        if as_id:
            return as_id

        bases = self.list_bases().get("bases", [])
        matches = [b for b in bases if b.get("name", "").strip().lower() == value.lower()]
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            raise ApiError("Several bases are named {!r}: {}. Pass the app... id instead.".format(
                value, ", ".join(b["id"] for b in matches)))
        near = difflib.get_close_matches(value, [b.get("name", "") for b in bases], n=3, cutoff=0.5)
        raise ApiError("No base named {!r}.{} Run `list-bases` to see them all.".format(
            value, " Did you mean: {}?".format(", ".join(near)) if near else ""))

    def get_schema(self, base_id):
        """Fetch (and cache) the table schema for a base."""
        if base_id not in self._schema_cache:
            self._schema_cache[base_id] = self._request(
                "GET", "/meta/bases/{}/tables".format(base_id), base_key=base_id)
        return self._schema_cache[base_id]

    def try_get_schema(self, base_id):
        """Schema if the token can read it, else None.

        Schema access is a separate scope from record access, so a token that
        can read records perfectly well may not be able to see the schema.
        Callers use this for validation and pretty output, both of which are
        nice-to-have -- never fail the user's actual request over it.
        """
        try:
            return self.get_schema(base_id)
        except (ApiError, ReadOnlyError):
            return None

    def resolve_table(self, base_id, value, need_schema=False):
        """Resolve a table reference to (table_id_or_name, table_schema_or_None).

        Airtable accepts a table *name* in the URL path, so reads need no lookup
        at all -- and skipping it keeps the common path to a single request,
        which matters under a 5 requests/second budget. Writes pass
        need_schema=True, where one extra call buys field-name validation and a
        useful error instead of a bare UNKNOWN_FIELD_NAME.
        """
        value = (value or "").strip()
        if not value:
            raise ConfigurationError("No table specified. Pass --table (a name, a 'tbl...' id, "
                                     "or a pasted airtable.com URL).")
        from_url = ids_from_url(value).get("table")
        if from_url:
            value = from_url

        if not need_schema:
            return value, None

        schema = self.try_get_schema(base_id)
        if not schema:
            return value, None

        tables = schema.get("tables", [])
        for t in tables:
            if t.get("id") == value:
                return t["id"], t
        for t in tables:
            if t.get("name", "").strip().lower() == value.lower():
                return t["id"], t

        near = difflib.get_close_matches(value, [t.get("name", "") for t in tables], n=3, cutoff=0.5)
        raise ApiError("No table named {!r} in base {}.{} Run `schema --base {}` to list them.".format(
            value, base_id,
            " Did you mean: {}?".format(", ".join(near)) if near else "", base_id))

    # -- metadata --

    def whoami(self):
        return self._request("GET", "/meta/whoami")

    def list_bases(self):
        """Every base the token can see, following offset pagination."""
        bases, offset = [], None
        while True:
            params = {"offset": offset} if offset else None
            page = self._request("GET", "/meta/bases", params=params)
            bases.extend(page.get("bases", []))
            offset = page.get("offset")
            if not offset:
                break
        return {"bases": bases}

    # -- records --

    def list_records(self, base_id, table, fields=None, formula=None, view=None,
                     sort=None, max_records=None, page_size=None, fetch_all=False,
                     offset=None, cell_format=None, time_zone=None, user_locale=None,
                     return_field_ids=False):
        """List records, following `offset` pagination when asked to.

        Returns {"records": [...], "offset": <str or None>}. One page is fetched
        unless fetch_all is set or max_records asks for more than a page holds --
        pulling a whole base by accident is expensive in both requests and context.
        """
        page_size = min(page_size or MAX_PAGE_SIZE, MAX_PAGE_SIZE)
        collected = []
        while True:
            remaining = None if max_records is None else max_records - len(collected)
            if remaining is not None and remaining <= 0:
                break
            params = [
                ("pageSize", min(page_size, remaining) if remaining else page_size),
                ("filterByFormula", formula),
                ("view", view),
                ("offset", offset),
                ("cellFormat", cell_format),
                ("timeZone", time_zone),
                ("userLocale", user_locale),
            ]
            if return_field_ids:
                params.append(("returnFieldsByFieldId", "true"))
            for name in fields or []:
                params.append(("fields[]", name))
            for i, (field, direction) in enumerate(sort or []):
                params.append(("sort[{}][field]".format(i), field))
                params.append(("sort[{}][direction]".format(i), direction))

            page = self._list_records_page(base_id, table, params)
            collected.extend(page.get("records", []))
            offset = page.get("offset")
            if not offset:
                break
            if not fetch_all and max_records is None:
                break
        if max_records is not None:
            collected = collected[:max_records]
        return {"records": collected, "offset": offset}

    def _list_records_page(self, base_id, table, params):
        """One page of records, over GET or -- for very long URLs -- POST listRecords."""
        path = "/{}/{}".format(base_id, urllib.parse.quote(table, safe=""))
        probe = "{}{}?{}".format(API_ROOT, path,
                                 urllib.parse.urlencode([(k, v) for k, v in params if v is not None]))
        if len(probe) <= MAX_URL_LENGTH:
            return self._request("GET", path, params=params, base_key=base_id)

        # Airtable caps URLs at 16,000 characters; the POST twin of this endpoint
        # takes the same options in a body. It reads data, so it is not a write.
        body = {}
        sorts = {}
        for key, value in params:
            if value is None:
                continue
            if key == "fields[]":
                body.setdefault("fields", []).append(value)
            elif key.startswith("sort["):
                idx, prop = re.match(r"sort\[(\d+)\]\[(\w+)\]", key).groups()
                sorts.setdefault(int(idx), {})[prop] = value
            else:
                body[key] = value
        if sorts:
            body["sort"] = [sorts[i] for i in sorted(sorts)]
        return self._request("POST", path + "/listRecords", body=body,
                             base_key=base_id, is_write=False)

    def get_record(self, base_id, table, record_id):
        return self._request(
            "GET", "/{}/{}/{}".format(base_id, urllib.parse.quote(table, safe=""), record_id),
            base_key=base_id)

    def list_comments(self, base_id, table, record_id, page_size=None, fetch_all=False):
        comments, offset = [], None
        while True:
            params = {"pageSize": min(page_size or MAX_PAGE_SIZE, MAX_PAGE_SIZE), "offset": offset}
            page = self._request(
                "GET", "/{}/{}/{}/comments".format(
                    base_id, urllib.parse.quote(table, safe=""), record_id),
                params=params, base_key=base_id)
            comments.extend(page.get("comments", []))
            offset = page.get("offset")
            if not offset or not fetch_all:
                break
        return {"comments": comments, "offset": offset}

    # -- writes --

    def create_records(self, base_id, table, records, typecast=False, return_field_ids=False):
        """Create records, splitting into the API's 10-per-request batches."""
        created = []
        for chunk in _chunks(records, MAX_BATCH):
            body = {"records": [{"fields": r} for r in chunk]}
            if typecast:
                body["typecast"] = True
            if return_field_ids:
                body["returnFieldsByFieldId"] = True
            page = self._request("POST", "/{}/{}".format(
                base_id, urllib.parse.quote(table, safe="")), body=body, base_key=base_id)
            created.extend(page.get("records", []))
        return {"records": created}

    def update_records(self, base_id, table, records, typecast=False, destructive=False):
        """Update records by id, in batches of 10.

        PATCH merges the supplied fields; PUT (destructive=True) clears every
        field you did not include, which is almost never what someone means by
        "update", so it has to be asked for explicitly.
        """
        method = "PUT" if destructive else "PATCH"
        updated = []
        for chunk in _chunks(records, MAX_BATCH):
            body = {"records": chunk}
            if typecast:
                body["typecast"] = True
            page = self._request(method, "/{}/{}".format(
                base_id, urllib.parse.quote(table, safe="")), body=body, base_key=base_id)
            updated.extend(page.get("records", []))
        return {"records": updated}

    def upsert_records(self, base_id, table, records, merge_on, typecast=False,
                       destructive=False):
        """Create-or-update records matched on up to three merge fields."""
        method = "PUT" if destructive else "PATCH"
        result = {"records": [], "createdRecords": [], "updatedRecords": []}
        for chunk in _chunks(records, MAX_BATCH):
            body = {
                "performUpsert": {"fieldsToMergeOn": merge_on},
                "records": [{"fields": r} for r in chunk],
            }
            if typecast:
                body["typecast"] = True
            page = self._request(method, "/{}/{}".format(
                base_id, urllib.parse.quote(table, safe="")), body=body, base_key=base_id)
            result["records"].extend(page.get("records", []))
            result["createdRecords"].extend(page.get("createdRecords", []))
            result["updatedRecords"].extend(page.get("updatedRecords", []))
        return result

    def add_comment(self, base_id, table, record_id, text):
        return self._request(
            "POST", "/{}/{}/{}/comments".format(
                base_id, urllib.parse.quote(table, safe=""), record_id),
            body={"text": text}, base_key=base_id)


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# --- Field validation ---

def validate_fields(table_schema, field_sets, typecast=False):
    """Check field names against the table schema before sending a write.

    Airtable answers a typo with UNKNOWN_FIELD_NAME and no suggestion, and a
    partially-applied batch is worse than a rejected one -- so catch it here,
    where we can point at the field that was probably meant.
    """
    if not table_schema:
        return
    by_name = {f.get("name"): f for f in table_schema.get("fields", [])}
    known = set(by_name) | {f.get("id") for f in table_schema.get("fields", [])}
    problems = []
    for fields in field_sets:
        for name in fields:
            if name not in known:
                near = difflib.get_close_matches(name, list(by_name), n=3, cutoff=0.5)
                problems.append("unknown field {!r}{}".format(
                    name, " (did you mean {}?)".format(" / ".join(repr(n) for n in near)) if near else ""))
            elif by_name.get(name, {}).get("type") in COMPUTED_FIELD_TYPES:
                problems.append("field {!r} is a {} field, which Airtable computes and "
                                "cannot accept a written value".format(
                                    name, by_name[name]["type"]))
    if problems:
        raise ApiError("Table {!r} rejected before sending:\n  - {}\nRun `schema` to see the "
                       "exact field names and types.".format(
                           table_schema.get("name"), "\n  - ".join(sorted(set(problems)))))


# --- Output ---

def output_json(data):
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _cell_text(value):
    """Flatten any Airtable cell value to one line of readable text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(item.get("name") or item.get("email") or item.get("filename")
                             or item.get("id") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return ", ".join(parts)
    if isinstance(value, dict):
        return (value.get("name") or value.get("email") or value.get("text")
                or json.dumps(value, ensure_ascii=False))
    return " ".join(str(value).split())


def render_records_text(records, columns=None, width=40, more=False):
    """Aligned table of records, truncated per cell.

    Records come back as sparse JSON (Airtable omits empty cells entirely), so a
    table is both the most scannable and the most token-efficient way to show a
    page of them.
    """
    if not records:
        return "No records matched."
    if not columns:
        columns = []
        for rec in records:
            for key in rec.get("fields", {}):
                if key not in columns:
                    columns.append(key)
    header = ["id"] + list(columns)
    rows = []
    for rec in records:
        fields = rec.get("fields", {})
        rows.append([rec.get("id", "")] + [_cell_text(fields.get(c)) for c in columns])

    def clip(s):
        return s if len(s) <= width else s[:width - 1] + "…"

    rows = [[clip(c) for c in row] for row in rows]
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(header)).rstrip(),
             "  ".join("-" * w for w in widths)]
    for row in rows:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    lines.append("")
    lines.append("{} record(s){}".format(
        len(records), " - more pages remain; re-run with --all or --max-records N" if more else ""))
    return "\n".join(lines)


def render_records_csv(records, columns=None):
    if not columns:
        columns = []
        for rec in records:
            for key in rec.get("fields", {}):
                if key not in columns:
                    columns.append(key)
    writer = csv.writer(sys.stdout)
    writer.writerow(["id"] + list(columns))
    for rec in records:
        fields = rec.get("fields", {})
        writer.writerow([rec.get("id", "")] + [_cell_text(fields.get(c)) for c in columns])
    return ""


def render_schema_text(schema, table_filter=None):
    """Compact view of a base's tables, fields and views.

    This is what you read before writing anything: exact field names, which
    options a select allows, and which fields are computed (and so unwritable).
    """
    lines = []
    for table in schema.get("tables", []):
        if table_filter and table_filter.lower() not in (
                table.get("name", "").lower(), table.get("id", "").lower()):
            continue
        fields = table.get("fields", [])
        primary = next((f.get("name") for f in fields
                        if f.get("id") == table.get("primaryFieldId")), "?")
        lines.append("{} ({})  -  {} field(s), primary: {}".format(
            table.get("name"), table.get("id"), len(fields), primary))
        name_w = max([len(f.get("name", "")) for f in fields] or [0])
        for f in fields:
            ftype = f.get("type", "")
            opts = f.get("options") or {}
            extra = ""
            if opts.get("choices"):
                extra = "[{}]".format(", ".join(c.get("name", "") for c in opts["choices"]))
            elif opts.get("linkedTableId"):
                extra = "-> {}".format(opts["linkedTableId"])
            if ftype in COMPUTED_FIELD_TYPES:
                extra = (extra + " " if extra else "") + "(computed, read-only)"
            lines.append("  {}  {}{}".format(
                f.get("name", "").ljust(name_w), ftype.ljust(22), extra).rstrip())
        views = table.get("views", [])
        if views:
            lines.append("  views: {}".format(", ".join(v.get("name", "") for v in views)))
        lines.append("")
    if not lines:
        return "No matching tables." if table_filter else "This base has no tables."
    return "\n".join(lines).rstrip()


def render_comments_text(comments):
    if not comments:
        return "No comments on this record."
    lines = []
    for c in comments:
        author = c.get("author") or {}
        lines.append("[{}] {}".format(
            c.get("createdTime", ""), author.get("name") or author.get("email") or "?"))
        lines.append("  " + (c.get("text") or "").replace("\n", "\n  "))
        lines.append("")
    return "\n".join(lines).rstrip()


# --- Argument plumbing ---

def split_list(value):
    """Split a comma-separated option value, keeping it None when unset."""
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def collect_fields(args):
    """Merge --fields / --field / --field-json into one field dict.

    Three forms because they answer three different needs: --fields for a whole
    JSON object, --field for the common "just set this text" case, --field-json
    when the value is a number, boolean, or array (linked records, multi-selects).
    """
    fields = {}
    if getattr(args, "fields", None):
        try:
            parsed = json.loads(args.fields)
        except ValueError as e:
            raise ConfigurationError("--fields is not valid JSON: {}".format(e))
        if not isinstance(parsed, dict):
            raise ConfigurationError("--fields must be a JSON object of field name -> value.")
        fields.update(parsed)
    for item in getattr(args, "field", None) or []:
        name, sep, value = item.partition("=")
        if not sep:
            raise ConfigurationError("--field expects NAME=VALUE, got {!r}.".format(item))
        fields[name.strip()] = value
    for item in getattr(args, "field_json", None) or []:
        name, sep, value = item.partition("=")
        if not sep:
            raise ConfigurationError("--field-json expects NAME=<json>, got {!r}.".format(item))
        try:
            fields[name.strip()] = json.loads(value)
        except ValueError as e:
            raise ConfigurationError("--field-json value for {!r} is not valid JSON: {}".format(
                name.strip(), e))
    return fields


def load_records_file(path, want_ids):
    """Read a batch of records from a JSON file (or stdin with '-').

    Accepts the shapes people naturally write -- a bare list of field objects,
    or the API's own [{"fields": {...}}] -- so a file dumped from list-records
    can be edited and fed straight back in.
    """
    text = sys.stdin.read() if path == "-" else open(path, "r", encoding="utf-8").read()
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConfigurationError("{} is not valid JSON: {}".format(path, e))
    if isinstance(data, dict) and "records" in data:
        data = data["records"]
    if not isinstance(data, list):
        raise ConfigurationError(
            "Expected a JSON array of records in {} (or an object with a 'records' array).".format(path))

    out = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ConfigurationError("Record {} in {} is not an object.".format(i, path))
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else \
            {k: v for k, v in item.items() if k != "id"}
        if want_ids:
            rec_id = item.get("id")
            if not rec_id:
                raise ConfigurationError(
                    "Record {} in {} has no 'id'. Updates match on record id -- use "
                    "upsert-records if you want to match on a field value instead.".format(i, path))
            out.append({"id": rec_id, "fields": fields})
        else:
            out.append(fields)
    return out


def parse_sort(values):
    """Parse --sort 'Field' / 'Field:desc' into (field, direction) pairs."""
    pairs = []
    for item in values or []:
        field, sep, direction = item.partition(":")
        direction = (direction or "asc").strip().lower()
        if direction not in ("asc", "desc"):
            raise ConfigurationError(
                "--sort direction must be asc or desc, got {!r}.".format(direction))
        pairs.append((field.strip(), direction))
    return pairs


def add_target_args(p, table_required=True):
    p.add_argument("--base", help="Base id (app...), base name, or a pasted airtable.com URL. "
                                  "Defaults to $AIRTABLE_BASE_ID.")
    p.add_argument("--table", required=table_required,
                   help="Table name, table id (tbl...), or a pasted airtable.com URL.")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Airtable Web API CLI",
        epilog="Read-only mode is ON by default (AIRTABLE_READ_ONLY=1): commands marked "
               "[write] are refused. Set AIRTABLE_READ_ONLY=0 to enable them. There is no "
               "delete command -- this tool cannot destroy Airtable data.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="Show the token's user id and scopes (cheapest auth check)")

    p = sub.add_parser("list-bases", help="List every base the token can see")
    p.add_argument("--format", choices=["text", "json"], default="text")

    p = sub.add_parser("schema", help="Show a base's tables, fields, select options and views")
    p.add_argument("--base", help="Base id, name, or URL. Defaults to $AIRTABLE_BASE_ID.")
    p.add_argument("--table", help="Limit the output to one table")
    p.add_argument("--format", choices=["text", "json"], default="text")

    p = sub.add_parser("list-records", help="List/filter records")
    add_target_args(p)
    p.add_argument("--where", action="append",
                   help="Filter as FIELD=VALUE (also !=, ~ for contains, >, >=, <, <=). "
                        "Repeatable; clauses are AND'd. Handles quoting and URL-encoding.")
    p.add_argument("--formula", help="Raw filterByFormula, for anything --where cannot express")
    p.add_argument("--view", help="Read through a named view (applies its saved filter and sort)")
    p.add_argument("--fields", help="Comma-separated field names to return")
    p.add_argument("--sort", action="append", help="FIELD or FIELD:desc (repeatable)")
    p.add_argument("--max-records", type=int, help="Stop after N records (paginates as needed)")
    p.add_argument("--page-size", type=int, help="Records per request (max 100)")
    p.add_argument("--all", action="store_true", help="Follow pagination to the end")
    p.add_argument("--offset", help="Resume from an offset returned by a previous call")
    p.add_argument("--cell-format", choices=["json", "string"],
                   help="'string' returns cells as the UI formats them (needs --time-zone)")
    p.add_argument("--time-zone")
    p.add_argument("--user-locale")
    p.add_argument("--format", choices=["text", "json", "csv"], default="text")

    p = sub.add_parser("get-record", help="Get one record by id")
    add_target_args(p)
    p.add_argument("--record", required=True, help="Record id (rec...) or a pasted record URL")
    p.add_argument("--format", choices=["json", "text"], default="json")

    p = sub.add_parser("list-comments", help="List the comments on a record")
    add_target_args(p)
    p.add_argument("--record", required=True)
    p.add_argument("--all", action="store_true")
    p.add_argument("--format", choices=["text", "json"], default="text")

    p = sub.add_parser("create-records", help="[write] Create one or many records")
    add_target_args(p)
    p.add_argument("--fields", help="Field values as a JSON object")
    p.add_argument("--field", action="append", metavar="NAME=VALUE",
                   help="Set one field to a string value (repeatable)")
    p.add_argument("--field-json", action="append", metavar="NAME=JSON",
                   help="Set one field to a JSON value: numbers, booleans, arrays (repeatable)")
    p.add_argument("--records-file", help="JSON array of records ('-' for stdin); batched by 10")
    p.add_argument("--typecast", dest="typecast", action="store_true", default=None,
                   help="Let Airtable coerce values and create missing select options")
    p.add_argument("--no-typecast", dest="typecast", action="store_false", default=None)

    p = sub.add_parser("update-records", help="[write] Update records by id")
    add_target_args(p)
    p.add_argument("--record", help="Record id to update (with --fields/--field)")
    p.add_argument("--fields", help="Field values as a JSON object")
    p.add_argument("--field", action="append", metavar="NAME=VALUE")
    p.add_argument("--field-json", action="append", metavar="NAME=JSON")
    p.add_argument("--records-file",
                   help="JSON array of {\"id\": ..., \"fields\": {...}} ('-' for stdin)")
    p.add_argument("--typecast", dest="typecast", action="store_true", default=None)
    p.add_argument("--no-typecast", dest="typecast", action="store_false", default=None)
    p.add_argument("--destructive", action="store_true",
                   help="Use PUT: clears every field not named in the request")

    p = sub.add_parser("upsert-records",
                       help="[write] Create-or-update records matched on field values")
    add_target_args(p)
    p.add_argument("--merge-on", required=True,
                   help="1-3 comma-separated field names that uniquely identify a record")
    p.add_argument("--fields", help="Field values as a JSON object")
    p.add_argument("--field", action="append", metavar="NAME=VALUE")
    p.add_argument("--field-json", action="append", metavar="NAME=JSON")
    p.add_argument("--records-file", help="JSON array of records ('-' for stdin)")
    p.add_argument("--typecast", dest="typecast", action="store_true", default=None)
    p.add_argument("--no-typecast", dest="typecast", action="store_false", default=None)

    p = sub.add_parser("add-comment", help="[write] Post a comment on a record")
    add_target_args(p)
    p.add_argument("--record", required=True)
    p.add_argument("--text", help="Comment body")
    p.add_argument("--text-file", help="Read the comment body from a file ('-' for stdin)")

    return parser


def resolve_text(direct, from_file):
    """Return text from --x or --x-file (use '-' for stdin)."""
    if from_file is not None:
        if from_file == "-":
            return sys.stdin.read()
        with open(from_file, "r", encoding="utf-8") as f:
            return f.read()
    return direct


def record_id_arg(value):
    """Accept a rec... id or a pasted record URL."""
    from_url = ids_from_url(value).get("record")
    return from_url or (value or "").strip()


def gather_write_records(args, want_ids=False):
    """Collect the records for a write from either --records-file or the --field flags."""
    if getattr(args, "records_file", None):
        return load_records_file(args.records_file, want_ids)
    fields = collect_fields(args)
    if not fields:
        raise ConfigurationError(
            "Nothing to write. Pass --fields / --field / --field-json for a single record, "
            "or --records-file for a batch.")
    if want_ids:
        if not getattr(args, "record", None):
            raise ConfigurationError(
                "update-records needs --record (the rec... id to update) when fields are "
                "given inline. Use --records-file to update several records at once.")
        return [{"id": record_id_arg(args.record), "fields": fields}]
    return [fields]


def resolve_typecast(args):
    """Decide whether to send typecast=true.

    Explicit --typecast/--no-typecast always wins. Otherwise it is on when the
    values came from --field, because those are strings by construction: a
    number or date field would reject "42" or "2026-04-01" without coercion,
    and that failure is confusing rather than protective.
    """
    if args.typecast is not None:
        return args.typecast
    return bool(getattr(args, "field", None)) and not getattr(args, "records_file", None)


def dispatch(client, args):
    cmd = args.command

    # Refuse write commands up front, before any lookup call is made.
    if client.read_only and cmd in WRITE_COMMANDS:
        raise ReadOnlyError(
            "'{}' is a write operation and read-only mode is on (AIRTABLE_READ_ONLY defaults "
            "to 1). Set AIRTABLE_READ_ONLY=0 to allow it.".format(cmd))

    if cmd == "whoami":
        return client.whoami()

    if cmd == "list-bases":
        data = client.list_bases()
        if args.format == "json":
            return data
        bases = data["bases"]
        if not bases:
            return ("No bases are visible to this token. Add them to the token's Access list "
                    "at https://airtable.com/create/tokens .")
        width = max(len(b.get("name", "")) for b in bases)
        return "\n".join("{}  {}  {}".format(
            b.get("name", "").ljust(width), b.get("id", ""), b.get("permissionLevel", ""))
            for b in bases)

    if cmd == "schema":
        base_id = client.resolve_base(args.base)
        schema = client.get_schema(base_id)
        if args.format == "json":
            return schema
        return render_schema_text(schema, args.table)

    base_id = client.resolve_base(getattr(args, "base", None))
    table, table_schema = client.resolve_table(
        base_id, getattr(args, "table", None), need_schema=cmd in WRITE_COMMANDS)

    if cmd == "list-records":
        fields = split_list(args.fields)
        data = client.list_records(
            base_id, table,
            fields=fields,
            formula=build_formula(args.formula, args.where),
            view=args.view,
            sort=parse_sort(args.sort),
            max_records=args.max_records,
            page_size=args.page_size,
            fetch_all=args.all,
            offset=args.offset,
            cell_format=args.cell_format,
            time_zone=args.time_zone,
            user_locale=args.user_locale)
        if args.format == "json":
            return data
        if args.format == "csv":
            return render_records_csv(data["records"], fields)
        return render_records_text(data["records"], fields, more=bool(data.get("offset")))

    if cmd == "get-record":
        data = client.get_record(base_id, table, record_id_arg(args.record))
        if args.format == "text":
            return render_records_text([data])
        return data

    if cmd == "list-comments":
        data = client.list_comments(base_id, table, record_id_arg(args.record), fetch_all=args.all)
        if args.format == "json":
            return data
        return render_comments_text(data["comments"])

    if cmd == "create-records":
        records = gather_write_records(args)
        validate_fields(table_schema, records)
        return client.create_records(base_id, table, records, typecast=resolve_typecast(args))

    if cmd == "update-records":
        records = gather_write_records(args, want_ids=True)
        validate_fields(table_schema, [r["fields"] for r in records])
        return client.update_records(base_id, table, records,
                                     typecast=resolve_typecast(args),
                                     destructive=args.destructive)

    if cmd == "upsert-records":
        merge_on = split_list(args.merge_on)
        if not 1 <= len(merge_on) <= 3:
            raise ConfigurationError(
                "--merge-on takes 1 to 3 field names; Airtable allows no more than three.")
        records = gather_write_records(args)
        validate_fields(table_schema, records)
        for name in merge_on:
            for rec in records:
                if name not in rec:
                    raise ConfigurationError(
                        "Every record must carry the merge field {!r} -- it is what Airtable "
                        "matches on.".format(name))
        return client.upsert_records(base_id, table, records, merge_on,
                                     typecast=resolve_typecast(args))

    if cmd == "add-comment":
        text = resolve_text(args.text, args.text_file)
        if not text:
            raise ConfigurationError("add-comment requires --text or --text-file.")
        return client.add_comment(base_id, table, record_id_arg(args.record), text)

    raise ConfigurationError("Unknown command: {}".format(cmd))


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        client = AirtableClient()
        result = dispatch(client, args)
        if isinstance(result, str):
            if result:
                sys.stdout.write(result + "\n")
        else:
            output_json(result)
    except ReadOnlyError as e:
        sys.stderr.write("Read-Only Mode: {}\n".format(e))
        sys.exit(3)
    except ConfigurationError as e:
        sys.stderr.write("Configuration Error: {}\n".format(e))
        sys.exit(2)
    except ApiError as e:
        sys.stderr.write("{}\n".format(e))
        sys.exit(1)
    except TransportError as e:
        sys.stderr.write("Transport Error: {}\n".format(e))
        sys.exit(1)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
