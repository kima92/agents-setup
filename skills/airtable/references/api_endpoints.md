# Airtable Web API — raw endpoints and curl recipes

The CLI covers the everyday work. This file is for the two cases it doesn't:
operations outside its scope (creating tables and fields, webhooks, attachment
uploads, enterprise admin), and debugging — seeing exactly what a request looks
like on the wire.

Docs: <https://airtable.com/developers/web/api/introduction>

## Contents

- [Basics](#basics)
- [Metadata endpoints](#metadata-endpoints)
- [Record endpoints](#record-endpoints)
- [Comment endpoints](#comment-endpoints)
- [Schema-writing endpoints (not in the CLI)](#schema-writing-endpoints-not-in-the-cli)
- [Attachments](#attachments)
- [Deleting records](#deleting-records)
- [Error codes](#error-codes)
- [Limits](#limits)

## Basics

- Root: `https://api.airtable.com/v0`
- Auth header: `Authorization: Bearer $AIRTABLE_API_KEY`
- JSON in and out; send `Content-Type: application/json` on any body.
- Object ids are a 3-character prefix plus 14 alphanumerics: bases `app…`,
  tables `tbl…`, views `viw…`, records `rec…`, fields `fld…`, users `usr…`.
  Ids are stable; names are not.

```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?maxRecords=5" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

`-s` keeps curl's progress meter out of the output. `python3 -m json.tool` is
always available; `jq` is nicer but optional.

Query parameters with brackets (`fields[]`, `sort[0][field]`) **must** be
URL-encoded, as must any formula. Let Python do it rather than escaping by hand:

```bash
ENC=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' \
      "{Status}='Todo'")
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?filterByFormula=$ENC" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

## Metadata endpoints

| Method | Path | Scope | Purpose |
|---|---|---|---|
| GET | `/meta/whoami` | none | Token's user id, and scopes for OAuth tokens |
| GET | `/meta/bases` | `schema.bases:read` | Bases the token can access (paginated, 1000/page) |
| GET | `/meta/bases/{baseId}/tables` | `schema.bases:read` | Full schema: tables, fields, options, views |

```bash
curl -s "https://api.airtable.com/v0/meta/bases/$BASE_ID/tables" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

CLI equivalents: `whoami`, `list-bases`, `schema`.

## Record endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/{baseId}/{tableIdOrName}` | List records |
| POST | `/{baseId}/{tableIdOrName}/listRecords` | Same, parameters in the body (for long formulas) |
| GET | `/{baseId}/{tableIdOrName}/{recordId}` | One record |
| POST | `/{baseId}/{tableIdOrName}` | Create up to 10 records |
| PATCH | `/{baseId}/{tableIdOrName}` | Update up to 10 by id (merge), or upsert |
| PUT | `/{baseId}/{tableIdOrName}` | Same, but clears unlisted fields |
| PATCH | `/{baseId}/{tableIdOrName}/{recordId}` | Update one record (merge) |

List parameters: `fields[]`, `filterByFormula`, `maxRecords`, `pageSize` (≤100),
`offset`, `view`, `sort[n][field]`, `sort[n][direction]`, `cellFormat`
(`json`|`string`), `timeZone`, `userLocale`, `returnFieldsByFieldId`,
`recordMetadata[]`.

A table **name** works in the path in place of the id, URL-encoded.

### Pagination

A response carrying `"offset"` has more pages; pass it back until the field is
gone.

```bash
OFFSET=""
while :; do
  URL="https://api.airtable.com/v0/$BASE_ID/$TABLE?pageSize=100"
  [ -n "$OFFSET" ] && URL="$URL&offset=$OFFSET"
  RESP=$(curl -s "$URL" -H "Authorization: Bearer $AIRTABLE_API_KEY")
  echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(r["id"]) for r in d["records"]]'
  OFFSET=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("offset",""))')
  [ -z "$OFFSET" ] && break
  sleep 0.25            # stay under 5 req/sec/base
done
```

The CLI's `list-records --all` does this, with pacing.

### Long formulas

Airtable rejects URLs over 16,000 characters. Move the same parameters into a
`POST .../listRecords` body — it is still a read:

```bash
curl -s -X POST "https://api.airtable.com/v0/$BASE_ID/$TABLE/listRecords" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"filterByFormula":"OR({Id}=1,{Id}=2)","pageSize":100}' | python3 -m json.tool
```

### Create, update, upsert

```bash
# Create (max 10 per request)
curl -s -X POST "https://api.airtable.com/v0/$BASE_ID/$TABLE" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" -H "Content-Type: application/json" \
  -d '{"typecast": true, "records": [{"fields": {"Name": "Task A", "Status": "Todo"}}]}'

# Update one record, merging
curl -s -X PATCH "https://api.airtable.com/v0/$BASE_ID/$TABLE/$RECORD_ID" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" -H "Content-Type: application/json" \
  -d '{"fields": {"Status": "Done"}}'

# Upsert on a merge field (no record id needed)
curl -s -X PATCH "https://api.airtable.com/v0/$BASE_ID/$TABLE" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" -H "Content-Type: application/json" \
  -d '{"performUpsert": {"fieldsToMergeOn": ["Email"]},
       "records": [{"fields": {"Email": "user@acme.io", "Status": "Active"}}]}'
```

`fieldsToMergeOn` takes 1–3 non-computed fields of type number, text, long text,
single select, multi-select or date. Zero matches create; one match updates;
multiple matches fail the whole request.

## Comment endpoints

| Method | Path | Scope |
|---|---|---|
| GET | `/{baseId}/{tableIdOrName}/{recordId}/comments` | `data.recordComments:read` |
| POST | `/{baseId}/{tableIdOrName}/{recordId}/comments` | `data.recordComments:write` |
| PATCH / DELETE | `…/comments/{commentId}` | `data.recordComments:write` |

Mention a collaborator inside `text` with `@[usrXXXXXXXXXXXXXX]`.

## Schema-writing endpoints (not in the CLI)

These need `schema.bases:write` and are deliberately outside the CLI's scope —
changing a base's structure affects every view, automation and integration built
on it, so it should be a deliberate, hand-written call.

| Method | Path | Purpose |
|---|---|---|
| POST | `/meta/bases/{baseId}/tables` | Create a table |
| PATCH | `/meta/bases/{baseId}/tables/{tableId}` | Rename a table / change description |
| POST | `/meta/bases/{baseId}/tables/{tableId}/fields` | Create a field |
| PATCH | `/meta/bases/{baseId}/tables/{tableId}/fields/{fieldId}` | Rename a field |
| POST | `/meta/bases` | Create a base (needs a workspace id) |

```bash
curl -s -X POST "https://api.airtable.com/v0/meta/bases/$BASE_ID/tables/$TABLE_ID/fields" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "Severity", "type": "singleSelect",
       "options": {"choices": [{"name": "Low"}, {"name": "High"}]}}'
```

There is no endpoint to delete a field or a table.

## Attachments

Two routes:

1. **By URL** (no extra scope): write `[{"url": "https://…"}]` to the attachment
   field. Airtable fetches and rehosts it, so the URL must be publicly
   reachable — a signed link that expires in 60 seconds often fails.
2. **Direct upload** (`data.records:write`), for local files up to 5 MB:

```bash
curl -s -X POST \
  "https://content.airtable.com/v0/$BASE_ID/$RECORD_ID/$FIELD_ID/uploadAttachment" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"contentType\":\"application/pdf\",\"filename\":\"spec.pdf\",\"file\":\"$(base64 -w0 spec.pdf)\"}"
```

Note the different host (`content.airtable.com`) and that the field is addressed
by **field id**, not name.

Attachment URLs in read responses **expire a couple of hours** after they're
issued. Download them promptly; don't store them as permanent links.

## Deleting records

The API supports `DELETE /{baseId}/{tableIdOrName}/{recordId}` and a batch form.
**The CLI implements neither, on purpose** — an API delete is immediate and has
no undo path, unlike the UI, which has a trash bin.

If a user genuinely needs rows removed, point them at the Airtable UI rather
than running a raw `DELETE` on their behalf. If they explicitly insist on the
API, read the records out first, show them the exact list and count, and get
confirmation before anything is sent.

## Error codes

Airtable returns `{"error": {"type": ..., "message": ...}}`, or sometimes a bare
`{"error": "NOT_FOUND"}`. The ones worth recognizing:

| Status / type | Meaning |
|---|---|
| `401 AUTHENTICATION_REQUIRED` | Missing, malformed or revoked token |
| `403 NOT_AUTHORIZED` | Usually the base is not in the token's **Access** list; sometimes a missing scope |
| `404 NOT_FOUND` / `TABLE_NOT_FOUND` | Wrong base id, table name/id, or record id |
| `422 INVALID_MULTIPLE_CHOICE_OPTIONS` | Select value isn't an existing choice (and typecast is off) |
| `422 UNKNOWN_FIELD_NAME` | Field name typo — case and spaces are significant |
| `422 INVALID_VALUE_FOR_COLUMN` | Right field, wrong value shape for its type |
| `429` | Rate limited; the base is locked for 30 seconds |
| `413 REQUEST_TOO_LARGE` | Body over the limit — split the batch |

## Limits

| Limit | Value |
|---|---|
| Requests per second, per base | 5 (429 then a 30-second lockout) |
| Requests per second, per token across all bases | 50 |
| Records per batch create/update/delete | 10 |
| Records per list page | 100 (`pageSize` max and default) |
| URL length | 16,000 characters |
| Direct attachment upload | 5 MB |
| Attachment URL lifetime in responses | ~2 hours |
