#!/usr/bin/env python3
"""
TestRail API v2 CLI Tool

A self-contained command-line interface for TestRail's REST API v2. It covers
test-management CRUD (projects, suites, sections, cases, runs, tests, results,
plans, milestones, users, metadata) plus higher-level workflows -- most notably
`report-playwright`, which ingests a Playwright JSON report and pushes pass/fail
results into a TestRail run, mapping specs to cases by embedded case IDs.

This mirrors the surface of the `pw-testrail` MCP server as a plain Python CLI,
so it runs without an MCP server. Modelled on the sibling `bitbucket` and
`slack` skills.

HTTP transport: uses `curl` by default (capturing the status via
`-w "%{http_code}"`) and falls back to Python's stdlib `urllib` when curl is not
on PATH. No third-party libraries are required.

Environment variables required:
    TESTRAIL_URL     - Instance base URL, e.g. https://yourcompany.testrail.io
    TESTRAIL_USER    - TestRail account email
    TESTRAIL_API_KEY - API key (My Settings > API Keys) or account password

TestRail URL quirk: the API path is `<base>/index.php?/api/v2/<endpoint>`. Because
the `?` already sits before `/api/v2`, extra query parameters are appended with
`&` (not `?`). This script handles that for you.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


# --- Status maps -------------------------------------------------------------

# TestRail's built-in result statuses.
STATUS = {
    "passed": 1,
    "blocked": 2,
    "untested": 3,  # cannot be set via add_result; informational only
    "retest": 4,
    "failed": 5,
}
STATUS_NAME = {v: k for k, v in STATUS.items()}

# Playwright result status -> TestRail status name. "skipped" maps to None,
# meaning "do not report" unless the caller opts in via --skipped-status.
PLAYWRIGHT_STATUS_MAP = {
    "passed": "passed",
    "failed": "failed",
    "timedOut": "failed",
    "interrupted": "failed",
    "skipped": None,
}

PRIORITY = {1: "Low", 2: "Medium", 3: "High", 4: "Critical"}


# --- HTTP transport (curl by default, urllib fallback) -----------------------

class ApiError(Exception):
    """Raised when the TestRail API returns a non-2xx status."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class TransportError(Exception):
    """Raised when the request could not be sent at all (curl/urllib failure)."""


def _curl_cfg_quote(value):
    """Escape a value for use inside a double-quoted curl config entry."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _curl_request(method, url, headers, data, auth):
    """Perform a request with curl, returning (status_code, response_text).

    Credentials, headers, URL and body are passed via a temp config file (`-K`)
    and temp files so nothing sensitive leaks into the process argv. The HTTP
    status is captured with `-w "%{http_code}"`; a non-2xx status is returned to
    the caller rather than raised here.
    """
    username, token = auth
    cfg_fd, cfg_path = tempfile.mkstemp(prefix="tr-curl-", suffix=".cfg")
    out_fd, out_path = tempfile.mkstemp(prefix="tr-curl-", suffix=".out")
    os.close(out_fd)
    data_path = None
    try:
        cfg_lines = [
            'request = "{}"'.format(_curl_cfg_quote(method)),
            'url = "{}"'.format(_curl_cfg_quote(url)),
            'user = "{}"'.format(_curl_cfg_quote("{}:{}".format(username, token))),
            'output = "{}"'.format(_curl_cfg_quote(out_path)),
            'write-out = "%{http_code}"',
            "silent",
            "show-error",
        ]
        for key, value in headers.items():
            cfg_lines.append('header = "{}: {}"'.format(_curl_cfg_quote(key), _curl_cfg_quote(value)))

        if data is not None:
            data_fd, data_path = tempfile.mkstemp(prefix="tr-curl-", suffix=".dat")
            with os.fdopen(data_fd, "w", encoding="utf-8") as f:
                f.write(data)
            cfg_lines.append('data-binary = "@{}"'.format(_curl_cfg_quote(data_path)))

        with os.fdopen(cfg_fd, "w", encoding="utf-8") as f:
            f.write("\n".join(cfg_lines) + "\n")

        result = subprocess.run(["curl", "-K", cfg_path], capture_output=True, text=True)
        if result.returncode != 0:
            raise TransportError(
                "curl request failed: {}".format(
                    result.stderr.strip() or "exit code {}".format(result.returncode)
                )
            )

        status = int(result.stdout.strip() or "0")
        with open(out_path, "r", encoding="utf-8") as f:
            text = f.read()
        return status, text
    finally:
        for path in (cfg_path, out_path, data_path):
            if path and os.path.exists(path):
                os.unlink(path)


def _urllib_request(method, url, headers, data, auth):
    """Perform a request with stdlib urllib, returning (status_code, response_text)."""
    username, token = auth
    req_headers = dict(headers)
    creds = base64.b64encode("{}:{}".format(username, token).encode("utf-8")).decode("ascii")
    req_headers["Authorization"] = "Basic {}".format(creds)

    body = data.encode("utf-8") if isinstance(data, str) else data
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.getcode(), resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise TransportError("urllib request failed: {}".format(e.reason))


def http_request(method, url, headers=None, data=None, auth=None):
    """Send an HTTP request via curl (default) or urllib (fallback).

    Returns (status_code, response_text). Does not raise on HTTP error status
    codes -- the caller inspects status_code. Raises TransportError when the
    request cannot be delivered at all.
    """
    headers = dict(headers or {})
    if shutil.which("curl"):
        return _curl_request(method, url, headers, data, auth)
    return _urllib_request(method, url, headers, data, auth)


# --- TestRail client ---------------------------------------------------------

class TestRailClient:
    """Client for TestRail's REST API v2."""

    def __init__(self):
        self.base = (os.environ.get("TESTRAIL_URL") or "").rstrip("/")
        self.user = os.environ.get("TESTRAIL_USER")
        self.api_key = os.environ.get("TESTRAIL_API_KEY")

        missing = [
            name
            for name, val in (
                ("TESTRAIL_URL", self.base),
                ("TESTRAIL_USER", self.user),
                ("TESTRAIL_API_KEY", self.api_key),
            )
            if not val
        ]
        if missing:
            raise EnvironmentError(
                "Missing required environment variables: {}".format(", ".join(missing))
            )

    @property
    def _auth(self):
        return (self.user, self.api_key)

    def _url(self, endpoint):
        # endpoint is everything after `/api/v2/`, e.g. "get_cases/1&suite_id=2"
        return "{}/index.php?/api/v2/{}".format(self.base, endpoint)

    def _error_message(self, status, text):
        msg = "TestRail API error {}".format(status)
        try:
            data = json.loads(text)
            if isinstance(data, dict) and data.get("error"):
                msg += ": {}".format(data["error"])
            elif text:
                msg += ": {}".format(text.strip())
        except (json.JSONDecodeError, AttributeError):
            if text:
                msg += ": {}".format(text.strip())
        if status == 401:
            msg += (
                " (check TESTRAIL_USER / TESTRAIL_API_KEY; regenerate the key under "
                "My Settings > API Keys, and confirm the API is enabled in "
                "Administration > Site Settings > API)"
            )
        return msg

    def request(self, method, endpoint, json_body=None):
        """Make a JSON API request and return the parsed body (or {} for 204)."""
        url = self._url(endpoint)
        headers = {"Accept": "application/json"}
        data = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body)

        status, text = http_request(method, url, headers=headers, data=data, auth=self._auth)
        if not (200 <= status < 300):
            raise ApiError(self._error_message(status, text), status)
        if status == 204 or not text:
            return {}
        return json.loads(text)

    def get_list(self, endpoint, entity_key, limit=None, params=None):
        """GET a list endpoint, transparently handling both response shapes.

        TestRail >= 6.7 wraps lists in {offset, limit, size, _links, <key>: [...]}
        and exposes `_links.next` for pagination; older versions return a bare
        array. This follows `_links.next` until exhausted (or `limit` is hit).
        """
        params = dict(params or {})
        suffix = "&".join("{}={}".format(k, urllib.parse.quote(str(v), safe=""))
                          for k, v in params.items() if v is not None)
        ep = endpoint + ("&" + suffix if suffix else "")

        collected = []
        while ep is not None:
            body = self.request("GET", ep)
            if isinstance(body, list):
                collected.extend(body)
                break  # old API: no pagination envelope
            collected.extend(body.get(entity_key, []))
            next_link = (body.get("_links") or {}).get("next")
            if not next_link:
                break
            # next_link looks like "/api/v2/get_cases/1&limit=250&offset=250"
            ep = next_link.split("/api/v2/", 1)[-1]
            if limit and len(collected) >= limit:
                break

        return collected[:limit] if limit else collected

    # --- Projects ---
    def get_projects(self, is_completed=None):
        params = {}
        if is_completed is not None:
            params["is_completed"] = 1 if is_completed else 0
        return self.get_list("get_projects", "projects", params=params)

    def get_project(self, project_id):
        return self.request("GET", "get_project/{}".format(project_id))

    # --- Suites ---
    def get_suites(self, project_id):
        return self.get_list("get_suites/{}".format(project_id), "suites")

    def get_suite(self, suite_id):
        return self.request("GET", "get_suite/{}".format(suite_id))

    def add_suite(self, project_id, name, description=None):
        body = {"name": name}
        if description is not None:
            body["description"] = description
        return self.request("POST", "add_suite/{}".format(project_id), body)

    # --- Sections ---
    def get_sections(self, project_id, suite_id=None):
        params = {"suite_id": suite_id} if suite_id else {}
        return self.get_list("get_sections/{}".format(project_id), "sections", params=params)

    def add_section(self, project_id, name, suite_id=None, parent_id=None, description=None):
        body = {"name": name}
        if suite_id is not None:
            body["suite_id"] = suite_id
        if parent_id is not None:
            body["parent_id"] = parent_id
        if description is not None:
            body["description"] = description
        return self.request("POST", "add_section/{}".format(project_id), body)

    # --- Cases ---
    def get_cases(self, project_id, suite_id=None, section_id=None, limit=None,
                  offset=None, filter_text=None):
        params = {
            "suite_id": suite_id,
            "section_id": section_id,
            "offset": offset,
            "filter": filter_text,
        }
        # TestRail caps a page at 250; pass through if caller wants fewer.
        if limit:
            params["limit"] = min(limit, 250)
        return self.get_list("get_cases/{}".format(project_id), "cases", limit=limit, params=params)

    def get_case(self, case_id):
        return self.request("GET", "get_case/{}".format(case_id))

    def add_case(self, section_id, payload):
        return self.request("POST", "add_case/{}".format(section_id), payload)

    def update_case(self, case_id, payload):
        return self.request("POST", "update_case/{}".format(case_id), payload)

    def update_cases(self, suite_id, case_ids, payload):
        body = dict(payload)
        body["case_ids"] = case_ids
        return self.request("POST", "update_cases/{}".format(suite_id), body)

    def delete_case(self, case_id):
        return self.request("POST", "delete_case/{}".format(case_id))

    # --- Runs ---
    def get_runs(self, project_id, limit=None, is_completed=None):
        params = {}
        if is_completed is not None:
            params["is_completed"] = 1 if is_completed else 0
        return self.get_list("get_runs/{}".format(project_id), "runs", limit=limit, params=params)

    def get_run(self, run_id):
        return self.request("GET", "get_run/{}".format(run_id))

    def add_run(self, project_id, payload):
        return self.request("POST", "add_run/{}".format(project_id), payload)

    def update_run(self, run_id, payload):
        return self.request("POST", "update_run/{}".format(run_id), payload)

    def close_run(self, run_id):
        return self.request("POST", "close_run/{}".format(run_id))

    def delete_run(self, run_id):
        return self.request("POST", "delete_run/{}".format(run_id))

    # --- Tests ---
    def get_tests(self, run_id, limit=None):
        return self.get_list("get_tests/{}".format(run_id), "tests", limit=limit)

    def get_test(self, test_id):
        return self.request("GET", "get_test/{}".format(test_id))

    # --- Results ---
    def add_result(self, test_id, payload):
        return self.request("POST", "add_result/{}".format(test_id), payload)

    def add_result_for_case(self, run_id, case_id, payload):
        return self.request("POST", "add_result_for_case/{}/{}".format(run_id, case_id), payload)

    def add_results_for_cases(self, run_id, results):
        return self.request("POST", "add_results_for_cases/{}".format(run_id),
                            {"results": results})

    def get_results(self, test_id, limit=None):
        return self.get_list("get_results/{}".format(test_id), "results", limit=limit)

    def get_results_for_case(self, run_id, case_id, limit=None):
        return self.get_list("get_results_for_case/{}/{}".format(run_id, case_id),
                            "results", limit=limit)

    def get_results_for_run(self, run_id, limit=None):
        return self.get_list("get_results_for_run/{}".format(run_id), "results", limit=limit)

    # --- Plans ---
    def get_plans(self, project_id, limit=None):
        return self.get_list("get_plans/{}".format(project_id), "plans", limit=limit)

    def get_plan(self, plan_id):
        return self.request("GET", "get_plan/{}".format(plan_id))

    def add_plan(self, project_id, payload):
        return self.request("POST", "add_plan/{}".format(project_id), payload)

    def add_plan_entry(self, plan_id, payload):
        return self.request("POST", "add_plan_entry/{}".format(plan_id), payload)

    def close_plan(self, plan_id):
        return self.request("POST", "close_plan/{}".format(plan_id))

    # --- Milestones ---
    def get_milestones(self, project_id, limit=None):
        return self.get_list("get_milestones/{}".format(project_id), "milestones", limit=limit)

    def get_milestone(self, milestone_id):
        return self.request("GET", "get_milestone/{}".format(milestone_id))

    def add_milestone(self, project_id, payload):
        return self.request("POST", "add_milestone/{}".format(project_id), payload)

    # --- Users ---
    def get_users(self, project_id=None):
        ep = "get_users"
        if project_id:
            ep += "/{}".format(project_id)
        return self.get_list(ep, "users")

    def get_user(self, user_id):
        return self.request("GET", "get_user/{}".format(user_id))

    def get_user_by_email(self, email):
        return self.request("GET", "get_user_by_email&email={}".format(
            urllib.parse.quote(email, safe="")))

    # --- Metadata ---
    def get_statuses(self):
        return self.get_list("get_statuses", "statuses")

    def get_priorities(self):
        return self.get_list("get_priorities", "priorities")

    def get_case_types(self):
        return self.get_list("get_case_types", "case_types")

    def get_templates(self, project_id):
        return self.get_list("get_templates/{}".format(project_id), "templates")


# --- Helpers -----------------------------------------------------------------

def output_json(data):
    print(json.dumps(data, indent=2, default=str))


CASE_ID_RE = re.compile(r"\bC(\d+)\b")


def parse_case_ids(*texts):
    """Extract TestRail case IDs (e.g. C1234) from any of the given strings.

    Returns a de-duplicated list of ints in first-seen order.
    """
    seen = []
    for text in texts:
        if not text:
            continue
        for match in CASE_ID_RE.findall(text):
            cid = int(match)
            if cid not in seen:
                seen.append(cid)
    return seen


def elapsed_str(seconds):
    """Format seconds into a TestRail-friendly elapsed string (e.g. "1m 5s").

    TestRail rejects "0s", so anything under 1s becomes "1s"; returns None when
    there was genuinely no duration to record.
    """
    if seconds is None:
        return None
    total = int(round(seconds))
    if total <= 0:
        return "1s" if seconds and seconds > 0 else None
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append("{}h".format(hours))
    if minutes:
        parts.append("{}m".format(minutes))
    if secs:
        parts.append("{}s".format(secs))
    return " ".join(parts) if parts else "1s"


def parse_steps(steps_json):
    """Parse a --steps JSON string into custom_steps_separated.

    Accepts either a JSON array of {content, expected} objects or a JSON array
    of plain strings (treated as step content with no expected result).
    """
    data = json.loads(steps_json)
    if not isinstance(data, list):
        raise ValueError("--steps must be a JSON array")
    steps = []
    for item in data:
        if isinstance(item, str):
            steps.append({"content": item, "expected": ""})
        elif isinstance(item, dict):
            steps.append({
                "content": item.get("content", ""),
                "expected": item.get("expected", ""),
            })
        else:
            raise ValueError("each step must be a string or {content, expected} object")
    return steps


# --- Playwright report parsing ----------------------------------------------

def _walk_specs(suite, ancestry, out):
    """Recursively collect specs from a Playwright JSON suite tree."""
    title = suite.get("title", "")
    next_ancestry = ancestry + ([title] if title else [])
    for spec in suite.get("specs", []) or []:
        out.append((next_ancestry, spec))
    for child in suite.get("suites", []) or []:
        _walk_specs(child, next_ancestry, out)


# Severity order for picking a spec's overall status from several outcomes.
_STATUS_PRECEDENCE = ["passed", "skipped", "interrupted", "timedOut", "failed"]


def parse_playwright_report(report, include_skipped=False, skipped_status="blocked",
                            flaky_status="passed"):
    """Turn a Playwright JSON report into a list of TestRail result dicts.

    Returns (results, unmapped) where:
      - results: [{case_id, status_id, comment, elapsed, _spec, _pw_status, _flaky}]
      - unmapped: [{title, status}] for specs with no resolvable case ID.

    Mapping rules, chosen to match how Playwright suites are usually annotated:

    * Case IDs are TestRail IDs written as `C1234` in titles. A spec's own title
      wins; only if the spec carries no ID do we fall back to the enclosing
      describe titles. This avoids a describe-level ID being stamped onto every
      child spec (which would post several conflicting results to one case).
    * A test's outcome is its FINAL retry, not the worst attempt -- a test that
      fails then passes is flaky-but-passing in Playwright, so we report it as
      passing (configurable via flaky_status) and note the flakiness in the
      comment, rather than reporting a misleading failure.
    * A spec with several tests (projects / parametrization) takes the most
      severe final status across them.
    """
    pairs = []
    for suite in report.get("suites", []) or []:
        _walk_specs(suite, [], pairs)

    results = []
    unmapped = []
    for ancestry, spec in pairs:
        spec_title = spec.get("title", "")
        # Prefer the spec's own marker; fall back to the describe chain.
        case_ids = parse_case_ids(spec_title) or parse_case_ids(*ancestry)

        pw_status, duration_ms, error_msgs, flaky = _aggregate_spec(spec)
        full_title = " > ".join(ancestry + [spec_title]).strip()

        if not case_ids:
            unmapped.append({"title": full_title, "status": pw_status})
            continue

        tr_status_name = PLAYWRIGHT_STATUS_MAP.get(pw_status, "failed")
        if tr_status_name is None:  # skipped
            if not include_skipped:
                continue
            tr_status_name = skipped_status
        elif flaky and tr_status_name == "passed":
            tr_status_name = flaky_status

        status_id = STATUS.get(tr_status_name, STATUS["failed"])
        comment = _build_comment(pw_status, error_msgs, flaky)
        elapsed = elapsed_str(duration_ms / 1000.0 if duration_ms else None)

        for cid in case_ids:
            entry = {"case_id": cid, "status_id": status_id}
            if comment:
                entry["comment"] = comment
            if elapsed:
                entry["elapsed"] = elapsed
            entry["_spec"] = full_title
            entry["_pw_status"] = pw_status
            entry["_flaky"] = flaky
            results.append(entry)

    return results, unmapped


def _aggregate_spec(spec):
    """Return (status, total_duration_ms, [error messages], flaky) for a spec.

    `status` is the most severe FINAL-retry status across the spec's tests.
    `flaky` is true if any test ultimately passed only after an earlier failure.
    """
    worst = "passed"
    total_ms = 0
    errors = []
    flaky = False
    any_test = False

    for test in spec.get("tests", []) or []:
        attempts = test.get("results", []) or []
        if not attempts:
            continue
        any_test = True
        for res in attempts:
            total_ms += res.get("duration", 0) or 0

        final_status = attempts[-1].get("status", "failed")
        earlier_failed = any(a.get("status") not in ("passed", "skipped")
                             for a in attempts[:-1])
        if final_status == "passed" and earlier_failed:
            flaky = True
        # Collect error messages only when the test ultimately did not pass.
        if final_status != "passed":
            for a in attempts:
                err = a.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else None
                if msg:
                    errors.append(msg)

        rank = (_STATUS_PRECEDENCE.index(final_status)
                if final_status in _STATUS_PRECEDENCE else len(_STATUS_PRECEDENCE))
        if rank > _STATUS_PRECEDENCE.index(worst):
            worst = final_status if final_status in _STATUS_PRECEDENCE else "failed"

    if not any_test:
        return "skipped", 0, errors, False
    return worst, total_ms, errors, flaky


def _build_comment(pw_status, error_msgs, flaky=False):
    """Build a result comment from the Playwright status and any error text."""
    header = "Playwright: {}{}".format(pw_status, " (flaky)" if flaky else "")
    if error_msgs:
        # ANSI escapes are noise in TestRail; strip them. De-dup repeated errors.
        cleaned, seen = [], set()
        for m in error_msgs:
            c = re.sub(r"\x1b\[[0-9;]*m", "", m).strip()
            if c and c not in seen:
                seen.add(c)
                cleaned.append(c)
        body = "\n\n".join(cleaned)
        if len(body) > 6000:  # TestRail accepts long text; trim runaway logs.
            body = body[:6000] + "\n... (truncated)"
        return header + "\n\n" + body
    return header


# --- Command handlers --------------------------------------------------------

def cmd_report_playwright(client, args):
    """Parse a Playwright JSON report and push results into a TestRail run."""
    with open(args.report, "r", encoding="utf-8") as f:
        report = json.load(f)

    results, unmapped = parse_playwright_report(
        report,
        include_skipped=args.include_skipped,
        skipped_status=args.skipped_status,
        flaky_status=args.flaky_status,
    )

    # Strip our internal annotation keys before sending to the API.
    def clean(r):
        return {k: v for k, v in r.items() if not k.startswith("_")}

    case_ids = sorted({r["case_id"] for r in results})

    summary = {
        "mapped_results": len(results),
        "distinct_cases": len(case_ids),
        "unmapped_specs": len(unmapped),
    }

    if args.dry_run:
        output_json({
            "dry_run": True,
            "summary": summary,
            "case_ids": case_ids,
            "results": [dict(r) for r in results],  # keep annotations for visibility
            "unmapped": unmapped,
        })
        return

    if not results:
        output_json({
            "status": "no_results",
            "message": "No specs mapped to TestRail case IDs (looked for C#### in titles).",
            "summary": summary,
            "unmapped": unmapped,
        })
        return

    # Resolve the run: use --run-id, or create a fresh run scoped to the cases.
    run_id = args.run_id
    created_run = None
    if not run_id:
        if not args.project_id:
            raise ApiError("Provide --run-id, or --project-id (+ optional --run-name) to create a run.")
        run_payload = {
            "name": args.run_name or "Automated run (Playwright)",
            "include_all": False,
            "case_ids": case_ids,
        }
        if args.suite_id:
            run_payload["suite_id"] = args.suite_id
        if args.milestone_id:
            run_payload["milestone_id"] = args.milestone_id
        if args.description:
            run_payload["description"] = args.description
        created_run = client.add_run(args.project_id, run_payload)
        run_id = created_run["id"]

    posted = client.add_results_for_cases(run_id, [clean(r) for r in results])

    if args.close_run:
        client.close_run(run_id)

    output_json({
        "status": "reported",
        "run_id": run_id,
        "run_url": (created_run or client.get_run(run_id)).get("url"),
        "created_run": bool(created_run),
        "closed_run": bool(args.close_run),
        "summary": summary,
        "results_posted": len(posted) if isinstance(posted, list) else None,
        "unmapped": unmapped,
    })


def cmd_test_auth(client, args):
    """Verify credentials by hitting a cheap authenticated endpoint."""
    projects = client.get_projects()
    output_json({
        "status": "ok",
        "user": client.user,
        "url": client.base,
        "projects_visible": len(projects),
    })


# --- Argument parsing --------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="TestRail API v2 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # --- auth / metadata ---
    sub.add_parser("test-auth", help="Verify TESTRAIL_* credentials work")
    sub.add_parser("list-statuses", help="List result statuses (id -> label)")
    sub.add_parser("list-priorities", help="List case priorities")
    sub.add_parser("list-case-types", help="List case types")
    p = sub.add_parser("list-templates", help="List field templates for a project")
    p.add_argument("--project-id", "-p", required=True, type=int)

    # --- projects ---
    p = sub.add_parser("list-projects", help="List all projects")
    p.add_argument("--completed", dest="is_completed", action="store_const", const=True, default=None,
                   help="Only completed projects")
    p.add_argument("--active", dest="is_completed", action="store_const", const=False,
                   help="Only active projects")
    p = sub.add_parser("get-project", help="Get a project by ID")
    p.add_argument("--project-id", "-p", required=True, type=int)

    # --- suites ---
    p = sub.add_parser("list-suites", help="List suites in a project")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p = sub.add_parser("get-suite", help="Get a suite by ID")
    p.add_argument("--suite-id", "-s", required=True, type=int)
    p = sub.add_parser("add-suite", help="Create a suite")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--name", "-n", required=True)
    p.add_argument("--description", "-d", default=None)

    # --- sections ---
    p = sub.add_parser("list-sections", help="List sections in a project (optionally a suite)")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--suite-id", "-s", type=int, default=None)
    p = sub.add_parser("add-section", help="Create a section")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--name", "-n", required=True)
    p.add_argument("--suite-id", "-s", type=int, default=None)
    p.add_argument("--parent-id", type=int, default=None)
    p.add_argument("--description", "-d", default=None)

    # --- cases ---
    p = sub.add_parser("list-cases", help="List/search cases in a project")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--suite-id", "-s", type=int, default=None,
                   help="Required for multi-suite projects")
    p.add_argument("--section-id", type=int, default=None)
    p.add_argument("--limit", "-l", type=int, default=None)
    p.add_argument("--offset", type=int, default=None)
    p.add_argument("--filter", "-f", dest="filter_text", default=None,
                   help="Substring filter on case titles")
    p = sub.add_parser("get-case", help="Get a case by ID")
    p.add_argument("--case-id", "-c", required=True, type=int)

    p = sub.add_parser("add-case", help="Create a test case in a section")
    p.add_argument("--section-id", required=True, type=int)
    p.add_argument("--title", "-t", required=True)
    p.add_argument("--template-id", type=int, default=None,
                   help="Template ID (2 = Test Case (Steps))")
    p.add_argument("--type-id", type=int, default=None)
    p.add_argument("--priority-id", type=int, default=None,
                   help="1=Low 2=Medium 3=High 4=Critical")
    p.add_argument("--refs", default=None, help="Linked references (e.g. JIRA-123)")
    p.add_argument("--preconds", dest="custom_preconds", default=None)
    p.add_argument("--steps", default=None,
                   help='JSON array of {content,expected} objects or plain strings')

    p = sub.add_parser("update-case", help="Update an existing case")
    p.add_argument("--case-id", "-c", required=True, type=int)
    p.add_argument("--title", "-t", default=None)
    p.add_argument("--priority-id", type=int, default=None)
    p.add_argument("--refs", default=None)
    p.add_argument("--preconds", dest="custom_preconds", default=None)
    p.add_argument("--steps", default=None,
                   help='JSON array of {content,expected} objects or plain strings')

    p = sub.add_parser("update-cases", help="Bulk-update multiple cases in a suite")
    p.add_argument("--suite-id", "-s", required=True, type=int)
    p.add_argument("--case-ids", required=True, help="Comma-separated case IDs")
    p.add_argument("--priority-id", type=int, default=None)
    p.add_argument("--refs", default=None)

    p = sub.add_parser("delete-case", help="Delete a case (irreversible)")
    p.add_argument("--case-id", "-c", required=True, type=int)

    # --- runs ---
    p = sub.add_parser("list-runs", help="List runs in a project")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--limit", "-l", type=int, default=None)
    p.add_argument("--completed", dest="is_completed", action="store_const", const=True, default=None)
    p.add_argument("--active", dest="is_completed", action="store_const", const=False)
    p = sub.add_parser("get-run", help="Get a run by ID")
    p.add_argument("--run-id", "-r", required=True, type=int)

    p = sub.add_parser("add-run", help="Create a test run")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--name", "-n", required=True)
    p.add_argument("--suite-id", "-s", type=int, default=None)
    p.add_argument("--description", "-d", default=None)
    p.add_argument("--milestone-id", type=int, default=None)
    p.add_argument("--assignedto-id", type=int, default=None)
    p.add_argument("--case-ids", default=None,
                   help="Comma-separated case IDs (sets include_all=false)")
    p.add_argument("--include-all", action="store_true",
                   help="Include all cases (default when --case-ids omitted)")

    p = sub.add_parser("update-run", help="Update a run (name/description/cases)")
    p.add_argument("--run-id", "-r", required=True, type=int)
    p.add_argument("--name", "-n", default=None)
    p.add_argument("--description", "-d", default=None)
    p.add_argument("--case-ids", default=None, help="Comma-separated case IDs")

    p = sub.add_parser("close-run", help="Close a run (archives results; irreversible)")
    p.add_argument("--run-id", "-r", required=True, type=int)
    p = sub.add_parser("delete-run", help="Delete a run (irreversible)")
    p.add_argument("--run-id", "-r", required=True, type=int)

    # --- tests ---
    p = sub.add_parser("list-tests", help="List tests (cases) in a run")
    p.add_argument("--run-id", "-r", required=True, type=int)
    p.add_argument("--limit", "-l", type=int, default=None)
    p = sub.add_parser("get-test", help="Get a test by ID")
    p.add_argument("--test-id", "-t", required=True, type=int)

    # --- results ---
    p = sub.add_parser("add-result", help="Add a result for a test (by test_id)")
    p.add_argument("--test-id", "-t", required=True, type=int)
    _add_result_args(p)
    p = sub.add_parser("add-result-for-case", help="Add a result for a case in a run")
    p.add_argument("--run-id", "-r", required=True, type=int)
    p.add_argument("--case-id", "-c", required=True, type=int)
    _add_result_args(p)

    p = sub.add_parser("add-results", help="Bulk add results for cases in a run (from JSON)")
    p.add_argument("--run-id", "-r", required=True, type=int)
    p.add_argument("--results-file", "-F", required=True,
                   help='JSON file: array of {case_id,status_id,comment,elapsed,...} or {"results":[...]} ("-" = stdin)')

    p = sub.add_parser("get-results", help="Get results for a test (by test_id)")
    p.add_argument("--test-id", "-t", required=True, type=int)
    p.add_argument("--limit", "-l", type=int, default=None)
    p = sub.add_parser("get-results-for-case", help="Get results for a case in a run")
    p.add_argument("--run-id", "-r", required=True, type=int)
    p.add_argument("--case-id", "-c", required=True, type=int)
    p.add_argument("--limit", "-l", type=int, default=None)
    p = sub.add_parser("get-results-for-run", help="Get all results in a run")
    p.add_argument("--run-id", "-r", required=True, type=int)
    p.add_argument("--limit", "-l", type=int, default=None)

    # --- plans ---
    p = sub.add_parser("list-plans", help="List test plans in a project")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--limit", "-l", type=int, default=None)
    p = sub.add_parser("get-plan", help="Get a plan by ID")
    p.add_argument("--plan-id", required=True, type=int)
    p = sub.add_parser("add-plan", help="Create a test plan")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--name", "-n", required=True)
    p.add_argument("--description", "-d", default=None)
    p.add_argument("--milestone-id", type=int, default=None)
    p = sub.add_parser("add-plan-entry", help="Add a run (entry) to a plan")
    p.add_argument("--plan-id", required=True, type=int)
    p.add_argument("--suite-id", "-s", required=True, type=int)
    p.add_argument("--name", "-n", default=None)
    p.add_argument("--case-ids", default=None, help="Comma-separated case IDs")
    p = sub.add_parser("close-plan", help="Close a plan (irreversible)")
    p.add_argument("--plan-id", required=True, type=int)

    # --- milestones ---
    p = sub.add_parser("list-milestones", help="List milestones in a project")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--limit", "-l", type=int, default=None)
    p = sub.add_parser("get-milestone", help="Get a milestone by ID")
    p.add_argument("--milestone-id", required=True, type=int)
    p = sub.add_parser("add-milestone", help="Create a milestone")
    p.add_argument("--project-id", "-p", required=True, type=int)
    p.add_argument("--name", "-n", required=True)
    p.add_argument("--description", "-d", default=None)
    p.add_argument("--due-on", type=int, default=None, help="Unix timestamp")

    # --- users ---
    p = sub.add_parser("list-users", help="List users")
    p.add_argument("--project-id", "-p", type=int, default=None)
    p = sub.add_parser("get-user", help="Get a user by ID")
    p.add_argument("--user-id", "-u", required=True, type=int)
    p = sub.add_parser("get-user-by-email", help="Look up a user by email")
    p.add_argument("--email", "-e", required=True)

    # --- workflows ---
    p = sub.add_parser("report-playwright",
                       help="Parse a Playwright JSON report and push results to a run")
    p.add_argument("--report", "-R", required=True, help="Path to Playwright JSON report")
    p.add_argument("--run-id", "-r", type=int, default=None,
                   help="Existing run to report into (else a run is created)")
    p.add_argument("--project-id", "-p", type=int, default=None,
                   help="Project to create a run in (when --run-id omitted)")
    p.add_argument("--suite-id", "-s", type=int, default=None)
    p.add_argument("--run-name", "-n", default=None)
    p.add_argument("--description", "-d", default=None)
    p.add_argument("--milestone-id", type=int, default=None)
    p.add_argument("--include-skipped", action="store_true",
                   help="Report skipped specs (default: omit them)")
    p.add_argument("--skipped-status", default="blocked",
                   choices=list(STATUS.keys()),
                   help="TestRail status for skipped specs when --include-skipped (default: blocked)")
    p.add_argument("--flaky-status", default="passed",
                   choices=list(STATUS.keys()),
                   help="TestRail status for flaky-but-passing specs (default: passed; use 'retest' to flag them)")
    p.add_argument("--close-run", action="store_true",
                   help="Close the run after reporting")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the mapping/results without calling the API")

    return parser


def _add_result_args(p):
    p.add_argument("--status", default=None, choices=list(STATUS.keys()),
                   help="Status name (passed/blocked/retest/failed)")
    p.add_argument("--status-id", type=int, default=None, help="Numeric status ID")
    p.add_argument("--comment", "-m", default=None)
    p.add_argument("--elapsed", default=None, help='e.g. "30s", "1m 45s"')
    p.add_argument("--defects", default=None, help="Comma-separated defect IDs")
    p.add_argument("--version", default=None)


def _resolve_status_id(args):
    if args.status_id is not None:
        return args.status_id
    if args.status is not None:
        return STATUS[args.status]
    raise ApiError("Provide --status (name) or --status-id (number).")


def _result_payload(args):
    payload = {"status_id": _resolve_status_id(args)}
    if args.comment is not None:
        payload["comment"] = args.comment
    if args.elapsed is not None:
        payload["elapsed"] = args.elapsed
    if args.defects is not None:
        payload["defects"] = args.defects
    if args.version is not None:
        payload["version"] = args.version
    return payload


def _csv_ints(value):
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def _read_file_arg(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# --- Dispatch ----------------------------------------------------------------

def dispatch(client, args):
    cmd = args.command

    if cmd == "test-auth":
        return cmd_test_auth(client, args)
    if cmd == "list-statuses":
        return output_json(client.get_statuses())
    if cmd == "list-priorities":
        return output_json(client.get_priorities())
    if cmd == "list-case-types":
        return output_json(client.get_case_types())
    if cmd == "list-templates":
        return output_json(client.get_templates(args.project_id))

    if cmd == "list-projects":
        return output_json(client.get_projects(args.is_completed))
    if cmd == "get-project":
        return output_json(client.get_project(args.project_id))

    if cmd == "list-suites":
        return output_json(client.get_suites(args.project_id))
    if cmd == "get-suite":
        return output_json(client.get_suite(args.suite_id))
    if cmd == "add-suite":
        return output_json(client.add_suite(args.project_id, args.name, args.description))

    if cmd == "list-sections":
        return output_json(client.get_sections(args.project_id, args.suite_id))
    if cmd == "add-section":
        return output_json(client.add_section(args.project_id, args.name, args.suite_id,
                                              args.parent_id, args.description))

    if cmd == "list-cases":
        return output_json(client.get_cases(args.project_id, args.suite_id, args.section_id,
                                            args.limit, args.offset, args.filter_text))
    if cmd == "get-case":
        return output_json(client.get_case(args.case_id))
    if cmd == "add-case":
        payload = {"title": args.title}
        if args.template_id is not None:
            payload["template_id"] = args.template_id
        if args.type_id is not None:
            payload["type_id"] = args.type_id
        if args.priority_id is not None:
            payload["priority_id"] = args.priority_id
        if args.refs is not None:
            payload["refs"] = args.refs
        if args.custom_preconds is not None:
            payload["custom_preconds"] = args.custom_preconds
        if args.steps is not None:
            payload["custom_steps_separated"] = parse_steps(args.steps)
        return output_json(client.add_case(args.section_id, payload))
    if cmd == "update-case":
        payload = {}
        if args.title is not None:
            payload["title"] = args.title
        if args.priority_id is not None:
            payload["priority_id"] = args.priority_id
        if args.refs is not None:
            payload["refs"] = args.refs
        if args.custom_preconds is not None:
            payload["custom_preconds"] = args.custom_preconds
        if args.steps is not None:
            payload["custom_steps_separated"] = parse_steps(args.steps)
        if not payload:
            raise ApiError("Nothing to update; pass at least one field.")
        return output_json(client.update_case(args.case_id, payload))
    if cmd == "update-cases":
        payload = {}
        if args.priority_id is not None:
            payload["priority_id"] = args.priority_id
        if args.refs is not None:
            payload["refs"] = args.refs
        if not payload:
            raise ApiError("Nothing to update; pass at least one field.")
        return output_json(client.update_cases(args.suite_id, _csv_ints(args.case_ids), payload))
    if cmd == "delete-case":
        client.delete_case(args.case_id)
        return output_json({"status": "deleted", "case_id": args.case_id})

    if cmd == "list-runs":
        return output_json(client.get_runs(args.project_id, args.limit, args.is_completed))
    if cmd == "get-run":
        return output_json(client.get_run(args.run_id))
    if cmd == "add-run":
        payload = {"name": args.name}
        if args.suite_id is not None:
            payload["suite_id"] = args.suite_id
        if args.description is not None:
            payload["description"] = args.description
        if args.milestone_id is not None:
            payload["milestone_id"] = args.milestone_id
        if args.assignedto_id is not None:
            payload["assignedto_id"] = args.assignedto_id
        if args.case_ids:
            payload["include_all"] = False
            payload["case_ids"] = _csv_ints(args.case_ids)
        else:
            payload["include_all"] = True
        return output_json(client.add_run(args.project_id, payload))
    if cmd == "update-run":
        payload = {}
        if args.name is not None:
            payload["name"] = args.name
        if args.description is not None:
            payload["description"] = args.description
        if args.case_ids is not None:
            payload["include_all"] = False
            payload["case_ids"] = _csv_ints(args.case_ids)
        if not payload:
            raise ApiError("Nothing to update; pass at least one field.")
        return output_json(client.update_run(args.run_id, payload))
    if cmd == "close-run":
        return output_json(client.close_run(args.run_id))
    if cmd == "delete-run":
        client.delete_run(args.run_id)
        return output_json({"status": "deleted", "run_id": args.run_id})

    if cmd == "list-tests":
        return output_json(client.get_tests(args.run_id, args.limit))
    if cmd == "get-test":
        return output_json(client.get_test(args.test_id))

    if cmd == "add-result":
        return output_json(client.add_result(args.test_id, _result_payload(args)))
    if cmd == "add-result-for-case":
        return output_json(client.add_result_for_case(args.run_id, args.case_id, _result_payload(args)))
    if cmd == "add-results":
        raw = json.loads(_read_file_arg(args.results_file))
        results = raw.get("results") if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            raise ApiError('--results-file must be a JSON array or {"results": [...]}.')
        return output_json(client.add_results_for_cases(args.run_id, results))
    if cmd == "get-results":
        return output_json(client.get_results(args.test_id, args.limit))
    if cmd == "get-results-for-case":
        return output_json(client.get_results_for_case(args.run_id, args.case_id, args.limit))
    if cmd == "get-results-for-run":
        return output_json(client.get_results_for_run(args.run_id, args.limit))

    if cmd == "list-plans":
        return output_json(client.get_plans(args.project_id, args.limit))
    if cmd == "get-plan":
        return output_json(client.get_plan(args.plan_id))
    if cmd == "add-plan":
        payload = {"name": args.name}
        if args.description is not None:
            payload["description"] = args.description
        if args.milestone_id is not None:
            payload["milestone_id"] = args.milestone_id
        return output_json(client.add_plan(args.project_id, payload))
    if cmd == "add-plan-entry":
        payload = {"suite_id": args.suite_id}
        if args.name is not None:
            payload["name"] = args.name
        if args.case_ids:
            payload["include_all"] = False
            payload["case_ids"] = _csv_ints(args.case_ids)
        return output_json(client.add_plan_entry(args.plan_id, payload))
    if cmd == "close-plan":
        return output_json(client.close_plan(args.plan_id))

    if cmd == "list-milestones":
        return output_json(client.get_milestones(args.project_id, args.limit))
    if cmd == "get-milestone":
        return output_json(client.get_milestone(args.milestone_id))
    if cmd == "add-milestone":
        payload = {"name": args.name}
        if args.description is not None:
            payload["description"] = args.description
        if args.due_on is not None:
            payload["due_on"] = args.due_on
        return output_json(client.add_milestone(args.project_id, payload))

    if cmd == "list-users":
        return output_json(client.get_users(args.project_id))
    if cmd == "get-user":
        return output_json(client.get_user(args.user_id))
    if cmd == "get-user-by-email":
        return output_json(client.get_user_by_email(args.email))

    if cmd == "report-playwright":
        return cmd_report_playwright(client, args)

    raise ApiError("Unknown command: {}".format(cmd))


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # `report-playwright --dry-run` only parses the report locally, so it
    # shouldn't demand credentials -- handy for validating spec->case mapping
    # in CI or before any TestRail access is set up.
    needs_client = not (args.command == "report-playwright" and getattr(args, "dry_run", False))

    try:
        client = TestRailClient() if needs_client else None
        dispatch(client, args)
    except EnvironmentError as e:
        print("Configuration Error: {}".format(e), file=sys.stderr)
        sys.exit(1)
    except ApiError as e:
        print("Error: {}".format(e), file=sys.stderr)
        sys.exit(1)
    except TransportError as e:
        print("Network Error: {}".format(e), file=sys.stderr)
        sys.exit(1)
    except (ValueError, json.JSONDecodeError) as e:
        print("Input Error: {}".format(e), file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print("File Error: {}".format(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
