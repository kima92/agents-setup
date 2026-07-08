#!/usr/bin/env python3
"""ArialTest execution CLI for Chikabase.

Creates and starts ArialTest e2e executions through the Chikabase REST API,
mirroring the frontend "New Execution" modal. Designed to run inside a Chika
Brain agent session, where the CHIKABASE_* environment variables are injected
automatically, but works anywhere those variables are set.

Conventions (shared with the sibling testrail/zendesk skills): env-var auth,
curl-with-urllib fallback transport, JSON on stdout, errors on stderr.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SENSITIVE_KEY_PATTERN = re.compile(r"password|secret|token|pat|key", re.IGNORECASE)
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ACTIVITY_BRANCH_PATTERN = re.compile(r"^activity-(\d+)-\d+$")
BRANCH_PREFIX = "arial-test/"
TERMINAL_STATUSES = {"passed", "failed", "cancelled"}
DEFAULT_POLL_INTERVAL_SECONDS = 20
DEFAULT_WAIT_TIMEOUT_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 120


class ConfigurationError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, message, url):
        super().__init__(f"Chikabase API error {status} on {url}: {message}")
        self.status = status


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def emit(data):
    print(json.dumps(data, indent=2, default=str))


def get_base_url():
    base = os.environ.get("CHIKABASE_BASE_URL", "http://localhost:3000/api")
    return base.rstrip("/")


def get_root_url():
    """Server root (no /api suffix) — a few legacy routes live there."""
    base = get_base_url()
    return base[: -len("/api")] if base.endswith("/api") else base


def get_token():
    token = os.environ.get("CHIKABASE_USER_ACCESS_KEY")
    if not token:
        raise ConfigurationError(
            "Configuration Error: Missing required environment variable "
            "CHIKABASE_USER_ACCESS_KEY. Inside a Chika Brain session it is "
            "injected automatically; otherwise export a valid access token."
        )
    return token


def http_request(method, url, body=None):
    token = get_token()
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    if shutil.which("curl"):
        return _curl_request(method, url, token, payload)
    return _urllib_request(method, url, token, payload)


def _curl_request(method, url, token, payload):
    # The Authorization header goes through a temp curl config file so the
    # token never appears in argv (visible via `ps`).
    config_file = tempfile.NamedTemporaryFile(
        "w", suffix=".curlcfg", delete=False
    )
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
            raise ApiError(
                0, completed.stderr.decode("utf-8", "replace").strip(), url
            )
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
        return _parse_response(
            err.code, err.read().decode("utf-8", "replace"), url
        )
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


def api_get(path):
    return http_request("GET", get_base_url() + path)


def api_post(path, body=None):
    return http_request("POST", get_base_url() + path, body)


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


def detect_execution_activity_id():
    """Read the current git branch; Brain worktrees are activity-<id>-<ts>."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    match = ACTIVITY_BRANCH_PATTERN.match(completed.stdout.strip())
    return int(match.group(1)) if match else None


def resolve_brain_activity_id(explicit):
    """Map the session's worktree branch back to its Brain activity id.

    Brain runs execute under a generic processor activity whose id is baked
    into the worktree branch name (activity-<id>-<timestamp>); that activity's
    metadata carries the owning brainActivityId. --activity-id skips all this.
    """
    if explicit is not None:
        return explicit
    execution_activity_id = detect_execution_activity_id()
    if execution_activity_id is None:
        return None
    activity = http_request(
        "GET", "%s/activities/%d" % (get_root_url(), execution_activity_id)
    )
    metadata = activity.get("metadata") or {}
    brain_activity_id = metadata.get("brainActivityId")
    return int(brain_activity_id) if brain_activity_id else None


def fetch_pinned_artifact_ids(brain_activity_id):
    pinned = api_get(
        "/brain/activities/%d/pinned-artifacts" % brain_activity_id
    )
    return [int(artifact_id) for artifact_id in (pinned or [])]


def fetch_env_template(workspace_id):
    """Server-side merge of e2e/.env.example with the user's last-used values."""
    template = api_get("/arial-test/env-template?workspaceId=%d" % workspace_id)
    variables = template.get("variables") if isinstance(template, dict) else None
    return {item["key"]: item["value"] for item in (variables or [])}


def parse_merge_env(pairs):
    merged = {}
    for pair in pairs or []:
        key, separator, value = pair.partition("=")
        if not separator or not ENV_KEY_PATTERN.match(key):
            fail(
                "Invalid --merge-env value %r: expected KEY=VALUE with an "
                "identifier-like key (e.g. APP_URL=https://test5.payme.io)."
                % pair
            )
        merged[key] = value
    return merged


def mask_env(env_map, show_secrets):
    if show_secrets:
        return dict(env_map)
    return {
        key: ("********" if SENSITIVE_KEY_PATTERN.search(key) and value else value)
        for key, value in env_map.items()
    }


def normalize_branch(suffix):
    branch = suffix if suffix.startswith(BRANCH_PREFIX) else BRANCH_PREFIX + suffix
    if re.search(r"\s|\.\.|~|\^|:|\\|\[|@\{", branch) or branch.endswith("/"):
        fail("Invalid branch name %r: not a valid git branch." % branch)
    return branch


def parse_artifact_ids(raw):
    try:
        return [int(part) for part in raw.split(",") if part.strip()]
    except ValueError:
        fail("Invalid --artifact-ids %r: expected comma-separated integers." % raw)


def summarize_execution(execution):
    return {
        "id": execution.get("id"),
        "status": execution.get("status"),
        "branchName": execution.get("branchName"),
        "testedBranch": execution.get("testedBranch"),
        "workspaceId": execution.get("workspaceId"),
        "brainActivityId": execution.get("brainActivityId"),
        "artifactIds": execution.get("artifactIds"),
        "createdAt": execution.get("createdAt"),
        "updatedAt": execution.get("updatedAt"),
    }


def wait_for_execution(execution_id, timeout_seconds, poll_interval_seconds):
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while True:
        execution = api_get("/arial-test/executions/%d" % execution_id)
        status = execution.get("status")
        if status != last_status:
            print(
                "execution %d status: %s" % (execution_id, status),
                file=sys.stderr,
            )
            last_status = status
        if status in TERMINAL_STATUSES:
            return execution
        if time.monotonic() >= deadline:
            fail(
                "Timed out after %ds waiting for execution %d (last status: "
                "%s). Check it with: status --id %d"
                % (timeout_seconds, execution_id, status, execution_id)
            )
        time.sleep(poll_interval_seconds)


def command_run(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    branch_name = normalize_branch(args.branch)
    env_overrides = parse_merge_env(args.merge_env)
    env_variables = {}
    if not args.no_env_template:
        env_variables = fetch_env_template(workspace_id)
    env_variables.update(env_overrides)
    artifact_ids = []
    brain_activity_id = None
    if args.artifact_ids is not None:
        artifact_ids = parse_artifact_ids(args.artifact_ids)
        brain_activity_id = args.activity_id
    elif not args.no_artifacts:
        brain_activity_id = resolve_brain_activity_id(args.activity_id)
        if brain_activity_id is None:
            fail(
                "Could not resolve the Brain activity for pinned artifacts "
                "(current branch is not an activity-<id>-<ts> worktree). Pass "
                "--activity-id <brainActivityId>, --artifact-ids 1,2,3, or "
                "--no-artifacts."
            )
        artifact_ids = fetch_pinned_artifact_ids(brain_activity_id)
    body = {
        "workspaceId": workspace_id,
        "branchName": branch_name,
        "artifactIds": artifact_ids,
    }
    if brain_activity_id:
        body["brainActivityId"] = brain_activity_id
    if args.prompt:
        body["userPrompt"] = args.prompt
    if args.tested_branch:
        body["testedBranch"] = args.tested_branch
    if env_variables:
        body["metadata"] = {"envVariables": env_variables}
    if args.dry_run:
        preview = json.loads(json.dumps(body))
        if env_variables:
            preview["metadata"]["envVariables"] = mask_env(
                env_variables, args.show_secrets
            )
        emit({
            "dryRun": True,
            "create": {
                "method": "POST",
                "url": get_base_url() + "/arial-test/executions",
                "body": preview,
            },
            "start": {
                "method": "POST",
                "url": get_base_url() + "/arial-test/executions/<id>/start",
            },
        })
        return
    execution = api_post("/arial-test/executions", body)
    execution_id = execution["id"]
    started = api_post("/arial-test/executions/%d/start" % execution_id)
    result = {
        "execution": summarize_execution(execution),
        "started": started,
        "envKeys": sorted(env_variables.keys()),
    }
    if args.wait:
        final_execution = wait_for_execution(
            execution_id, args.timeout, args.poll_interval
        )
        result["execution"] = summarize_execution(final_execution)
        result["outcome"] = final_execution.get("status")
    emit(result)


def command_env_template(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    env_variables = fetch_env_template(workspace_id)
    env_variables.update(parse_merge_env(args.merge_env))
    emit({
        "workspaceId": workspace_id,
        "variables": mask_env(env_variables, args.show_secrets),
    })


def command_pinned_artifacts(args):
    brain_activity_id = resolve_brain_activity_id(args.activity_id)
    if brain_activity_id is None:
        fail(
            "Could not resolve a Brain activity id from the current branch. "
            "Pass --activity-id <brainActivityId>."
        )
    artifact_ids = fetch_pinned_artifact_ids(brain_activity_id)
    result = {"brainActivityId": brain_activity_id, "artifactIds": artifact_ids}
    if args.resolve:
        result["artifacts"] = [
            api_get("/brain/artifacts/%d/meta" % artifact_id)
            for artifact_id in artifact_ids
        ]
    emit(result)


def command_status(args):
    execution = (
        wait_for_execution(args.id, args.timeout, args.poll_interval)
        if args.wait
        else api_get("/arial-test/executions/%d" % args.id)
    )
    emit(summarize_execution(execution) if args.summary else execution)


def command_list(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    executions = api_get("/arial-test/executions?workspaceId=%d" % workspace_id)
    summaries = [summarize_execution(execution) for execution in executions]
    if args.status:
        summaries = [
            summary for summary in summaries if summary["status"] == args.status
        ]
    emit(summaries)


def command_test_auth(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    executions = api_get("/arial-test/executions?workspaceId=%d" % workspace_id)
    emit({
        "ok": True,
        "baseUrl": get_base_url(),
        "workspaceId": workspace_id,
        "visibleExecutions": len(executions),
    })


def add_workspace_flag(parser):
    parser.add_argument(
        "--workspace-id", type=int,
        help="Workspace id (default: CHIKABASE_WORKSPACE_ID)",
    )


def add_wait_flags(parser):
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help="Max seconds to wait (default %d)" % DEFAULT_WAIT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between polls (default %d)" % DEFAULT_POLL_INTERVAL_SECONDS,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="arial_test_executor.py",
        description="Create and start ArialTest e2e executions via the Chikabase API.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser(
        "run", help="Create and start an execution (the headline command)"
    )
    run_parser.add_argument(
        "--branch", required=True,
        help="Branch suffix; becomes arial-test/<suffix>",
    )
    run_parser.add_argument(
        "--merge-env", action="append", metavar="KEY=VALUE",
        help="Override an env var on top of the server-side template "
             "(last-used values + e2e/.env.example). Repeatable.",
    )
    run_parser.add_argument(
        "--tested-branch", help="App branch under test (optional)"
    )
    run_parser.add_argument(
        "--prompt", help="Extra instructions for the test agent (userPrompt)"
    )
    run_parser.add_argument(
        "--activity-id", type=int,
        help="Brain activity id (default: auto-detected from the worktree branch)",
    )
    run_parser.add_argument(
        "--artifact-ids",
        help="Comma-separated artifact ids; skips pinned-artifact auto-detection",
    )
    run_parser.add_argument(
        "--no-artifacts", action="store_true",
        help="Send no artifacts instead of auto-fetching pinned ones",
    )
    run_parser.add_argument(
        "--no-env-template", action="store_true",
        help="Skip the server env template; send only --merge-env values",
    )
    run_parser.add_argument(
        "--wait", action="store_true",
        help="Poll until the execution reaches passed/failed/cancelled",
    )
    run_parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the requests without creating anything",
    )
    run_parser.add_argument(
        "--show-secrets", action="store_true",
        help="Do not mask sensitive env values in --dry-run output",
    )
    add_workspace_flag(run_parser)
    add_wait_flags(run_parser)
    run_parser.set_defaults(handler=command_run)
    env_parser = subparsers.add_parser(
        "env-template",
        help="Show the merged env template (last-used values win)",
    )
    env_parser.add_argument("--merge-env", action="append", metavar="KEY=VALUE")
    env_parser.add_argument("--show-secrets", action="store_true")
    add_workspace_flag(env_parser)
    env_parser.set_defaults(handler=command_env_template)
    pinned_parser = subparsers.add_parser(
        "pinned-artifacts", help="List the Brain activity's pinned artifact ids"
    )
    pinned_parser.add_argument("--activity-id", type=int)
    pinned_parser.add_argument(
        "--resolve", action="store_true",
        help="Also fetch each artifact's metadata (title, type, filePath)",
    )
    pinned_parser.set_defaults(handler=command_pinned_artifacts)
    status_parser = subparsers.add_parser(
        "status", help="Get an execution (optionally wait for completion)"
    )
    status_parser.add_argument("--id", type=int, required=True)
    status_parser.add_argument("--wait", action="store_true")
    status_parser.add_argument(
        "--summary", action="store_true", help="Print a compact summary only"
    )
    add_wait_flags(status_parser)
    status_parser.set_defaults(handler=command_status)
    list_parser = subparsers.add_parser(
        "list", help="List executions in a workspace"
    )
    list_parser.add_argument(
        "--status",
        choices=["queued", "running", "passed", "failed", "cancelled"],
    )
    add_workspace_flag(list_parser)
    list_parser.set_defaults(handler=command_list)
    auth_parser = subparsers.add_parser(
        "test-auth", help="Verify the CHIKABASE_* credentials work"
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
