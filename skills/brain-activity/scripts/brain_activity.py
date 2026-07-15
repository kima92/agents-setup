#!/usr/bin/env python3
"""Chika Brain conversation CLI for Chikabase.

Drives Brain "activities" (AI conversations) through the Chikabase REST API:
start a new session and watch the agent work, send a follow-up to an existing
one, and pull back conversations, messages, and artifacts by id. Designed to
run inside a Chika Brain agent session, where the CHIKABASE_* environment
variables are injected automatically, but works anywhere those variables (or
an email/password pair) are set.

Conventions (shared with the sibling arial-test-executor/testrail/zendesk
skills): env-var auth, curl-with-urllib fallback transport, JSON on stdout,
human-readable progress and errors on stderr.
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

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# An assistant reply is "done" once it leaves the streaming state. `error` and
# `aborted` are terminal too — the agent stopped, just not happily — so the
# monitor returns them instead of hanging until the timeout.
TERMINAL_MESSAGE_STATUSES = {"completed", "error", "aborted"}
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_WAIT_TIMEOUT_SECONDS = 900
HTTP_TIMEOUT_SECONDS = 120
_CACHED_TOKEN = None


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


def log(message):
    print(message, file=sys.stderr)


def get_server_root():
    """The configured server root — every route is concatenated onto this.

    Relative to the Nest root the controllers are literally `/api/brain/...`
    (there is no global prefix) and `/auth/...`. Configure CHIKABASE_BASE_URL to
    whatever prefix reaches that root: `https://chikabase.payme.io/api` for the
    hosted proxy, `http://localhost:3000` for a direct local backend.
    """
    base = os.environ.get("CHIKABASE_BASE_URL", "http://localhost:3000")
    return base.rstrip("/")


def get_base_url():
    """Base for the `/api/...` controllers (brain routes hang off this)."""
    return get_server_root() + "/api"


def get_root_url():
    """Server root — auth routes live directly here, not under `/api`."""
    return get_server_root()


def get_token():
    """Bearer token: the injected access key first, else an email/password login.

    Inside a Brain session CHIKABASE_USER_ACCESS_KEY is present and this never
    hits the network. Standalone, it falls back to the Postman login flow using
    CHIKABASE_EMAIL/CHIKABASE_PASSWORD and caches the minted token for the run.
    """
    global _CACHED_TOKEN
    token = os.environ.get("CHIKABASE_USER_ACCESS_KEY")
    if token:
        return token
    if _CACHED_TOKEN:
        return _CACHED_TOKEN
    _CACHED_TOKEN = login_with_password()
    return _CACHED_TOKEN


def login_with_password():
    email = os.environ.get("CHIKABASE_USER_EMAIL") or os.environ.get(
        "CHIKABASE_EMAIL"
    )
    password = os.environ.get("CHIKABASE_USER_PASS") or os.environ.get(
        "CHIKABASE_PASSWORD"
    )
    if not email or not password:
        raise ConfigurationError(
            "Configuration Error: no credentials. Inside a Chika Brain session "
            "CHIKABASE_USER_ACCESS_KEY is injected automatically; otherwise "
            "export it, or set CHIKABASE_USER_EMAIL and CHIKABASE_USER_PASS so "
            "the CLI can log in for you."
        )
    url = get_root_url() + "/auth/login"
    response = _send_request(
        "POST", url, token=None, body={"email": email, "password": password}
    )
    token = response.get("access_token") if isinstance(response, dict) else None
    if not token:
        raise ConfigurationError(
            "Login succeeded but no access_token was returned by /auth/login."
        )
    user = response.get("user") or {}
    if user.get("defaultWorkspaceId") and not os.environ.get(
        "CHIKABASE_WORKSPACE_ID"
    ):
        os.environ["CHIKABASE_WORKSPACE_ID"] = str(user["defaultWorkspaceId"])
    log("Logged in as %s via email/password." % email)
    return token


def http_request(method, url, body=None):
    return _send_request(method, url, get_token(), body)


def _send_request(method, url, token, body):
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    if shutil.which("curl"):
        return _curl_request(method, url, token, payload)
    return _urllib_request(method, url, token, payload)


def _curl_request(method, url, token, payload):
    # The Authorization header goes through a temp curl config file so the
    # token never appears in argv (visible via `ps`).
    config_file = tempfile.NamedTemporaryFile("w", suffix=".curlcfg", delete=False)
    try:
        if token:
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
    if token:
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
        "CHIKABASE_WORKSPACE_ID (injected automatically in Brain sessions; the "
        "email/password login also fills it from your default workspace)."
    )


def describe_user(user):
    return "%s <%s> (id %s)" % (
        user.get("fullName") or "?", user.get("email"), user.get("id")
    )


def fetch_org_users():
    """The org roster, resolvable by any authenticated user (no admin needed).

    `/tasks/assignable-users` is the authoritative org set ({id, email}); the
    leaderboard adds display names but isn't strictly org-scoped, so its
    fullName is grafted on only for ids already in the roster — a name lookup
    can never match someone outside the org. Mirrors how the Brain UI knows
    other users without the admin-only /users route.
    """
    roster = api_get("/tasks/assignable-users")
    users = {
        int(user["id"]): {
            "id": int(user["id"]), "email": user.get("email"), "fullName": None
        }
        for user in roster
        if user.get("id") is not None
    }
    try:
        for row in api_get("/metrics/leaderboard?limit=1000"):
            user_id = row.get("userId")
            if user_id in users and row.get("fullName"):
                users[user_id]["fullName"] = row["fullName"]
    except ApiError:
        pass
    return list(users.values())


def resolve_user_id(value):
    """A user reference is a numeric id, or an email/name substring to look up.

    Substrings are matched case-insensitively against org emails first, then
    full names. Exactly one match resolves; zero or many is a hard error so a
    filter never silently targets the wrong person. Pass a numeric id to skip
    the lookup entirely.
    """
    if value is None:
        return None
    if value.isdigit():
        return int(value)
    needle = value.strip().lower()
    users = fetch_org_users()
    matches = [u for u in users if needle in (u.get("email") or "").lower()]
    if not matches:
        matches = [u for u in users if needle in (u.get("fullName") or "").lower()]
    if not matches:
        fail("No org user matches %r (searched email and full name)." % value)
    if len(matches) > 1:
        listed = "; ".join(describe_user(u) for u in matches[:10])
        fail(
            "Ambiguous user %r — %d matches: %s. Narrow it or pass the numeric "
            "id." % (value, len(matches), listed)
        )
    log("Resolved user %r → %s" % (value, describe_user(matches[0])))
    return int(matches[0]["id"])


def parse_id_list(raw, flag):
    try:
        return [int(part) for part in raw.split(",") if part.strip()]
    except ValueError:
        fail("Invalid %s %r: expected comma-separated integers." % (flag, raw))


def parse_csv(raw):
    return [part.strip() for part in raw.split(",") if part.strip()]


def truncate(text, limit):
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


def summarize_activity(activity):
    return {
        "id": activity.get("id"),
        "title": activity.get("title"),
        "type": activity.get("type"),
        "status": activity.get("status"),
        "workspaceId": activity.get("workspaceId"),
        "createdBy": activity.get("createdBy"),
        "systemPrompt": activity.get("systemPrompt"),
        "createdAt": activity.get("createdAt"),
        "updatedAt": activity.get("updatedAt"),
    }


def summarize_message(message, content_limit):
    return {
        "id": message.get("id"),
        "role": message.get("role"),
        "status": message.get("status"),
        "parentMessageId": message.get("parentMessageId"),
        "presetSlug": message.get("presetSlug"),
        "content": truncate(message.get("content"), content_limit),
        "artifactIds": message.get("artifactIds") or [],
        "resourceCount": len(message.get("resources") or []),
        "createdAt": message.get("createdAt"),
    }


def summarize_artifact(artifact):
    return {
        "id": artifact.get("id"),
        "title": artifact.get("title"),
        "type": artifact.get("type"),
        "createdAt": artifact.get("createdAt"),
    }


def create_activity(workspace_id, title, description, preset_slug):
    body = {"workspaceId": workspace_id}
    if title:
        body["title"] = title
    if description:
        body["description"] = description
    if preset_slug:
        body["systemPrompt"] = {"preset": preset_slug}
    return api_post("/brain/activities", body)


def send_message(activity_id, message, preset_slug, artifact_ids, allowed_tools,
                 system_prompt):
    body = {"message": message}
    if preset_slug:
        body["presetSlug"] = preset_slug
    if artifact_ids:
        body["artifactIds"] = artifact_ids
    if allowed_tools:
        body["allowedTools"] = allowed_tools
    if system_prompt:
        body["systemPromptOverride"] = system_prompt
    return api_post("/brain/activities/%d/messages" % activity_id, body)


def find_assistant_reply(activity_id, user_message_id):
    """The assistant reply is the newest message whose parent is our user turn.

    Reruns/forks add siblings under the same user message, so pick the highest
    id — that is the reply the send we just made is producing.
    """
    messages = api_get("/brain/activities/%d/messages" % activity_id)
    replies = [
        message
        for message in messages
        if message.get("role") == "assistant"
        and message.get("parentMessageId") == user_message_id
    ]
    if not replies:
        return None
    return max(replies, key=lambda message: message.get("id") or 0)


def monitor_assistant(activity_id, user_message_id, timeout_seconds,
                      poll_interval_seconds):
    """Poll until the assistant reply for our user turn leaves 'streaming'.

    Progress (status transitions and resource counts as the agent reads files,
    runs searches, calls APIs) is logged to stderr so a watching human sees the
    agent working; the resolved message is returned for the JSON payload.
    """
    deadline = time.monotonic() + timeout_seconds
    last_signature = None
    while True:
        reply = find_assistant_reply(activity_id, user_message_id)
        if reply is not None:
            status = reply.get("status")
            resource_count = len(reply.get("resources") or [])
            signature = (status, resource_count)
            if signature != last_signature:
                log(
                    "assistant message %s: status=%s resources=%d"
                    % (reply.get("id"), status, resource_count)
                )
                last_signature = signature
            if status in TERMINAL_MESSAGE_STATUSES:
                return reply
        elif last_signature is None:
            log("waiting for the agent to start…")
            last_signature = ("pending", 0)
        if time.monotonic() >= deadline:
            fail(
                "Timed out after %ds waiting for the assistant reply in activity "
                "%d. It may still be running — check with: messages "
                "--activity-id %d" % (timeout_seconds, activity_id, activity_id)
            )
        time.sleep(poll_interval_seconds)


def resolve_generated_artifacts(reply):
    artifacts = []
    for artifact_id in reply.get("artifactIds") or []:
        try:
            meta = api_get("/brain/artifacts/%d/meta" % int(artifact_id))
            artifacts.append(summarize_artifact(meta))
        except ApiError as err:
            artifacts.append({"id": artifact_id, "error": str(err)})
    return artifacts


def command_chat(args):
    preset_slug = args.preset
    artifact_ids = parse_id_list(args.artifact_ids, "--artifact-ids") \
        if args.artifact_ids else []
    allowed_tools = parse_csv(args.allowed_tools) if args.allowed_tools else []
    if args.activity_id is not None:
        activity_id = args.activity_id
    else:
        workspace_id = resolve_workspace_id(args.workspace_id)
        activity = create_activity(
            workspace_id, args.title, args.description, preset_slug
        )
        activity_id = activity["id"]
        log("Created Brain activity %d (%s)" % (activity_id, activity.get("title")))
    sent = send_message(
        activity_id, args.message, preset_slug, artifact_ids, allowed_tools,
        args.system_prompt,
    )
    user_message_id = sent.get("messageId")
    log("Sent message %s to activity %d" % (user_message_id, activity_id))
    result = {
        "activityId": activity_id,
        "userMessageId": user_message_id,
        "executionActivityId": sent.get("executionActivityId"),
    }
    if args.no_wait:
        result["note"] = (
            "Not waiting. Poll with: messages --activity-id %d" % activity_id
        )
        emit(result)
        return
    reply = monitor_assistant(
        activity_id, user_message_id, args.timeout, args.poll_interval
    )
    content_limit = 1_000_000 if args.full else 4000
    result["assistant"] = summarize_message(reply, content_limit)
    result["outcome"] = reply.get("status")
    result["artifacts"] = resolve_generated_artifacts(reply)
    emit(result)


def command_create(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    activity = create_activity(
        workspace_id, args.title, args.description, args.preset
    )
    emit(summarize_activity(activity))


def command_messages(args):
    messages = api_get("/brain/activities/%d/messages" % args.activity_id)
    if args.role:
        messages = [m for m in messages if m.get("role") == args.role]
    content_limit = 1_000_000 if args.full else 4000
    emit([summarize_message(message, content_limit) for message in messages])


def command_activity(args):
    activity = api_get("/brain/activities/%d" % args.activity_id)
    emit(activity if args.full else summarize_activity(activity))


def command_list(args):
    workspace_id = resolve_workspace_id(args.workspace_id)
    path = "/brain/activities?workspaceId=%d" % workspace_id
    if args.status:
        path += "&status=%s" % args.status
    created_by = resolve_user_id(args.created_by)
    if created_by is not None:
        path += "&createdBy=%d" % created_by
    if args.search:
        path += "&search=%s" % urllib.request.quote(args.search)
    activities = api_get(path)
    emit([summarize_activity(activity) for activity in activities])


def command_presets(args):
    presets = api_get("/brain/presets")
    if args.full:
        emit(presets)
        return
    emit([
        {
            "id": preset.get("id"),
            "slug": preset.get("slug"),
            "name": preset.get("name"),
            "description": preset.get("description"),
            "isBuiltIn": preset.get("isBuiltIn"),
        }
        for preset in presets
    ])


def command_artifact(args):
    if args.export:
        body = {"format": args.export}
        emit(http_request(
            "POST", get_base_url() + "/brain/artifacts/%d/export" % args.id, body
        ))
        return
    suffix = "/meta" if args.meta else ""
    emit(api_get("/brain/artifacts/%d%s" % (args.id, suffix)))


def command_resources(args):
    emit(api_get("/brain/activities/%d/resources" % args.activity_id))


def command_pinned_artifacts(args):
    artifact_ids = api_get(
        "/brain/activities/%d/pinned-artifacts" % args.activity_id
    )
    result = {"activityId": args.activity_id, "artifactIds": artifact_ids}
    if args.resolve:
        result["artifacts"] = [
            summarize_artifact(api_get("/brain/artifacts/%d/meta" % int(aid)))
            for aid in (artifact_ids or [])
        ]
    emit(result)


def command_test_auth(args):
    token_source = (
        "CHIKABASE_USER_ACCESS_KEY"
        if os.environ.get("CHIKABASE_USER_ACCESS_KEY")
        else "CHIKABASE_USER_EMAIL/CHIKABASE_USER_PASS login"
    )
    get_token()
    workspace_id = resolve_workspace_id(args.workspace_id)
    presets = api_get("/brain/presets")
    emit({
        "ok": True,
        "baseUrl": get_base_url(),
        "authVia": token_source,
        "workspaceId": workspace_id,
        "visiblePresets": len(presets) if isinstance(presets, list) else None,
    })


def add_workspace_flag(parser):
    parser.add_argument(
        "--workspace-id", type=int,
        help="Workspace id (default: CHIKABASE_WORKSPACE_ID / login default)",
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


def add_full_flag(parser, help_text):
    parser.add_argument("--full", action="store_true", help=help_text)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="brain_activity.py",
        description="Drive Chika Brain conversations via the Chikabase API.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    chat_parser = subparsers.add_parser(
        "chat",
        help="Start (or continue) a conversation, send a message, watch the "
             "agent, and print its reply (the headline command)",
    )
    chat_parser.add_argument("--message", required=True, help="Message to send")
    chat_parser.add_argument(
        "--activity-id", type=int,
        help="Continue this activity instead of creating a new one",
    )
    chat_parser.add_argument(
        "--title", help="Title for the new activity (auto-generated if omitted)"
    )
    chat_parser.add_argument("--description", help="Description for a new activity")
    chat_parser.add_argument(
        "--preset", help="Preset slug (see the `presets` command)"
    )
    chat_parser.add_argument(
        "--artifact-ids",
        help="Comma-separated artifact ids to attach as context for this turn",
    )
    chat_parser.add_argument(
        "--allowed-tools",
        help="Comma-separated tool names to restrict this turn to",
    )
    chat_parser.add_argument(
        "--system-prompt", help="System-prompt override for this turn"
    )
    chat_parser.add_argument(
        "--no-wait", action="store_true",
        help="Send without monitoring; print the ids and return immediately",
    )
    add_full_flag(chat_parser, "Print the full assistant reply (no truncation)")
    add_workspace_flag(chat_parser)
    add_wait_flags(chat_parser)
    chat_parser.set_defaults(handler=command_chat)
    create_parser = subparsers.add_parser(
        "create", help="Create an empty activity (no message)"
    )
    create_parser.add_argument("--title")
    create_parser.add_argument("--description")
    create_parser.add_argument("--preset", help="Preset slug")
    add_workspace_flag(create_parser)
    create_parser.set_defaults(handler=command_create)
    messages_parser = subparsers.add_parser(
        "messages", help="Fetch the messages of an activity"
    )
    messages_parser.add_argument("--activity-id", type=int, required=True)
    messages_parser.add_argument(
        "--role", choices=["user", "assistant", "system"],
        help="Only messages with this role",
    )
    add_full_flag(messages_parser, "Print full message content (no truncation)")
    messages_parser.set_defaults(handler=command_messages)
    activity_parser = subparsers.add_parser(
        "activity", help="Get one activity's details"
    )
    activity_parser.add_argument("--activity-id", type=int, required=True)
    add_full_flag(activity_parser, "Print the raw activity (all fields)")
    activity_parser.set_defaults(handler=command_activity)
    list_parser = subparsers.add_parser(
        "list", help="List activities in a workspace"
    )
    list_parser.add_argument("--status", choices=["active", "archived", "draft"])
    list_parser.add_argument(
        "--created-by", metavar="USER",
        help="Only this user's activities: a numeric id, or an email/name "
             "substring resolved against the org (must be unique)",
    )
    list_parser.add_argument("--search", help="Filter by title/description text")
    add_workspace_flag(list_parser)
    list_parser.set_defaults(handler=command_list)
    presets_parser = subparsers.add_parser(
        "presets", help="List available presets (slug + name + description)"
    )
    add_full_flag(presets_parser, "Print full preset objects")
    presets_parser.set_defaults(handler=command_presets)
    artifact_parser = subparsers.add_parser(
        "artifact", help="Fetch an artifact by id"
    )
    artifact_parser.add_argument("--id", type=int, required=True)
    artifact_parser.add_argument(
        "--meta", action="store_true",
        help="Metadata only (title/type), without the content body",
    )
    artifact_parser.add_argument(
        "--export", choices=["raw", "json", "markdown"],
        help="Export the artifact in this format instead of the raw record",
    )
    artifact_parser.set_defaults(handler=command_artifact)
    resources_parser = subparsers.add_parser(
        "resources", help="List resources (files/APIs/searches) used in an activity"
    )
    resources_parser.add_argument("--activity-id", type=int, required=True)
    resources_parser.set_defaults(handler=command_resources)
    pinned_parser = subparsers.add_parser(
        "pinned-artifacts", help="List an activity's pinned artifact ids"
    )
    pinned_parser.add_argument("--activity-id", type=int, required=True)
    pinned_parser.add_argument(
        "--resolve", action="store_true",
        help="Also fetch each artifact's metadata (title/type)",
    )
    pinned_parser.set_defaults(handler=command_pinned_artifacts)
    auth_parser = subparsers.add_parser(
        "test-auth", help="Verify credentials and print the resolved context"
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
