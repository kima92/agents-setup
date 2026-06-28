#!/usr/bin/env python3
"""
Zendesk Support REST API v2 CLI Tool

A command-line interface for interacting with Zendesk Support's REST API,
focusing on read and reply/update workflows: listing, reading and searching
tickets, reading and adding comments (public replies or internal notes),
updating ticket fields, and looking up users, organizations and groups.

This tool is intentionally non-destructive: it never deletes tickets, users,
organizations or groups. It can create tickets/users/organizations and update
them, but the only "remove" it performs is clearing a value via an update
(e.g. unassigning), never an outright delete.

HTTP transport: uses `curl` by default (capturing the status via
`-w "%{http_code}"`) and falls back to Python's stdlib `urllib` when curl is
not on PATH. No third-party libraries are required.

Authentication is HTTP basic auth, the standard Zendesk API-token scheme:
the username is "{email}/token" and the password is the API token. (curl's
documented `-u "$ZENDESK_EMAIL/token:$ZENDESK_API_TOKEN"` is exactly this.)

Environment Variables Required:
    ZENDESK_SUBDOMAIN  - Your Zendesk subdomain, i.e. the "acme" in
                         https://acme.zendesk.com . A full URL is also accepted
                         and the subdomain is extracted from it.
    ZENDESK_EMAIL      - The agent email address used for the API token.
    ZENDESK_API_TOKEN  - An API token (Admin Center > Apps and integrations >
                         APIs > Zendesk API > Token access).
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


# --- Errors ---

class ApiError(Exception):
    """Raised when the Zendesk API returns a non-2xx status."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class TransportError(Exception):
    """Raised when the request could not be sent at all (curl/urllib failure)."""


class ConfigurationError(Exception):
    """Raised when required configuration (env vars) is missing or invalid."""


# --- HTTP transport (curl by default, urllib fallback) ---

def _curl_cfg_quote(value):
    """Escape a value for use inside a double-quoted curl config entry."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _curl_request(method, url, headers, data, auth):
    """Perform a request with curl, returning (status_code, response_text).

    Credentials, headers, URL and body are passed via a temp config file
    (`-K`) and temp files so nothing sensitive leaks into the process argv.
    The HTTP status is captured with `-w "%{http_code}"`; a non-2xx status is
    returned to the caller rather than raised here.
    """
    username, token = auth
    cfg_fd, cfg_path = tempfile.mkstemp(prefix="zd-curl-", suffix=".cfg")
    out_fd, out_path = tempfile.mkstemp(prefix="zd-curl-", suffix=".out")
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
            data_fd, data_path = tempfile.mkstemp(prefix="zd-curl-", suffix=".dat")
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
        # Non-2xx responses surface here; return them like curl would.
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


# --- Client ---

class ZendeskClient:
    """Thin wrapper over the Zendesk Support REST API v2."""

    def __init__(self):
        subdomain = os.environ.get("ZENDESK_SUBDOMAIN", "").strip()
        email = os.environ.get("ZENDESK_EMAIL", "").strip()
        token = os.environ.get("ZENDESK_API_TOKEN", "").strip()

        missing = [name for name, val in (
            ("ZENDESK_SUBDOMAIN", subdomain),
            ("ZENDESK_EMAIL", email),
            ("ZENDESK_API_TOKEN", token),
        ) if not val]
        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): {}".format(", ".join(missing)))

        # Accept either a bare subdomain ("acme") or a full URL and normalize.
        m = re.search(r"https?://([^.]+)\.zendesk\.com", subdomain)
        if m:
            subdomain = m.group(1)
        subdomain = subdomain.replace(".zendesk.com", "")

        self.subdomain = subdomain
        self.base_url = "https://{}.zendesk.com/api/v2".format(subdomain)
        # Zendesk API-token auth: username is "{email}/token", password is the token.
        self.auth = ("{}/token".format(email), token)

    # -- low-level --

    def _url(self, path, params=None):
        url = "{}{}".format(self.base_url, path if path.startswith("/") else "/" + path)
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def _request(self, method, path, params=None, body=None):
        url = path if path.startswith("http") else self._url(path, params)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body)
        status, text = http_request(method, url, headers=headers, data=data, auth=self.auth)

        if status == 204 or not text:
            payload = {}
        else:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {"raw": text}

        if status < 200 or status >= 300:
            detail = ""
            if isinstance(payload, dict):
                detail = payload.get("error") or payload.get("description") or ""
                if isinstance(detail, dict):
                    detail = detail.get("message") or json.dumps(detail)
                if not detail and payload.get("details"):
                    detail = json.dumps(payload["details"])
            raise ApiError(
                "Zendesk API error (HTTP {}) on {} {}: {}".format(
                    status, method, url, detail or text[:500]),
                status_code=status)
        return payload

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, body):
        return self._request("POST", path, body=body)

    def _put(self, path, body):
        return self._request("PUT", path, body=body)

    # -- resolution helpers (names/emails -> ids) --

    def resolve_user_id(self, value):
        """Resolve a user reference to a numeric id.

        Accepts a numeric id (returned as-is), an email, or a name. Emails and
        names are looked up via /users/search; an exact email match wins, else
        the single result is used, else an ambiguity error is raised.
        """
        if value is None:
            return None
        s = str(value).strip()
        if s.isdigit():
            return int(s)
        data = self._get("/users/search.json", {"query": s})
        users = data.get("users", [])
        if not users:
            raise ApiError("No Zendesk user found matching {!r}".format(s))
        if "@" in s:
            exact = [u for u in users if (u.get("email") or "").lower() == s.lower()]
            if exact:
                return exact[0]["id"]
        if len(users) == 1:
            return users[0]["id"]
        names = ", ".join("{} <{}> (id {})".format(u.get("name"), u.get("email"), u.get("id"))
                          for u in users[:8])
        raise ApiError("Ambiguous user {!r}; matches: {}. Pass a numeric id or exact email."
                       .format(s, names))

    def resolve_group_id(self, value):
        """Resolve a group reference (numeric id or name) to a numeric id."""
        if value is None:
            return None
        s = str(value).strip()
        if s.isdigit():
            return int(s)
        data = self._get("/groups.json", {"per_page": 100})
        groups = data.get("groups", [])
        matches = [g for g in groups if (g.get("name") or "").lower() == s.lower()]
        if not matches:
            partial = [g for g in groups if s.lower() in (g.get("name") or "").lower()]
            matches = partial
        if not matches:
            raise ApiError("No Zendesk group found matching {!r}".format(s))
        if len(matches) > 1:
            names = ", ".join("{} (id {})".format(g.get("name"), g.get("id")) for g in matches[:8])
            raise ApiError("Ambiguous group {!r}; matches: {}.".format(s, names))
        return matches[0]["id"]

    # -- tickets --

    def list_tickets(self, sort_by=None, sort_order=None, per_page=None, page=None, cursor=None):
        if cursor:
            return self._get(cursor)
        params = {
            "sort_by": sort_by,
            "sort_order": sort_order,
            "per_page": per_page,
            "page": page,
        }
        return self._get("/tickets.json", params)

    def get_ticket(self, ticket_id):
        return self._get("/tickets/{}.json".format(ticket_id))

    def get_ticket_comments(self, ticket_id, sort_order=None, per_page=None):
        params = {"sort_order": sort_order, "per_page": per_page}
        return self._get("/tickets/{}/comments.json".format(ticket_id), params)

    def create_ticket(self, subject, comment_body, comment_public=True,
                      requester=None, priority=None, ticket_type=None,
                      assignee=None, group=None, tags=None, status=None):
        comment = {"body": comment_body, "public": comment_public}
        ticket = {"subject": subject, "comment": comment}
        if priority:
            ticket["priority"] = priority
        if ticket_type:
            ticket["type"] = ticket_type
        if status:
            ticket["status"] = status
        if tags is not None:
            ticket["tags"] = tags
        if assignee is not None:
            ticket["assignee_id"] = self.resolve_user_id(assignee)
        if group is not None:
            ticket["group_id"] = self.resolve_group_id(group)
        if requester is not None:
            # requester_id when numeric/known; otherwise let an email create the requester.
            r = str(requester).strip()
            if "@" in r and not r.isdigit():
                ticket["requester"] = {"email": r}
            else:
                ticket["requester_id"] = self.resolve_user_id(r)
        return self._post("/tickets.json", {"ticket": ticket})

    def update_ticket(self, ticket_id, status=None, priority=None, ticket_type=None,
                      subject=None, assignee=None, group=None, tags=None,
                      comment_body=None, comment_public=True):
        ticket = {}
        if status:
            ticket["status"] = status
        if priority:
            ticket["priority"] = priority
        if ticket_type:
            ticket["type"] = ticket_type
        if subject:
            ticket["subject"] = subject
        if tags is not None:
            ticket["tags"] = tags
        if assignee is not None:
            ticket["assignee_id"] = self.resolve_user_id(assignee)
        if group is not None:
            ticket["group_id"] = self.resolve_group_id(group)
        if comment_body is not None:
            ticket["comment"] = {"body": comment_body, "public": comment_public}
        if not ticket:
            raise ApiError("update-ticket: nothing to update; pass at least one field.")
        return self._put("/tickets/{}.json".format(ticket_id), {"ticket": ticket})

    def add_comment(self, ticket_id, body, public=True):
        # In Zendesk a comment is added by updating the ticket with a comment.
        return self._put("/tickets/{}.json".format(ticket_id),
                         {"ticket": {"comment": {"body": body, "public": public}}})

    # -- search --

    def search(self, query, sort_by=None, sort_order=None, per_page=None, page=None):
        params = {
            "query": query,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "per_page": per_page,
            "page": page,
        }
        return self._get("/search.json", params)

    # -- users --

    def list_users(self, role=None, per_page=None, page=None):
        params = {"role[]": role, "per_page": per_page, "page": page}
        # role[] only when provided
        if role is None:
            params.pop("role[]")
        return self._get("/users.json", params)

    def get_user(self, user):
        uid = self.resolve_user_id(user)
        return self._get("/users/{}.json".format(uid))

    def search_users(self, query, per_page=None, page=None):
        params = {"query": query, "per_page": per_page, "page": page}
        return self._get("/users/search.json", params)

    def create_user(self, name, email, role=None, organization=None, verified=None):
        user = {"name": name, "email": email}
        if role:
            user["role"] = role
        if verified is not None:
            user["verified"] = verified
        if organization is not None:
            user["organization_id"] = self.resolve_organization_id(organization)
        return self._post("/users.json", {"user": user})

    def update_user(self, user, name=None, email=None, role=None, organization=None,
                    phone=None, notes=None):
        uid = self.resolve_user_id(user)
        body = {}
        if name:
            body["name"] = name
        if email:
            body["email"] = email
        if role:
            body["role"] = role
        if phone:
            body["phone"] = phone
        if notes is not None:
            body["notes"] = notes
        if organization is not None:
            body["organization_id"] = self.resolve_organization_id(organization)
        if not body:
            raise ApiError("update-user: nothing to update; pass at least one field.")
        return self._put("/users/{}.json".format(uid), {"user": body})

    # -- organizations --

    def resolve_organization_id(self, value):
        if value is None:
            return None
        s = str(value).strip()
        if s.isdigit():
            return int(s)
        data = self._get("/organizations/autocomplete.json", {"name": s})
        orgs = data.get("organizations", [])
        if not orgs:
            raise ApiError("No Zendesk organization found matching {!r}".format(s))
        exact = [o for o in orgs if (o.get("name") or "").lower() == s.lower()]
        if exact:
            return exact[0]["id"]
        if len(orgs) == 1:
            return orgs[0]["id"]
        names = ", ".join("{} (id {})".format(o.get("name"), o.get("id")) for o in orgs[:8])
        raise ApiError("Ambiguous organization {!r}; matches: {}.".format(s, names))

    def list_organizations(self, per_page=None, page=None):
        return self._get("/organizations.json", {"per_page": per_page, "page": page})

    def get_organization(self, organization):
        oid = self.resolve_organization_id(organization)
        return self._get("/organizations/{}.json".format(oid))

    def create_organization(self, name, domain_names=None, notes=None, details=None):
        org = {"name": name}
        if domain_names is not None:
            org["domain_names"] = domain_names
        if notes is not None:
            org["notes"] = notes
        if details is not None:
            org["details"] = details
        return self._post("/organizations.json", {"organization": org})

    # -- groups --

    def list_groups(self, per_page=None, page=None):
        return self._get("/groups.json", {"per_page": per_page, "page": page})

    def get_group(self, group):
        gid = self.resolve_group_id(group)
        return self._get("/groups/{}.json".format(gid))


# --- CLI ---

def output_json(data):
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def resolve_text(direct, from_file):
    """Return text from --x or --x-file (use '-' for stdin)."""
    if from_file is not None:
        if from_file == "-":
            return sys.stdin.read()
        with open(from_file, "r", encoding="utf-8") as f:
            return f.read()
    return direct


def split_tags(value):
    if value is None:
        return None
    return [t.strip() for t in value.split(",") if t.strip()]


def comment_visibility(args):
    """public unless --internal/--private is passed."""
    return not getattr(args, "internal", False)


def build_parser():
    parser = argparse.ArgumentParser(description="Zendesk Support REST API v2 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # tickets
    p = sub.add_parser("list-tickets", help="List tickets")
    p.add_argument("--sort-by", choices=["created_at", "updated_at", "priority", "status", "ticket_type"])
    p.add_argument("--sort-order", choices=["asc", "desc"])
    p.add_argument("--per-page", type=int)
    p.add_argument("--page", type=int)
    p.add_argument("--cursor", help="A full 'next' URL returned by a previous page")

    p = sub.add_parser("get-ticket", help="Get a single ticket by id")
    p.add_argument("--ticket-id", required=True)

    p = sub.add_parser("get-comments", help="Get the conversation (comments) of a ticket")
    p.add_argument("--ticket-id", required=True)
    p.add_argument("--sort-order", choices=["asc", "desc"])
    p.add_argument("--per-page", type=int)

    p = sub.add_parser("create-ticket", help="Create a new ticket")
    p.add_argument("--subject", required=True)
    p.add_argument("--comment", help="First comment body (required unless --comment-file)")
    p.add_argument("--comment-file", help="Read the comment body from a file ('-' for stdin)")
    p.add_argument("--internal", action="store_true", help="Make the first comment a private note")
    p.add_argument("--requester", help="Requester email, name, or numeric id")
    p.add_argument("--assignee", help="Assignee email, name, or numeric id")
    p.add_argument("--group", help="Group name or numeric id")
    p.add_argument("--priority", choices=["low", "normal", "high", "urgent"])
    p.add_argument("--type", dest="ticket_type", choices=["problem", "incident", "question", "task"])
    p.add_argument("--status", choices=["new", "open", "pending", "hold", "solved", "closed"])
    p.add_argument("--tags", help="Comma-separated tags")

    p = sub.add_parser("update-ticket", help="Update ticket fields and/or add a comment")
    p.add_argument("--ticket-id", required=True)
    p.add_argument("--status", choices=["new", "open", "pending", "hold", "solved", "closed"])
    p.add_argument("--priority", choices=["low", "normal", "high", "urgent"])
    p.add_argument("--type", dest="ticket_type", choices=["problem", "incident", "question", "task"])
    p.add_argument("--subject")
    p.add_argument("--assignee", help="Assignee email, name, or numeric id")
    p.add_argument("--group", help="Group name or numeric id")
    p.add_argument("--tags", help="Comma-separated tags (replaces existing tags)")
    p.add_argument("--comment", help="Add a comment as part of the update")
    p.add_argument("--comment-file", help="Read the comment body from a file ('-' for stdin)")
    p.add_argument("--internal", action="store_true", help="Make the comment a private note")

    p = sub.add_parser("add-comment", help="Add a public reply (or internal note) to a ticket")
    p.add_argument("--ticket-id", required=True)
    p.add_argument("--text", help="Comment body (required unless --text-file)")
    p.add_argument("--text-file", help="Read the comment body from a file ('-' for stdin)")
    p.add_argument("--internal", action="store_true", help="Post as a private internal note")

    # search
    p = sub.add_parser("search", help="Search tickets/users/orgs with Zendesk search syntax")
    p.add_argument("--query", required=True,
                   help="e.g. 'type:ticket status:open priority:high' or free text")
    p.add_argument("--sort-by", choices=["created_at", "updated_at", "priority", "status", "ticket_type", "relevance"])
    p.add_argument("--sort-order", choices=["asc", "desc"])
    p.add_argument("--per-page", type=int)
    p.add_argument("--page", type=int)

    # users
    p = sub.add_parser("list-users", help="List users")
    p.add_argument("--role", choices=["end-user", "agent", "admin"])
    p.add_argument("--per-page", type=int)
    p.add_argument("--page", type=int)

    p = sub.add_parser("get-user", help="Get a user by id, email, or name")
    p.add_argument("--user", required=True)

    p = sub.add_parser("search-users", help="Search users by name, email, or query")
    p.add_argument("--query", required=True)
    p.add_argument("--per-page", type=int)
    p.add_argument("--page", type=int)

    p = sub.add_parser("create-user", help="Create a user")
    p.add_argument("--name", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--role", choices=["end-user", "agent", "admin"])
    p.add_argument("--organization", help="Organization name or numeric id")
    p.add_argument("--verified", action="store_true", help="Mark the email as verified")

    p = sub.add_parser("update-user", help="Update a user's fields")
    p.add_argument("--user", required=True, help="Target user: id, email, or name")
    p.add_argument("--name")
    p.add_argument("--email")
    p.add_argument("--role", choices=["end-user", "agent", "admin"])
    p.add_argument("--organization", help="Organization name or numeric id")
    p.add_argument("--phone")
    p.add_argument("--notes")

    # organizations
    p = sub.add_parser("list-organizations", help="List organizations")
    p.add_argument("--per-page", type=int)
    p.add_argument("--page", type=int)

    p = sub.add_parser("get-organization", help="Get an organization by id or name")
    p.add_argument("--organization", required=True)

    p = sub.add_parser("create-organization", help="Create an organization")
    p.add_argument("--name", required=True)
    p.add_argument("--domain-names", help="Comma-separated domain names")
    p.add_argument("--notes")
    p.add_argument("--details")

    # groups
    p = sub.add_parser("list-groups", help="List agent groups")
    p.add_argument("--per-page", type=int)
    p.add_argument("--page", type=int)

    p = sub.add_parser("get-group", help="Get a group by id or name")
    p.add_argument("--group", required=True)

    return parser


def dispatch(client, args):
    cmd = args.command

    if cmd == "list-tickets":
        return client.list_tickets(args.sort_by, args.sort_order, args.per_page, args.page, args.cursor)

    if cmd == "get-ticket":
        return client.get_ticket(args.ticket_id)

    if cmd == "get-comments":
        return client.get_ticket_comments(args.ticket_id, args.sort_order, args.per_page)

    if cmd == "create-ticket":
        body = resolve_text(args.comment, args.comment_file)
        if not body:
            raise ConfigurationError("create-ticket requires --comment or --comment-file.")
        return client.create_ticket(
            subject=args.subject, comment_body=body, comment_public=comment_visibility(args),
            requester=args.requester, priority=args.priority, ticket_type=args.ticket_type,
            assignee=args.assignee, group=args.group, tags=split_tags(args.tags), status=args.status)

    if cmd == "update-ticket":
        body = resolve_text(args.comment, args.comment_file)
        return client.update_ticket(
            args.ticket_id, status=args.status, priority=args.priority,
            ticket_type=args.ticket_type, subject=args.subject, assignee=args.assignee,
            group=args.group, tags=split_tags(args.tags), comment_body=body,
            comment_public=comment_visibility(args))

    if cmd == "add-comment":
        text = resolve_text(args.text, args.text_file)
        if not text:
            raise ConfigurationError("add-comment requires --text or --text-file.")
        return client.add_comment(args.ticket_id, text, public=comment_visibility(args))

    if cmd == "search":
        return client.search(args.query, args.sort_by, args.sort_order, args.per_page, args.page)

    if cmd == "list-users":
        return client.list_users(args.role, args.per_page, args.page)
    if cmd == "get-user":
        return client.get_user(args.user)
    if cmd == "search-users":
        return client.search_users(args.query, args.per_page, args.page)
    if cmd == "create-user":
        return client.create_user(args.name, args.email, role=args.role,
                                  organization=args.organization,
                                  verified=True if args.verified else None)
    if cmd == "update-user":
        return client.update_user(args.user, name=args.name, email=args.email, role=args.role,
                                  organization=args.organization, phone=args.phone, notes=args.notes)

    if cmd == "list-organizations":
        return client.list_organizations(args.per_page, args.page)
    if cmd == "get-organization":
        return client.get_organization(args.organization)
    if cmd == "create-organization":
        return client.create_organization(args.name, domain_names=split_tags(args.domain_names),
                                          notes=args.notes, details=args.details)

    if cmd == "list-groups":
        return client.list_groups(args.per_page, args.page)
    if cmd == "get-group":
        return client.get_group(args.group)

    raise ConfigurationError("Unknown command: {}".format(cmd))


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        client = ZendeskClient()
        result = dispatch(client, args)
        output_json(result)
    except ConfigurationError as e:
        sys.stderr.write("Configuration Error: {}\n".format(e))
        sys.exit(2)
    except ApiError as e:
        sys.stderr.write("{}\n".format(e))
        sys.exit(1)
    except TransportError as e:
        sys.stderr.write("Transport Error: {}\n".format(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
