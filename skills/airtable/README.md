# Airtable skill

A self-contained Python CLI for the [Airtable Web API](https://airtable.com/developers/web/api/introduction),
packaged as a Claude skill. It covers the everyday Airtable workflows —
discovering bases and their schema, filtering, sorting and exporting records,
reading record comments, and creating, updating and upserting rows.

It is **non-destructive**: there is no delete command at all, so the skill
cannot remove records, fields or tables. It is also **read-only by default** —
writes have to be enabled explicitly with `AIRTABLE_READ_ONLY=0`.

## Install

```bash
npx skills add https://github.com/kima92/agents-setup --skill airtable
```

## Setup

Create a **personal access token** at <https://airtable.com/create/tokens> and
export it:

```bash
export AIRTABLE_API_KEY="pat..."     # AIRTABLE_PAT / AIRTABLE_TOKEN also work
export AIRTABLE_BASE_ID="app..."     # optional default base, so --base can be omitted
```

Two things to set on the token, both in the same UI:

- **Scopes** — `data.records:read` at minimum, plus `schema.bases:read` (to list
  bases and read schema), `data.records:write` (for writes) and
  `data.recordComments:read` / `:write` (for comments).
- **Access** — the list of bases the token may touch. This is the easily missed
  one: a token with the right scopes still gets a **403** on a base that isn't
  in its Access list.

Check it works with the cheapest possible call:

```bash
python3 scripts/airtable_api.py whoami
```

Legacy `key...` API keys were deprecated in February 2024 and no longer work;
the script detects one and tells you.

No third-party Python packages are needed — the script uses `curl` (falling back
to stdlib `urllib`) and the Python standard library only.

### Read-only mode

`AIRTABLE_READ_ONLY` defaults to `1`. While it's on, `create-records`,
`update-records`, `upsert-records` and `add-comment` are refused with exit code
3, and the transport layer blocks mutating requests as a second line of defense.
Reads are unaffected. Any unrecognized value leaves read-only on, so a typo
fails safe.

```bash
export AIRTABLE_READ_ONLY=0                                     # for the session
AIRTABLE_READ_ONLY=0 python3 scripts/airtable_api.py create-records …   # or per command
```

## Commands

| Command | Description |
|---|---|
| `whoami` | Show the token's user id and scopes |
| `list-bases` | Every base the token can see |
| `schema` | A base's tables, fields, types, select options and views |
| `list-records` | Filter, sort and page through records (text / JSON / CSV) |
| `get-record` | One record by id |
| `list-comments` | The comment thread on a record |
| `create-records` | **[write]** Create one or many records |
| `update-records` | **[write]** Update records by id |
| `upsert-records` | **[write]** Create-or-update, matched on field values |
| `add-comment` | **[write]** Post a comment on a record |

```bash
python3 scripts/airtable_api.py <command> [options]
python3 scripts/airtable_api.py <command> --help
```

Exit codes: `0` ok, `1` API/transport error, `2` bad configuration or arguments,
`3` blocked by read-only mode.

## Examples

```bash
# What's in here?
python3 scripts/airtable_api.py list-bases
python3 scripts/airtable_api.py schema --base "Sales CRM"

# Filter without writing formula syntax; --where clauses are AND'd
python3 scripts/airtable_api.py list-records --table Tasks \
  --where "Status=Todo" --where "Due<2026-04-01" \
  --fields "Name,Owner,Due" --sort "Due" --max-records 50

# Export a view to CSV
python3 scripts/airtable_api.py list-records --table Tasks \
  --view "Q3 pipeline" --all --format csv > pipeline.csv

# Paste a browser URL instead of hunting for ids
URL="https://airtable.com/appSW9R5uCNmRmfl6/tbltp8DGLhqbUmjK1/viwkWFQ1nPBcJkEXs/rec5dNAPMJ1fcUKPX"
python3 scripts/airtable_api.py get-record --base "$URL" --table "$URL" --record "$URL"

# Write a row
AIRTABLE_READ_ONLY=0 python3 scripts/airtable_api.py create-records --table Tasks \
  --field "Name=Ship the docs" --field "Status=Todo" --field-json "Score=8"

# Idempotent sync: create what's new, update what exists
AIRTABLE_READ_ONLY=0 python3 scripts/airtable_api.py upsert-records \
  --table Contacts --merge-on Email --records-file contacts.json
```

## What the CLI handles for you

The point of the script, rather than raw `curl`, is that these are done once and
correctly:

- **Filter quoting and URL-encoding** — `--where "Status=Todo"` becomes a
  properly escaped, encoded `filterByFormula`. Date values are routed through
  Airtable's `IS_SAME`/`IS_AFTER`/`IS_BEFORE` instead of `=`/`>`/`<`, which
  silently match nothing on a datetime field.
- **Pagination** — `offset` looping behind `--all` / `--max-records`, and an
  automatic switch to the `POST /listRecords` endpoint when a formula would push
  the URL past Airtable's 16,000-character cap.
- **Batching** — writes are split into the API's 10-record requests, so a
  1,000-row file is one command.
- **Rate limiting** — requests are paced under the 5/second/base ceiling, and a
  429 (which locks the base for 30 seconds) is waited out and retried.
- **Pre-flight field validation** — writes are checked against the table schema
  first, so a typo returns "unknown field 'Nmae' (did you mean 'Name'?)" instead
  of a partially applied batch.
- **Error hints** — a 403 explains the per-base Access list; a select failure
  explains typecast.

## Layout

```
airtable/
├── SKILL.md                      instructions loaded when the skill triggers
├── README.md
├── scripts/airtable_api.py       the CLI (stdlib only)
└── references/
    ├── field_types.md            write shape for every field type
    ├── formulas.md               filterByFormula cookbook
    └── api_endpoints.md          raw REST endpoints + curl recipes
```

## Credit

The endpoint coverage and several of the pitfalls were informed by the
[Hermes agent Airtable skill](https://github.com/NousResearch/hermes-agent/tree/main/skills/productivity/airtable)
(MIT), reworked here into a Python CLI with read-only-by-default safety.
