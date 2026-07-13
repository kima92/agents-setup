#!/usr/bin/env python3
"""SQL builder + execution CLI for Chikabase.

Drives the Chikabase "Answer" API — the same feature the frontend SQL-builder
screen uses. It turns a natural-language question into SQL (or accepts SQL you
already have), runs it against a workspace database, polls the async execution,
and prints the rows. Designed to run inside a Chika Brain agent session, where
CHIKABASE_* environment variables are injected automatically, but works anywhere
those variables are set — or after a one-time `login` with email/password.

Conventions (shared with the sibling arial-test/testrail/zendesk skills):
env-var auth, curl-with-urllib fallback transport, JSON on stdout, errors on
stderr. No third-party Python libraries.
"""

import argparse
import csv
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ATTEMPT_TERMINAL_STATUSES = {"succeeded", "failed"}
EXECUTION_TERMINAL_STATUSES = {"succeeded", "failed", "timeout", "cancelled"}
EXECUTION_RUNNING_STATUSES = {"pending", "analyzing", "executing"}
READY_SQL_MARKER = "ready-made SQL"
SQL_LEADING_KEYWORDS = re.compile(
    r"^\s*(with|select|insert|update|delete|explain|analyze|show|create|alter|drop|truncate)\b",
    re.IGNORECASE,
)
LAYOUT_FLIP_STATUSES = {404, 405}
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 600
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 600
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_DISPLAY_ROWS = 50
HTTP_TIMEOUT_SECONDS = 120


class ConfigurationError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, message, url):
        super().__init__(f"Chikabase API error {status} on {url}: {message}")
        self.status = status
        self.api_message = message


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def emit(data):
    print(json.dumps(data, indent=2, default=str))


def get_base_url():
    base = os.environ.get("CHIKABASE_BASE_URL", "http://localhost:3000/api")
    return base.rstrip("/")


def get_root_url():
    """Server root (no /api suffix) — auth/login lives there, not under /api."""
    base = get_base_url()
    return base[: -len("/api")] if base.endswith("/api") else base


def get_token_file_path():
    """Where `login` caches the minted token so later commands can reuse it."""
    override = os.environ.get("CHIKABASE_TOKEN_FILE")
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".chikabase", "token")


def read_cached_token():
    path = get_token_file_path()
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        token = handle.read().strip()
    return token or None


def write_cached_token(token):
    path = get_token_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(token)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def get_credentials():
    """Email/password for auto-login, taken only from the environment."""
    email = os.environ.get("CHIKABASE_USER_EMAIL")
    password = os.environ.get("CHIKABASE_USER_PASS")
    return (email, password) if email and password else (None, None)


def can_relogin():
    return get_credentials() != (None, None)


# Two deployment layouts reach the same backend. "direct" (Chika Brain sessions,
# local dev): the answer routes sit at base + '/answer/…' and login at
# root + '/auth/login'. "nginx" (public ingress, e.g. chikabase.payme.io/api):
# an '/api'-stripping proxy sits in front, so the double-prefixed answer routes
# need base + '/api/answer/…' and login moves to base + '/auth/login'. The
# default matches the Brain convention (same as the arial-test skill); a wrong
# guess self-heals on the first 404/405, after which the layout is pinned.
_LAYOUT = None
_LAYOUT_CONFIRMED = False


def current_layout():
    global _LAYOUT
    if _LAYOUT is None:
        override = os.environ.get("CHIKABASE_API_LAYOUT")
        _LAYOUT = override if override in ("direct", "nginx") else "direct"
    return _LAYOUT


def is_layout_locked():
    return _LAYOUT_CONFIRMED or os.environ.get("CHIKABASE_API_LAYOUT") in (
        "direct", "nginx",
    )


def confirm_layout():
    global _LAYOUT_CONFIRMED
    _LAYOUT_CONFIRMED = True


def flip_layout():
    global _LAYOUT
    _LAYOUT = "nginx" if current_layout() == "direct" else "direct"


def answer_url(path):
    """Full URL for an answer route path like '/answer/generate'."""
    prefix = "/api" if current_layout() == "nginx" else ""
    return get_base_url() + prefix + path


def login_url():
    root = get_base_url() if current_layout() == "nginx" else get_root_url()
    return root + "/auth/login"


def perform_login():
    """Trade CHIKABASE_USER_EMAIL/PASS for a token, cache it, and return it."""
    email, password = get_credentials()
    if not email:
        raise ConfigurationError(
            "Configuration Error: no access token and no credentials. Inside a "
            "Chika Brain session CHIKABASE_USER_ACCESS_KEY is injected "
            "automatically. Otherwise export CHIKABASE_USER_EMAIL and "
            "CHIKABASE_USER_PASS (or CHIKABASE_USER_ACCESS_KEY directly)."
        )
    credentials = {"email": email, "password": password}
    try:
        response = http_request("POST", login_url(), credentials, token="none")
    except ApiError as err:
        if err.status in LAYOUT_FLIP_STATUSES and not is_layout_locked():
            flip_layout()
            response = http_request("POST", login_url(), credentials, token="none")
        else:
            raise
    confirm_layout()
    token = response.get("access_token") if isinstance(response, dict) else None
    if not token:
        raise ConfigurationError("Login succeeded but no access_token was returned.")
    write_cached_token(token)
    return token


def resolve_bearer(force_login=False):
    """Token precedence: injected env key, cached token, then auto-login."""
    if not force_login:
        env_token = os.environ.get("CHIKABASE_USER_ACCESS_KEY")
        if env_token:
            return env_token
        cached = read_cached_token()
        if cached:
            return cached
    return perform_login()


def http_request(method, url, body=None, token=None):
    bearer = token if token is not None else resolve_bearer()
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    if shutil.which("curl"):
        return _curl_request(method, url, bearer, payload)
    return _urllib_request(method, url, bearer, payload)


def request_with_auth(method, path, body=None):
    """Call an answer route, self-healing on a 401 (re-login) or 404/405 (layout)."""
    # Resolve the token first: an auto-login may pin the layout, and the URL must
    # be built afterwards so it reflects the confirmed layout.
    token = resolve_bearer()
    try:
        result = http_request(method, answer_url(path), body, token=token)
    except ApiError as err:
        if err.status == 401 and can_relogin():
            token = resolve_bearer(force_login=True)
            result = http_request(method, answer_url(path), body, token=token)
        elif err.status in LAYOUT_FLIP_STATUSES and not is_layout_locked():
            flip_layout()
            result = http_request(method, answer_url(path), body, token=token)
        else:
            raise
    confirm_layout()
    return result


def _curl_request(method, url, token, payload):
    # The Authorization header goes through a temp curl config file so the token
    # never appears in argv (visible via `ps`).
    config_file = tempfile.NamedTemporaryFile("w", suffix=".curlcfg", delete=False)
    try:
        config_file.write('header = "Authorization: Bearer %s"\n' % token)
        config_file.close()
        command = [
            "curl", "-sS", "-X", method, "-K", config_file.name,
            "-H", "Content-Type: application/json",
            "-w", "\n%{http_code}",
            "--max-time", str(HTTP_TIMEOUT_SECONDS),
            url,
        ]
        if payload is not None:
            command += ["--data-binary", "@-"]
        completed = subprocess.run(
            command, input=payload, capture_output=True,
            timeout=HTTP_TIMEOUT_SECONDS + 10,
        )
        if completed.returncode != 0:
            raise ApiError(0, completed.stderr.decode("utf-8", "replace").strip(), url)
        raw = completed.stdout.decode("utf-8", "replace")
        body_text, _, status_text = raw.rpartition("\n")
        return _parse_response(int(status_text or 0), body_text, url)
    finally:
        os.unlink(config_file.name)


def _urllib_request(method, url, token, payload):
    request = urllib.request.Request(url, data=payload, method=method)
    request.add_header("Authorization", "Bearer %s" % token)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return _parse_response(
                response.status, response.read().decode("utf-8", "replace"), url
            )
    except urllib.error.HTTPError as err:
        return _parse_response(err.code, err.read().decode("utf-8", "replace"), url)
    except urllib.error.URLError as err:
        raise ApiError(0, str(err.reason), url)


def _parse_response(status, body_text, url):
    body_text = body_text.strip()
    parsed = None
    if body_text:
        try:
            parsed = json.loads(body_text)
        except ValueError:
            parsed = body_text
    if status >= 400 or status == 0:
        message = parsed.get("message") if isinstance(parsed, dict) else parsed
        if isinstance(message, list):
            message = "; ".join(str(item) for item in message)
        raise ApiError(status, message or "request failed", url)
    return parsed


def unwrap(response):
    """Answer endpoints wrap payloads as { success, data } — return data."""
    if isinstance(response, dict) and "data" in response and "success" in response:
        return response["data"]
    return response


def api_get(path):
    return unwrap(request_with_auth("GET", path))


def api_post(path, body=None):
    return unwrap(request_with_auth("POST", path, body))


def resolve_workspace_id(explicit):
    if explicit is not None:
        return explicit
    from_env = os.environ.get("CHIKABASE_WORKSPACE_ID")
    if from_env:
        return int(from_env)
    raise ConfigurationError(
        "Configuration Error: no workspace id. Pass --workspace-id or set "
        "CHIKABASE_WORKSPACE_ID (injected automatically in Brain sessions)."
    )


def looks_like_sql(text):
    return bool(SQL_LEADING_KEYWORDS.match(text or ""))


def create_direct_attempt(workspace_id, sql, title=None):
    body = {"workspaceId": workspace_id, "sql": sql}
    if title:
        body["title"] = title
    return api_post("/answer/direct", body)


def create_generate_attempt(workspace_id, query, agent, include_mermaid):
    body = {"workspaceId": workspace_id, "query": query, "agent": agent}
    if include_mermaid:
        body["includeMermaid"] = True
    return api_post("/answer/generate", body)


def fetch_attempt(attempt_id):
    return api_get("/answer/history/%d" % attempt_id)


def wait_for_attempt(attempt_id, timeout_seconds, poll_interval_seconds):
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while True:
        attempt = fetch_attempt(attempt_id)
        status = attempt.get("status")
        if status != last_status:
            print("attempt %d status: %s" % (attempt_id, status), file=sys.stderr)
            last_status = status
        if status in ATTEMPT_TERMINAL_STATUSES:
            return attempt
        if time.monotonic() >= deadline:
            fail(
                "Timed out after %ds waiting for attempt %d (last status: %s). "
                "Check it with: attempt --id %d"
                % (timeout_seconds, attempt_id, status, attempt_id)
            )
        time.sleep(poll_interval_seconds)


def start_execution(attempt_id, database_source_id):
    body = {}
    if database_source_id is not None:
        body["databaseSourceId"] = database_source_id
    return api_post("/answer/history/%d/execute" % attempt_id, body)


def fetch_execution_status(execution_id):
    return api_get("/answer/execution/%d/status" % execution_id)


def fetch_execution_result(execution_id):
    return api_get("/answer/execution/%d/result" % execution_id)


def wait_for_execution(execution_id, timeout_seconds, poll_interval_seconds):
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while True:
        status_data = fetch_execution_status(execution_id)
        status = status_data.get("status")
        if status != last_status:
            print("execution %d status: %s" % (execution_id, status), file=sys.stderr)
            last_status = status
        if status in EXECUTION_TERMINAL_STATUSES:
            return status_data
        if time.monotonic() >= deadline:
            fail(
                "Timed out after %ds waiting for execution %d (last status: %s). "
                "Check it with: execution --id %d"
                % (timeout_seconds, execution_id, status, execution_id)
            )
        time.sleep(poll_interval_seconds)


def summarize_attempt(attempt):
    return {
        "id": attempt.get("id"),
        "status": attempt.get("status"),
        "source": attempt.get("source"),
        "workspaceId": attempt.get("workspaceId"),
        "query": attempt.get("query"),
        "generatedSql": attempt.get("generatedSql"),
        "response": attempt.get("response"),
        "entities": attempt.get("entities"),
        "confidence": attempt.get("confidence"),
        "error": attempt.get("error"),
    }


def rows_to_csv(execution_result):
    fields = [field["name"] for field in (execution_result.get("fields") or [])]
    rows = execution_result.get("rows") or []
    if not fields and rows and isinstance(rows[0], dict):
        fields = list(rows[0].keys())
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(fields)
    for row in rows:
        if isinstance(row, dict):
            writer.writerow([row.get(name) for name in fields])
        elif isinstance(row, (list, tuple)):
            writer.writerow(list(row))
        else:
            writer.writerow([row])
    return buffer.getvalue()


def write_csv_file(execution_result, path):
    resolved = os.path.expanduser(path)
    with open(resolved, "w", encoding="utf-8", newline="") as handle:
        handle.write(rows_to_csv(execution_result))
    return resolved


def build_result_payload(execution_id, result, display_rows, csv_path):
    execution_result = result.get("executionResult") or {}
    rows = execution_result.get("rows") or []
    payload = {
        "executionId": execution_id,
        "status": result.get("status"),
        "rowCount": execution_result.get("rowCount", len(rows)),
        "fields": [field.get("name") for field in (execution_result.get("fields") or [])],
        "executionTimeMs": result.get("executionTimeMs"),
        "analyzeTimeMs": result.get("analyzeTimeMs"),
        "rows": rows[:display_rows],
    }
    if len(rows) > display_rows:
        payload["rowsTruncated"] = True
        payload["displayedRows"] = display_rows
    if csv_path:
        payload["csvPath"] = write_csv_file(execution_result, csv_path)
    return payload


def resolve_attempt_id(args):
    """Turn a run/execute request into a ready-to-execute attempt id + summary.

    Three input shapes: an existing --attempt-id, ready --sql (routed straight to
    /direct), or a natural-language --query (routed to the AI agent, with an
    automatic fall back to /direct when the server flags it as ready-made SQL).
    """
    if getattr(args, "attempt_id", None) is not None:
        return args.attempt_id, None
    workspace_id = resolve_workspace_id(args.workspace_id)
    if args.sql:
        attempt = create_direct_attempt(workspace_id, args.sql.strip(), args.title)
        return attempt["attemptId"], summarize_attempt(fetch_attempt(attempt["attemptId"]))
    query = args.query.strip()
    if looks_like_sql(query):
        print(
            "note: --query looks like SQL; submitting it directly without the AI agent.",
            file=sys.stderr,
        )
        attempt = create_direct_attempt(workspace_id, query, args.title)
        return attempt["attemptId"], summarize_attempt(fetch_attempt(attempt["attemptId"]))
    try:
        attempt = create_generate_attempt(
            workspace_id, query, args.agent, args.mermaid
        )
    except ApiError as err:
        if err.status == 400 and READY_SQL_MARKER in str(err.api_message or ""):
            print(
                "note: server flagged the query as ready-made SQL; retrying via /direct.",
                file=sys.stderr,
            )
            attempt = create_direct_attempt(workspace_id, query, args.title)
            return attempt["attemptId"], summarize_attempt(
                fetch_attempt(attempt["attemptId"])
            )
        raise
    attempt_id = attempt["attemptId"]
    final_attempt = wait_for_attempt(
        attempt_id, args.attempt_timeout, args.poll_interval
    )
    if final_attempt.get("status") == "failed":
        fail(
            "SQL generation failed for attempt %d: %s"
            % (attempt_id, final_attempt.get("error") or "unknown error")
        )
    return attempt_id, summarize_attempt(final_attempt)


def command_run(args):
    attempt_id, attempt_summary = resolve_attempt_id(args)
    result = {"attempt": attempt_summary or {"id": attempt_id}}
    if args.analyze:
        result["analysis"] = api_post(
            "/answer/history/%d/analyze" % attempt_id,
            {"databaseSourceId": args.database_source_id}
            if args.database_source_id
            else {},
        )
    if args.no_execute:
        emit(result)
        return
    execution = start_execution(attempt_id, args.database_source_id)
    execution_id = execution["executionId"]
    final_status = wait_for_execution(
        execution_id, args.execution_timeout, args.poll_interval
    )
    result["executionId"] = execution_id
    result["executionStatus"] = final_status.get("status")
    if final_status.get("status") == "succeeded":
        result["result"] = build_result_payload(
            execution_id, fetch_execution_result(execution_id), args.max_rows, args.csv
        )
    else:
        result["error"] = final_status.get("error")
    emit(result)


def command_generate(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    attempt = create_generate_attempt(
        workspace_id, args.query.strip(), args.agent, args.mermaid
    )
    attempt_id = attempt["attemptId"]
    result = {"attemptId": attempt_id, "activityId": attempt.get("activityId")}
    if args.wait:
        final_attempt = wait_for_attempt(
            attempt_id, args.attempt_timeout, args.poll_interval
        )
        result["attempt"] = summarize_attempt(final_attempt)
    emit(result)


def command_direct(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    attempt = create_direct_attempt(workspace_id, args.sql.strip(), args.title)
    emit({"attemptId": attempt["attemptId"]})


def command_attempt(args):
    if args.wait:
        attempt = wait_for_attempt(args.id, args.attempt_timeout, args.poll_interval)
    else:
        attempt = fetch_attempt(args.id)
    emit(summarize_attempt(attempt) if args.summary else attempt)


def command_analyze(args):
    body = {"databaseSourceId": args.database_source_id} if args.database_source_id else {}
    emit(api_post("/answer/history/%d/analyze" % args.attempt_id, body))


def command_execute(args):
    execution = start_execution(args.attempt_id, args.database_source_id)
    execution_id = execution["executionId"]
    result = {"executionId": execution_id, "status": execution.get("status")}
    if args.wait:
        final_status = wait_for_execution(
            execution_id, args.execution_timeout, args.poll_interval
        )
        result["status"] = final_status.get("status")
        if final_status.get("status") == "succeeded":
            result["result"] = build_result_payload(
                execution_id, fetch_execution_result(execution_id),
                args.max_rows, args.csv,
            )
        else:
            result["error"] = final_status.get("error")
    emit(result)


def command_execution(args):
    if args.wait:
        status_data = wait_for_execution(
            args.id, args.execution_timeout, args.poll_interval
        )
    else:
        status_data = fetch_execution_status(args.id)
    result = {"execution": status_data}
    if args.result and status_data.get("status") == "succeeded":
        result["result"] = build_result_payload(
            args.id, fetch_execution_result(args.id), args.max_rows, args.csv
        )
    emit(result)


def command_databases(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    emit(api_get("/answer/workspace/%d/databases" % workspace_id))


def command_history(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    path = "/answer/history?workspaceId=%d&limit=%d" % (workspace_id, args.limit)
    if args.status:
        path += "&status=%s" % args.status
    if args.q:
        path += "&q=%s" % urllib.request.quote(args.q)
    emit(api_get(path))


def command_login(args):
    token = perform_login()
    emit({
        "ok": True,
        "tokenFile": get_token_file_path(),
        "tokenPreview": token[:8] + "…",
        "layout": current_layout(),
        "hint": "Token cached; subsequent commands reuse it and re-login on expiry.",
    })


def command_test_auth(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    databases = api_get("/answer/workspace/%d/databases" % workspace_id)
    emit({
        "ok": True,
        "baseUrl": get_base_url(),
        "layout": current_layout(),
        "workspaceId": workspace_id,
        "databaseSources": databases,
    })


def add_workspace_flag(parser):
    parser.add_argument(
        "--workspace-id", type=int,
        help="Workspace id (default: CHIKABASE_WORKSPACE_ID)",
    )


def add_poll_flags(parser):
    parser.add_argument(
        "--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between polls (default %d)" % DEFAULT_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--attempt-timeout", type=int, default=DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
        help="Max seconds to wait for SQL generation (default %d)"
        % DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--execution-timeout", type=int, default=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        help="Max seconds to wait for query execution (default %d)"
        % DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    )


def add_result_flags(parser):
    parser.add_argument(
        "--max-rows", type=int, default=DEFAULT_DISPLAY_ROWS,
        help="Max result rows to print inline (default %d; full set still goes to "
        "--csv)" % DEFAULT_DISPLAY_ROWS,
    )
    parser.add_argument(
        "--csv", help="Write the full result set to this CSV file path",
    )


def add_agent_flags(parser):
    parser.add_argument(
        "--agent", choices=["langraph", "claude"], default="langraph",
        help="NL->SQL agent (default: langraph)",
    )
    parser.add_argument(
        "--mermaid", action="store_true", help="Ask the agent for a mermaid diagram",
    )
    parser.add_argument(
        "--title", help="Optional title (used when routing to a direct attempt)",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="sql_executor.py",
        description="Build SQL from natural language (or accept ready SQL), run it "
        "against a workspace database, and poll for results via the Chikabase API.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser(
        "run", help="End-to-end: build/accept SQL, execute, poll, print rows"
    )
    run_group = run_parser.add_mutually_exclusive_group(required=True)
    run_group.add_argument("--query", help="Natural-language question to turn into SQL")
    run_group.add_argument("--sql", help="Ready-made SQL to run as-is (skips the AI)")
    run_group.add_argument(
        "--attempt-id", type=int, help="Execute an existing attempt's SQL",
    )
    run_parser.add_argument(
        "--database-source-id", type=int,
        help="Target database source id (default: workspace primary database)",
    )
    run_parser.add_argument(
        "--analyze", action="store_true",
        help="Run EXPLAIN/validation before executing",
    )
    run_parser.add_argument(
        "--no-execute", action="store_true",
        help="Only build the SQL; do not run it",
    )
    add_agent_flags(run_parser)
    add_result_flags(run_parser)
    add_workspace_flag(run_parser)
    add_poll_flags(run_parser)
    run_parser.set_defaults(handler=command_run)
    generate_parser = subparsers.add_parser(
        "generate", help="Create an NL->SQL attempt (optionally wait for the SQL)"
    )
    generate_parser.add_argument("--query", required=True)
    generate_parser.add_argument("--wait", action="store_true")
    add_agent_flags(generate_parser)
    add_workspace_flag(generate_parser)
    add_poll_flags(generate_parser)
    generate_parser.set_defaults(handler=command_generate)
    direct_parser = subparsers.add_parser(
        "direct", help="Register ready-made SQL as an executable attempt"
    )
    direct_parser.add_argument("--sql", required=True)
    direct_parser.add_argument("--title")
    add_workspace_flag(direct_parser)
    direct_parser.set_defaults(handler=command_direct)
    attempt_parser = subparsers.add_parser(
        "attempt", help="Fetch an attempt (optionally wait for generation)"
    )
    attempt_parser.add_argument("--id", type=int, required=True)
    attempt_parser.add_argument("--wait", action="store_true")
    attempt_parser.add_argument("--summary", action="store_true")
    add_poll_flags(attempt_parser)
    attempt_parser.set_defaults(handler=command_attempt)
    analyze_parser = subparsers.add_parser(
        "analyze", help="EXPLAIN/validate an attempt's SQL (synchronous)"
    )
    analyze_parser.add_argument("--attempt-id", type=int, required=True)
    analyze_parser.add_argument("--database-source-id", type=int)
    analyze_parser.set_defaults(handler=command_analyze)
    execute_parser = subparsers.add_parser(
        "execute", help="Run an attempt's SQL against the DB (optionally wait)"
    )
    execute_parser.add_argument("--attempt-id", type=int, required=True)
    execute_parser.add_argument("--database-source-id", type=int)
    execute_parser.add_argument("--wait", action="store_true")
    add_result_flags(execute_parser)
    add_poll_flags(execute_parser)
    execute_parser.set_defaults(handler=command_execute)
    execution_parser = subparsers.add_parser(
        "execution", help="Poll an execution's status and optionally its result"
    )
    execution_parser.add_argument("--id", type=int, required=True)
    execution_parser.add_argument("--wait", action="store_true")
    execution_parser.add_argument(
        "--result", action="store_true", help="Also fetch the rows when succeeded",
    )
    add_result_flags(execution_parser)
    add_poll_flags(execution_parser)
    execution_parser.set_defaults(handler=command_execution)
    databases_parser = subparsers.add_parser(
        "databases", help="List the workspace's database sources"
    )
    add_workspace_flag(databases_parser)
    databases_parser.set_defaults(handler=command_databases)
    history_parser = subparsers.add_parser(
        "history", help="List recent SQL attempts in the workspace"
    )
    history_parser.add_argument(
        "--status", choices=["queued", "running", "succeeded", "failed"],
    )
    history_parser.add_argument("--q", help="Free-text search over the query")
    history_parser.add_argument("--limit", type=int, default=20)
    add_workspace_flag(history_parser)
    history_parser.set_defaults(handler=command_history)
    login_parser = subparsers.add_parser(
        "login",
        help="Force a fresh login from CHIKABASE_USER_EMAIL/PASS and cache the token",
    )
    login_parser.set_defaults(handler=command_login)
    auth_parser = subparsers.add_parser(
        "test-auth", help="Verify credentials and list the workspace databases"
    )
    add_workspace_flag(auth_parser)
    auth_parser.set_defaults(handler=command_test_auth)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        args.handler(args)
    except (ConfigurationError, ApiError) as err:
        fail(str(err))


if __name__ == "__main__":
    main()
