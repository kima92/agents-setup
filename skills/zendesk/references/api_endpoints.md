# Zendesk Support REST API v2 — Endpoint Reference

This is a focused reference for the endpoints the `zendesk_api.py` script uses.
The full Zendesk API docs live at <https://developer.zendesk.com/api-reference/>.

## Base URL and auth

```
https://{subdomain}.zendesk.com/api/v2
```

Authentication is HTTP basic auth using API-token credentials:

- username: `{email}/token`
- password: `{api_token}`

(Equivalent to `curl -u "$ZENDESK_EMAIL/token:$ZENDESK_API_TOKEN"`.) Token
access must be enabled in Admin Center → Apps and integrations → APIs → Zendesk
API.

Unlike Slack, Zendesk uses real HTTP status codes: 2xx is success, and the
script raises on anything else, surfacing the status plus Zendesk's `error` /
`description` / `details` message.

## Table of contents

- [Tickets](#tickets)
- [Ticket comments](#ticket-comments)
- [Search](#search)
- [Users](#users)
- [Organizations](#organizations)
- [Groups](#groups)
- [Pagination](#pagination)
- [Rate limiting](#rate-limiting)

## Tickets

| Method | Path | Notes |
|--------|------|-------|
| GET | `/tickets.json` | List tickets. Query: `sort_by` (created_at, updated_at, priority, status, ticket_type), `sort_order` (asc/desc), `per_page`, `page`. |
| GET | `/tickets/{id}.json` | Single ticket. Returns `{ "ticket": {...} }`. |
| POST | `/tickets.json` | Create. Body: `{ "ticket": { "subject", "comment": {"body","public"}, ... } }`. |
| PUT | `/tickets/{id}.json` | Update. Body: `{ "ticket": { ...changed fields... } }`. Adding a `comment` object appends a comment. |

Common writable ticket fields: `subject`, `status` (new/open/pending/hold/
solved/closed), `priority` (low/normal/high/urgent), `type` (problem/incident/
question/task), `assignee_id`, `group_id`, `requester_id` (or `requester`:
`{email, name}` to create one on the fly), `tags` (array — **replaces** existing
tags), and `comment` (`{ "body": "...", "public": true|false }`).

A ticket comment is created **by updating the ticket** with a `comment` object —
there is no standalone "add comment" endpoint. `public: true` is a customer-
visible reply; `public: false` is an internal note.

## Ticket comments

| Method | Path | Notes |
|--------|------|-------|
| GET | `/tickets/{id}/comments.json` | Full conversation. Query: `sort_order` (asc/desc), `per_page`. Each item has `body`, `html_body`, `public`, `author_id`, `created_at`, `attachments`. |

## Search

| Method | Path | Notes |
|--------|------|-------|
| GET | `/search.json` | Unified search. Query: `query` (required), `sort_by`, `sort_order`, `per_page`, `page`. Results in `results[]`, each with a `result_type` (ticket/user/organization/group). |

Search query operators (AND-ed when space-separated):

- `type:ticket` / `type:user` / `type:organization`
- `status:open`, `priority:high`, `ticket_type:incident`
- `assignee:jane@acme.io`, `assignee:none` (unassigned), `requester:…`, `submitter:…`
- `group:Billing`, `organization:"Acme Corp"`, `tags:refund`
- Dates: `created>2026-06-01`, `updated<2026-06-20`, `solved>2026-01-01`
- Free text matches subject/description/comments.

Example: `type:ticket status:open priority:high assignee:none group:Billing`.

## Users

| Method | Path | Notes |
|--------|------|-------|
| GET | `/users.json` | List. Query: `role[]` (end-user/agent/admin), `per_page`, `page`. |
| GET | `/users/{id}.json` | Single user. |
| GET | `/users/search.json` | Query `query` = email, name, or a search expression. Used internally to resolve `--assignee`/`--user`/`--requester` names and emails to ids. |
| POST | `/users.json` | Create. Body: `{ "user": { "name", "email", "role", "organization_id", "verified" } }`. |
| PUT | `/users/{id}.json` | Update. Body: `{ "user": { ...changed fields... } }`. |

## Organizations

| Method | Path | Notes |
|--------|------|-------|
| GET | `/organizations.json` | List. Query: `per_page`, `page`. |
| GET | `/organizations/{id}.json` | Single organization. |
| GET | `/organizations/autocomplete.json` | Query `name` — used to resolve `--organization` names to ids. |
| POST | `/organizations.json` | Create. Body: `{ "organization": { "name", "domain_names":[…], "notes", "details" } }`. |

## Groups

| Method | Path | Notes |
|--------|------|-------|
| GET | `/groups.json` | List agent groups. Used to resolve `--group` names to ids. |
| GET | `/groups/{id}.json` | Single group. |

## Pagination

Newer list endpoints support both offset pagination (`page` / `per_page`, max
100) and cursor pagination. `list-tickets` exposes the simplest path: when more
results exist, Zendesk includes a `next_page` URL — pass it back verbatim as
`--cursor` to fetch the next page. `search` exposes `next_page`/`previous_page`
and a `count`; use `--page` to walk further.

## Rate limiting

Zendesk enforces per-minute request caps that vary by plan (often a few hundred
to a few thousand requests/minute). When you exceed it you get **HTTP 429** with
a `Retry-After` header (seconds to wait). To stay under the limit, prefer one
`search` call over looping reads, and batch your reasoning rather than making
many tiny calls.
