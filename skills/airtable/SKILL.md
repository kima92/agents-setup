---
name: airtable
description: Read and write Airtable bases through the Airtable Web API as a self-contained Python CLI (stdlib only, no pip install) — discover bases and table schema, filter/sort/export records, read record comments, and, when writes are enabled, create, update, and upsert records. Use whenever the user mentions Airtable, an Airtable base/table/view/record, a pasted airtable.com URL, or an app.../tbl.../rec... id — e.g. "what's in my Airtable CRM", "find the Todo rows in the Tasks table", "export this Airtable view to CSV", "who owns the overdue items in the base", "add these leads to Airtable", "sync this list into my base without creating duplicates". Trigger it too when someone asks to look something up in, report on, or push rows into "the base" / "the tracker" / "the Airtable sheet" in a context where Airtable is the system of record, even if they never name the API. Prefer this skill's filtering and pagination over hand-rolled curl — it handles filterByFormula quoting, URL-encoding, offset pagination, 10-record write batches, and the 5 requests/second rate limit for you. Non-destructive — it has no delete command at all — and read-only by default, refusing every create/update/upsert/comment command unless AIRTABLE_READ_ONLY is set to 0. Not for Notion, Google Sheets, Coda, or Smartsheet.
---

# Airtable

Drives the [Airtable Web API](https://airtable.com/developers/web/api/introduction)
(`https://api.airtable.com/v0`) from one dependency-free Python CLI
(`scripts/airtable_api.py`). It follows the same conventions as the sibling
`zendesk`/`testrail` skills: credentials never touch argv, JSON on stdout,
errors on stderr, Python standard library only.

| Command | What it does |
|---|---|
| `whoami` | Confirm the token works; show its user id and scopes |
| `list-bases` | Every base the token can see |
| `schema` | A base's tables, fields, types, select options and views |
| `list-records` | Filter, sort and page through records (text / JSON / CSV) |
| `get-record` | One record by id |
| `list-comments` | The comment thread on a record |
| `create-records` | **[write]** Create one or many records |
| `update-records` | **[write]** Update records by id |
| `upsert-records` | **[write]** Create-or-update, matched on field values |
| `add-comment` | **[write]** Post a comment on a record |

There is deliberately **no delete command**. Airtable's delete endpoints are
permanent and have no undo through the API, so the capability is absent rather
than merely guarded — this skill cannot destroy someone's data. If a user
genuinely needs rows gone, tell them to do it in the Airtable UI, where there's
a trash bin and a restore path.

## Auth (read this first)

Every call needs a **personal access token (PAT)**, created at
<https://airtable.com/create/tokens>. Tokens start with `pat`. Legacy `key...`
API keys were deprecated in February 2024 and no longer work; the CLI detects
one and says so.

```bash
export AIRTABLE_API_KEY="pat..."        # AIRTABLE_PAT / AIRTABLE_TOKEN also accepted
export AIRTABLE_BASE_ID="app..."        # optional: lets you omit --base
```

A PAT carries two independent things, and confusing them causes most Airtable
auth pain:

1. **Scopes** — what kinds of operation are allowed. The useful minimum is
   `data.records:read`, plus `schema.bases:read` to list bases and read schema,
   plus `data.records:write` for any write and `data.recordComments:read` /
   `data.recordComments:write` for comments.
2. **Access** — *which bases* the token may touch, picked from a list in the
   same UI. This is the one people forget. A token with perfect scopes returns
   **403 on a base that isn't in its Access list**, which reads like an auth
   failure but isn't. When a 403 hits one base while another works, that's the
   cause; the CLI says so in the error.

`whoami` is the cheapest way to check a token is live — it needs no scopes at
all. Never ask the user to paste the token into a shell command where it lands
in history; have them export it or put it in their shell profile.

## Read-only mode (the default)

Unless `AIRTABLE_READ_ONLY` is explicitly set to `0` (or `false`/`no`/`off`),
every write command is refused before it does anything, and the transport layer
independently blocks mutating requests. Refusals go to stderr with exit code
**3**. Any unrecognized value leaves read-only **on**, so a typo fails safe.

```bash
# Reads: always fine
python3 .claude/skills/airtable/scripts/airtable_api.py list-records --table Tasks

# Writes: enable them explicitly for the command (or export for the session)
AIRTABLE_READ_ONLY=0 python3 .claude/skills/airtable/scripts/airtable_api.py \
  create-records --table Tasks --field "Name=Ship the docs"
```

When a user asks for a write and the run is read-only, don't silently skip the
work: report that read-only mode blocked it and tell them to set
`AIRTABLE_READ_ONLY=0`. Don't flip the variable yourself unless they've asked
for the write to actually go through — an Airtable base is usually shared with
a whole team, so an unexpected row is someone else's problem to clean up.

## Running

```bash
python3 scripts/airtable_api.py <command> [options]
```

When installed, the path is typically
`.claude/skills/airtable/scripts/airtable_api.py`.

Exit codes: `0` ok, `1` API/transport error, `2` bad configuration or arguments,
`3` blocked by read-only mode.

## Pasted URLs work everywhere

People copy the browser URL rather than hunting for ids. Any `--base`,
`--table` or `--record` accepts a full `airtable.com` URL and takes the id it
needs out of it, so the same pasted string can fill all three:

```bash
URL="https://airtable.com/appSW9R5uCNmRmfl6/tbltp8DGLhqbUmjK1/viwkWFQ1nPBcJkEXs/rec5dNAPMJ1fcUKPX"
python3 scripts/airtable_api.py get-record --base "$URL" --table "$URL" --record "$URL"
```

`--base` also accepts a base **name** (resolved via `list-bases`), and `--table`
accepts a table name directly — Airtable itself resolves table names, so no
extra lookup is spent on reads.

## Orient before you act

Field names in Airtable are free text chosen by whoever built the base —
`Status` vs `status` vs `Stage`, `Owner` vs `Assigned To`. Guessing produces
`UNKNOWN_FIELD_NAME`, or worse, a filter that silently matches nothing and
reads as "there are no results". So unless you already know the base:

```bash
python3 scripts/airtable_api.py list-bases
python3 scripts/airtable_api.py schema --base "Sales CRM"
```

`schema` is the high-value call. It prints, per table, every field with its
type, the exact choices a single/multi-select allows, which fields are computed
(and therefore unwritable), and the available views:

```
Tasks (tbltp8DGLhqbUmjK1)  -  4 field(s), primary: Name
  Name     singleLineText
  Status   singleSelect          [Todo, In progress, Done]
  Owner    multipleRecordLinks   -> tblPeopleXXXXXXXX
  Created  createdTime           (computed, read-only)
  views: Grid view, By owner
```

Read that output before writing a filter or a record, and quote the exact
strings it gives you.

## Finding records

`list-records` is the workhorse. `--where` builds the filter for you, with
quoting and URL-encoding handled, so you rarely need to write Airtable formula
syntax by hand:

```bash
# One condition
python3 scripts/airtable_api.py list-records --table Tasks --where "Status=Todo"

# Several conditions are AND'd; --fields trims the output to what you need
python3 scripts/airtable_api.py list-records --table Tasks \
  --where "Status=Todo" --where "Priority=High" \
  --fields "Name,Owner,Due" --sort "Due" --max-records 50
```

`--where` operators:

| Form | Meaning |
|---|---|
| `Field=Value` | equals (dates compare by day, not timestamp) |
| `Field=` | is empty |
| `Field!=Value` | not equal (`Field!=` means "is not empty") |
| `Field~Value` | contains, case-insensitive |
| `Field>Value` `>=` `<` `<=` | greater/less than; numeric or date-aware |

Dates are handled specially because the naive version fails quietly: a date
written `2026-01-01` is compared with Airtable's `IS_SAME`/`IS_AFTER`/`IS_BEFORE`
rather than `=`/`>`/`<`, since a datetime cell carries a time and would never
equal a bare date.

For anything `--where` can't express — `OR`, nested logic, `DATETIME_DIFF`,
regex — pass raw Airtable formula syntax to `--formula` (it's AND'd with any
`--where` clauses). `references/formulas.md` is a cookbook of the patterns worth
knowing.

```bash
python3 scripts/airtable_api.py list-records --table Tasks \
  --formula "OR({Owner}='alice', {Owner}='bob')" --where "Status!=Done"
```

**Prefer a view when one fits.** If the base already has a "My open items" view,
`--view "My open items"` applies its saved filter and sort server-side and is
both cheaper and truer to what the user sees on screen than a reconstructed
filter.

### Output shape

`--format text` (default) prints an aligned table — compact and scannable,
which is what you want when summarizing for a person. `--format json` gives the
raw API response for further processing. `--format csv` writes CSV to stdout,
which is the right answer to "export this":

```bash
python3 scripts/airtable_api.py list-records --table Tasks --view "Q3 pipeline" \
  --all --format csv > pipeline.csv
```

### Pagination

Airtable returns at most 100 records per page. By default `list-records` fetches
**one page** — pulling a 50,000-row base into context by accident helps nobody.
Ask for more explicitly with `--max-records N` (stops at N, paginating as
needed) or `--all` (follows pagination to the end). The text output tells you
when more pages remain.

## Writing records

All writes need `AIRTABLE_READ_ONLY=0`. Before sending, the CLI fetches the
table schema and rejects unknown field names (suggesting the closest real one)
and computed fields — a rejected write is much cheaper than a batch that
half-applied.

Three ways to supply values, for three different needs:

```bash
# --field for plain text values
... create-records --table Tasks --field "Name=Ship the docs" --field "Status=Todo"

# --field-json when the value is a number, boolean, or array
... create-records --table Tasks --field "Name=Ship" --field-json "Score=8" \
    --field-json 'Tags=["urgent","docs"]' --field-json 'Owner=["rec5dNAPMJ1fcUKPX"]'

# --fields for a whole object, --records-file for a batch
... create-records --table Tasks --fields '{"Name":"Ship","Status":"Todo"}'
... create-records --table Tasks --records-file new_rows.json
```

`--records-file` takes a JSON array — either bare field objects or the API's own
`[{"fields": {...}}]` shape, so output from `list-records --format json` can be
edited and fed back in. Batches are split into the API's 10-record requests
automatically, so the file can hold as many rows as you like.

**Field value shapes matter.** A linked record is an array of record ids, not a
name; a collaborator is `{"id": "usr..."}`; an attachment is
`[{"url": "https://..."}]`. Getting this wrong returns a 422.
`references/field_types.md` has the full table — read it before writing
anything beyond text, numbers and dates.

### typecast

`typecast: true` lets Airtable coerce values — turning `"42"` into a number, or
creating a select option that doesn't exist yet. The CLI turns it on
automatically for `--field` (those values are strings by construction, so a
number field would otherwise reject them) and leaves it off for
`--records-file` and `--fields`, where you supplied real JSON types on purpose.
Override either way with `--typecast` / `--no-typecast`.

Be aware of the edge: with typecast on, a misspelled single-select value doesn't
error — it silently **creates a new option**, leaving the base with both
"In progress" and "In Progess". When writing select fields, check the allowed
choices in `schema` and use `--no-typecast` if you'd rather the typo fail loudly.

### Updating

`update-records` matches on record id, so resolve the id first — never guess it:

```bash
# Find it, then change it
python3 scripts/airtable_api.py list-records --table Tasks \
  --where "Name~ship the docs" --fields Name
AIRTABLE_READ_ONLY=0 python3 scripts/airtable_api.py update-records --table Tasks \
  --record rec5dNAPMJ1fcUKPX --field "Status=Done"
```

Updates are a `PATCH`: fields you don't mention keep their values. `--destructive`
switches to `PUT`, which **clears every field you didn't include** — that is
almost never what "update" means, so reach for it only when the user has asked
to replace a row wholesale.

### Upserting — the right tool for syncs

When the user says "add these, but don't duplicate what's already there",
`upsert-records` matches on 1–3 field values instead of record ids: no match
creates, one match updates, several matches fail the request.

```bash
AIRTABLE_READ_ONLY=0 python3 scripts/airtable_api.py upsert-records \
  --table Contacts --merge-on Email --records-file contacts.json
```

The merge fields must uniquely identify a record and can't be computed. This
makes a re-run idempotent, which is what makes it safe to retry a half-finished
sync.

## Rate limits, handled

Airtable allows **5 requests/second/base** and answers a breach with a 429 that
**locks the base for 30 seconds** — so a burst is far more expensive than it
looks. The CLI paces its own requests under the ceiling and, if a 429 still
lands, waits out the lockout and retries (twice) rather than failing. You don't
need to add sleeps between calls. Do avoid launching several CLI invocations
against one base in parallel, though: separate processes can't see each other's
pacing.

## Pitfalls worth knowing

- **Empty cells are omitted from responses.** A record with no `Assignee` has no
  `"Assignee"` key at all — that means "empty", not "no such field". Don't
  conclude a field is missing from the data; check `schema`.
- **A filter that matches nothing looks identical to an empty table.** Before
  reporting "there are no results", sanity-check the field name and value
  against `schema` — a case mismatch on a select option is the usual culprit.
- **403 on one base, 200 on another** means the token's Access list, not scopes.
- **Names change, ids don't.** For anything the user will re-run later, prefer
  `app...`/`tbl...` ids over names.
- **Computed fields can't be written.** Formula, rollup, count, lookup,
  autonumber, created/modified time and by — the CLI catches these before
  sending.

## Reference files

Read these when the task calls for them; they're not needed for ordinary
lookups.

- `references/field_types.md` — the write shape for every Airtable field type
  (linked records, attachments, collaborators, selects, barcodes), plus which
  types are read-only. **Read before any write beyond text/number/date.**
- `references/formulas.md` — `filterByFormula` cookbook: text matching, date
  windows, empty checks, linked-record and multi-select filters, and the
  escaping rules.
- `references/api_endpoints.md` — the raw REST endpoints with `curl` recipes,
  for the cases the CLI doesn't cover (creating tables or fields, webhooks,
  attachment uploads) and for debugging what the CLI sent.
