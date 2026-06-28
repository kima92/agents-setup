---
name: zendesk
description: This skill enables interaction with the Zendesk Support REST API v2 for ticket and customer management. Use when the user wants to list, read, search, create, or update Zendesk tickets, read a ticket's conversation, post a public reply or internal note on a ticket, change ticket status/priority/assignee/tags, or look up and manage Zendesk users, organizations, and agent groups. Trigger this whenever the user mentions Zendesk, a support ticket, a ticket number, replying to a customer, ticket status/priority/assignee, an end-user or requester, a support organization, or agent groups — even if they don't name the API explicitly. For finding tickets or users across the whole account, prefer this skill's search over paging through lists one by one. This skill is non-destructive: it never deletes tickets, users, organizations, or groups.
---

# Zendesk Support

This skill provides tools for interacting with Zendesk Support's REST API v2,
focusing on the day-to-day support workflows an agent runs: triaging and reading
tickets, replying to customers (public comments) or leaving internal notes,
updating ticket fields, and looking up the users, organizations, and groups
behind a ticket.

It is deliberately **non-destructive** — it can create and update tickets,
users, and organizations, but it never deletes anything. The strongest "remove"
it does is clearing a field through an update (e.g. unassigning a ticket).

## Prerequisites

The following environment variables must be set:

- `ZENDESK_SUBDOMAIN` — your Zendesk subdomain, i.e. the `acme` in
  `https://acme.zendesk.com`. A full URL is also accepted; the subdomain is
  extracted from it.
- `ZENDESK_EMAIL` — the agent email address tied to the API token.
- `ZENDESK_API_TOKEN` — an API token (Admin Center → Apps and integrations →
  APIs → Zendesk API → enable Token access and add a token).

Authentication is standard Zendesk API-token basic auth: the username is
`{email}/token` and the password is the token. The curl idiom
`-u "$ZENDESK_EMAIL/token:$ZENDESK_API_TOKEN"` is exactly this — the script
builds the same credentials for you.

No third-party Python libraries are needed. The script talks to the API with
`curl` by default (capturing the HTTP status via `-w "%{http_code}"`) and
automatically falls back to Python's stdlib `urllib` when `curl` is unavailable.

## Names and emails instead of IDs — pass them directly

Zendesk's APIs work in numeric IDs, but you usually know a **name** or an
**email**. You don't need a separate lookup step: pass a value straight to the
relevant option and the script resolves it for you.

- `--assignee` / `--user` / `--requester` accept a numeric **id**, an **email**,
  or a **name**. Emails and names are resolved via `/users/search`; an exact
  email match wins, a single result is used directly, and an ambiguous name
  raises a clear error listing the candidates so you can pass an id or exact
  email instead.
- `--group` accepts a numeric **id** or a group **name** (resolved against the
  group list).
- `--organization` accepts a numeric **id** or an organization **name**
  (resolved via organization autocomplete).

Numeric values are always treated as IDs, so there's no ambiguity when you
already have one. When a name resolves, the lookup is silent on success and only
speaks up (with candidates) when it can't pick a single target.

## Available Commands

The `scripts/zendesk_api.py` script provides a CLI for Zendesk Support
operations. Execute it with Python 3:

```bash
python3 scripts/zendesk_api.py <command> [options]
```

### Ticket Commands

| Command | Description |
|---------|-------------|
| `list-tickets` | List tickets (sortable, paginated) |
| `get-ticket` | Get a single ticket by id |
| `get-comments` | Get a ticket's conversation (all comments) |
| `create-ticket` | Create a new ticket with a first comment |
| `update-ticket` | Update fields (status/priority/assignee/group/tags/subject) and/or add a comment |
| `add-comment` | Add a public reply or internal note to a ticket |

### Search Commands

| Command | Description |
|---------|-------------|
| `search` | Search tickets/users/orgs across the whole account with Zendesk search syntax |

### User Commands

| Command | Description |
|---------|-------------|
| `list-users` | List users (optionally filtered by role) |
| `get-user` | Get a user by id, email, or name |
| `search-users` | Search users by name, email, or query |
| `create-user` | Create an end-user, agent, or admin |
| `update-user` | Update a user's fields |

### Organization Commands

| Command | Description |
|---------|-------------|
| `list-organizations` | List organizations |
| `get-organization` | Get an organization by id or name |
| `create-organization` | Create an organization |

### Group Commands

| Command | Description |
|---------|-------------|
| `list-groups` | List agent groups |
| `get-group` | Get a group by id or name |

## Command Usage Examples

### List and read tickets

```bash
# Most recently updated tickets first
python3 scripts/zendesk_api.py list-tickets --sort-by updated_at --sort-order desc --per-page 25

# Next page: Zendesk returns a full 'next' URL; pass it back as --cursor
python3 scripts/zendesk_api.py list-tickets --cursor "https://acme.zendesk.com/api/v2/tickets.json?page=2"

# A single ticket and its full conversation
python3 scripts/zendesk_api.py get-ticket --ticket-id 12345
python3 scripts/zendesk_api.py get-comments --ticket-id 12345 --sort-order asc
```

### Create a ticket

```bash
# Minimal: subject + first comment
python3 scripts/zendesk_api.py create-ticket \
  --subject "Login fails after password reset" \
  --comment "Customer reports a 500 right after resetting their password."

# Triaged on creation, with requester resolved from an email and a group by name
python3 scripts/zendesk_api.py create-ticket \
  --subject "Refund request" \
  --comment "Customer wants a refund for order #5512." \
  --requester "customer@example.com" \
  --assignee "jane@acme.io" \
  --group "Billing" \
  --priority high --type task --tags "refund,billing"

# Long/multi-line comment from a file (use - for stdin)
python3 scripts/zendesk_api.py create-ticket --subject "Outage report" --comment-file /tmp/report.md
```

### Reply to a customer or leave an internal note

`add-comment` posts a **public reply by default** — it's what the customer sees.
Add `--internal` to leave a private note visible only to agents.

```bash
# Public reply
python3 scripts/zendesk_api.py add-comment --ticket-id 12345 \
  --text "Thanks for the details — we've reproduced this and a fix is on the way."

# Internal note (not visible to the requester)
python3 scripts/zendesk_api.py add-comment --ticket-id 12345 \
  --text "Root cause is the cache layer; looping in infra." --internal

# Long reply from a file or stdin
python3 scripts/zendesk_api.py add-comment --ticket-id 12345 --text-file /tmp/reply.md
```

### Update a ticket

Any field you omit is left unchanged. You can change fields and add a comment in
the same call — a comment added via `update-ticket` is also public unless you
pass `--internal`.

```bash
# Solve a ticket with a closing public reply
python3 scripts/zendesk_api.py update-ticket --ticket-id 12345 \
  --status solved --comment "Glad that worked — closing this out. Reopen anytime."

# Reassign and reprioritize (assignee/group resolved from email/name)
python3 scripts/zendesk_api.py update-ticket --ticket-id 12345 \
  --assignee "jane@acme.io" --group "Tier 2" --priority urgent

# Replace the ticket's tags
python3 scripts/zendesk_api.py update-ticket --ticket-id 12345 --tags "vip,escalated"
```

### Search

Search is the right tool for any "find every ticket/user that …" question — it
queries the whole account in one call instead of paging through lists. Use
Zendesk's search operators in `--query`.

```bash
# Open, high-priority tickets
python3 scripts/zendesk_api.py search --query "type:ticket status:open priority:high"

# Unassigned tickets in a group, newest first
python3 scripts/zendesk_api.py search --query "type:ticket assignee:none group:Billing" --sort-by created_at --sort-order desc

# Tickets a person requested, updated this month
python3 scripts/zendesk_api.py search --query "type:ticket requester:customer@example.com updated>2026-06-01"

# Find a user
python3 scripts/zendesk_api.py search --query "type:user jane@acme.io"
```

Common operators: `type:ticket|user|organization`, `status:`, `priority:`,
`assignee:`, `requester:`, `group:`, `tags:`, `organization:`, and date filters
like `created>2026-06-01`, `updated<2026-06-20`. `assignee:none` finds
unassigned tickets.

### Users, organizations, and groups

```bash
# Look up a user (id, email, or name all work)
python3 scripts/zendesk_api.py get-user --user "jane@acme.io"
python3 scripts/zendesk_api.py search-users --query "name:Jane"
python3 scripts/zendesk_api.py list-users --role agent --per-page 100

# Create / update users
python3 scripts/zendesk_api.py create-user --name "Sam Lee" --email "sam@example.com" --role end-user --organization "Acme Corp"
python3 scripts/zendesk_api.py update-user --user "sam@example.com" --phone "+1-555-0100" --notes "VIP"

# Organizations and groups
python3 scripts/zendesk_api.py list-organizations --per-page 50
python3 scripts/zendesk_api.py get-organization --organization "Acme Corp"
python3 scripts/zendesk_api.py create-organization --name "Globex" --domain-names "globex.com,globex.io"
python3 scripts/zendesk_api.py list-groups
python3 scripts/zendesk_api.py get-group --group "Billing"
```

## Workflow Guidelines

### Triaging and replying to a ticket

1. `get-ticket` to understand the request, then `get-comments` to read the
   full conversation so your reply has context.
2. Reply with `add-comment` (public) or leave an `--internal` note for
   teammates. Keep replies in the customer's voice; reserve `--internal` for
   things the customer shouldn't see.
3. Move the ticket along with `update-ticket` — set `status`, reassign with
   `--assignee`/`--group`, adjust `--priority`, or update `--tags`. You can
   bundle a closing reply and `--status solved` into one `update-ticket` call.

### Finding tickets or users across the account

Reach for `search` first — **don't** page through `list-tickets` and filter by
hand. One `search` call with the right operators (`status:`, `assignee:`,
`requester:`, `tags:`, date filters) covers the whole account, where list +
manual filtering is slow and easily misses anything past the fetch window.

### Working from a name or email

You rarely need a separate lookup step. Pass the email or name straight to
`--assignee`, `--requester`, `--user`, `--group`, or `--organization` and let
the script resolve it. Only fall back to an explicit id when a name is ambiguous
(the error lists the candidates).

## Error Handling

The script outputs JSON for successful operations and writes error messages to
stderr (with a non-zero exit code) for failures. Common cases:

- **Configuration Error: Missing required environment variable(s): …** — set
  `ZENDESK_SUBDOMAIN`, `ZENDESK_EMAIL`, and `ZENDESK_API_TOKEN`.
- **Zendesk API error (HTTP 401) …** — bad credentials, or token access isn't
  enabled in Admin Center. Verify the email/token and that the email is an
  active agent.
- **Zendesk API error (HTTP 403) …** — the agent lacks permission for that
  action (e.g. end-users can't update tickets).
- **Zendesk API error (HTTP 404) …** — wrong ticket/user/org id, or it's on a
  different subdomain.
- **Zendesk API error (HTTP 422) …** — invalid field value (e.g. an unknown
  status) or a validation failure; the message includes Zendesk's details.
- **Zendesk API error (HTTP 429) …** — rate limited. Zendesk returns a
  `Retry-After` header; wait and retry. Prefer `search` and bulk reads over
  many small calls to stay under the limit.
- **Ambiguous user/group/organization …** — a name matched more than one
  record; the error lists candidates. Pass a numeric id or exact email.

## Additional Reference

For detailed API endpoint documentation (paths, parameters, response shapes,
pagination, and search syntax), see `references/api_endpoints.md`.
