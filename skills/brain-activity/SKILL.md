---
name: brain-activity
description: Drive Chika Brain conversations (activities) through the Chikabase REST API from an agent session. Use whenever the user wants to talk to the Brain agent programmatically — "start a Brain session and ask it to …", "open a new Brain conversation about withdrawals", "send this to the Brain agent and wait for its answer", "run a Brain preset against …" — or to pull an existing conversation back by id — "show me the messages in Brain activity 42", "get the assistant's last reply in conversation 42", "fetch artifact 591", "what artifacts did activity 42 produce", "list the Brain conversations in this workspace". The headline `chat` command creates (or continues) an activity, sends a message, and MONITORS the agent — polling until the assistant reply stops streaming while logging each status change and the files/searches/APIs it touches — then prints the reply plus any generated artifacts. Also lists presets and fetches conversations, messages, artifacts, resources, and pinned artifacts by id. Trigger on any mention of Brain conversations/activities/sessions, sending a message to the Brain agent, monitoring a Brain run, or retrieving Brain messages/artifacts by id.
---

# Brain Activity

Drive Chika Brain "activities" (AI conversations) via one Python CLI
(`scripts/brain_activity.py`): create a conversation, send a message, watch the
agent respond, and read conversations/messages/artifacts back by id.

A Brain reply is **asynchronous** — sending a message returns a user-message id
immediately and the assistant answer is produced in the background. The `chat`
command hides that: it polls until the assistant message leaves the `streaming`
state, so one call returns the finished reply.

## Auth

A bearer token is resolved in this order:

1. `CHIKABASE_USER_ACCESS_KEY` — injected in Chika Brain sessions; used as-is.
2. Else email/password login — set `CHIKABASE_USER_EMAIL` +
   `CHIKABASE_USER_PASS`; the CLI calls `POST /auth/login`, uses the returned
   token for the run, and adopts your default workspace.

Also: `CHIKABASE_BASE_URL` — the **server root** every route is concatenated
onto (`https://chikabase.payme.io/api` for the hosted proxy,
`http://localhost:3000` for a direct local backend; defaults to the latter) —
and `CHIKABASE_WORKSPACE_ID` (override per call with `--workspace-id`). No
third-party libraries. Check everything with:

```bash
python3 scripts/brain_activity.py test-auth
```

## Headline command — `chat`

Create-or-continue, then monitor. Give it a message; it creates a new activity
(or continues `--activity-id`), sends the message, watches the agent, and prints
the reply.

```bash
S=scripts/brain_activity.py

# New conversation with a preset; wait and print the full reply.
python3 $S chat --preset qa-analyst --full \
  --message "What are the most common withdrawal support questions?"

# Continue an existing conversation (threads onto its latest message).
python3 $S chat --activity-id 42 --message "Now group those by product area."

# Attach context artifacts and restrict tools for this turn.
python3 $S chat --activity-id 42 --artifact-ids 588,591 \
  --allowed-tools knowledge_search,sql_query \
  --message "Cross-check the numbers against artifact 591."

# Send without monitoring; just return the ids.
python3 $S chat --activity-id 42 --no-wait --message "Draft a summary."
```

Flow: creates the activity when `--activity-id` is omitted (`--preset` sets its
system prompt, `--title` skips the auto title) → sends the message → **monitors**
unless `--no-wait`, polling the messages and logging each status change and
resource bump to stderr, returning when the reply is `completed`/`error`/
`aborted` (timeout 900s; tune `--timeout`/`--poll-interval`) → prints JSON:
`activityId`, `userMessageId`, `outcome`, the `assistant` message, and generated
`artifacts`. Reply content truncates at 4000 chars unless `--full`.

## Read conversations back

```bash
python3 $S messages --activity-id 42                 # whole conversation
python3 $S messages --activity-id 42 --role assistant --full
python3 $S activity --activity-id 42                 # metadata (+ --full for raw)
python3 $S list --status active                      # workspace activities
python3 $S list --search withdrawals                 # free-text over title/description
python3 $S list --created-by omer@payme.io           # by user: email, name, or id
python3 $S list --created-by omer --search refund     # substrings resolve if unique; filters combine
python3 $S artifact --id 591 --meta                  # or: (raw) / --export markdown
python3 $S resources --activity-id 42                # files/APIs/searches touched
python3 $S pinned-artifacts --activity-id 42 --resolve
python3 $S presets                                   # slugs for --preset (--full for detail)
```

## Command summary

| Command | Description |
|---------|-------------|
| `chat --message <text>` | Create/continue a conversation, monitor the agent, print the reply |
| `create [--title] [--preset] [--description]` | Create an empty activity, no message |
| `messages --activity-id N [--role] [--full]` | A conversation's messages |
| `activity --activity-id N [--full]` | One activity's details |
| `list [--status] [--created-by id\|email\|name] [--search text] [--workspace-id]` | Activities in a workspace (filters combine; `--created-by` resolves a unique email/name) |
| `presets [--full]` | Available presets (slug + name + description) |
| `artifact --id N [--meta] [--export raw\|json\|markdown]` | Fetch an artifact |
| `resources --activity-id N` | Resources used in an activity |
| `pinned-artifacts --activity-id N [--resolve]` | Pinned artifact ids (+ meta) |
| `test-auth` | Verify credentials and print the resolved context |

## Errors

JSON on stdout; progress and errors on stderr with a non-zero exit.

- **no credentials** — export `CHIKABASE_USER_ACCESS_KEY`, or
  `CHIKABASE_USER_EMAIL`+`CHIKABASE_USER_PASS`.
- **no workspace id** — pass `--workspace-id` or set `CHIKABASE_WORKSPACE_ID`.
- **API error 401** — token expired or bad login; re-run in a live session.
- **Timed out … waiting for the assistant reply** — the agent is still running,
  not lost; re-check with `messages --activity-id N` or raise `--timeout`.
