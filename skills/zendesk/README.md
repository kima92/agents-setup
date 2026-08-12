# Zendesk skill

A self-contained Python CLI for the Zendesk Support REST API v2, packaged as a
Claude skill. It covers the everyday support workflows — triaging, reading,
searching, creating and updating tickets, replying to customers or leaving
internal notes, and looking up users, organizations, and agent groups.

It is **non-destructive**: it never deletes tickets, users, organizations, or
groups. It is also **read-only by default** — writes have to be enabled
explicitly with `ZENDESK_READ_ONLY=0`.

## Install

```bash
npx skills add https://github.com/kima92/agents-setup --skill zendesk
```

## Setup

Set three environment variables:

```bash
export ZENDESK_SUBDOMAIN="acme"          # the 'acme' in acme.zendesk.com
export ZENDESK_EMAIL="agent@acme.io"
export ZENDESK_API_TOKEN="…"             # Admin Center → APIs → Token access
```

No third-party Python packages are needed — the script uses `curl` (falling
back to stdlib `urllib`) and the Python standard library only.

### Read-only mode

`ZENDESK_READ_ONLY` defaults to `1`. While it's on, the write commands
(`create-ticket`, `update-ticket`, `add-comment`, `create-user`, `update-user`,
`create-organization`) are refused with exit code 3, and the transport layer
blocks any non-GET request as a second line of defense. Reads are unaffected.

```bash
export ZENDESK_READ_ONLY=0               # allow writes for the whole session
ZENDESK_READ_ONLY=0 python3 scripts/zendesk_api.py add-comment …   # or per command
```

Only `0`, `false`, `no`, and `off` disable it; anything else (including a typo)
keeps read-only on.

## Usage

```bash
python3 scripts/zendesk_api.py <command> [options]
python3 scripts/zendesk_api.py --help
```

See [`SKILL.md`](SKILL.md) for the full command list, examples, and workflow
guidance, and [`references/api_endpoints.md`](references/api_endpoints.md) for
the underlying API details.

## Layout

```
zendesk/
├── SKILL.md                     # skill instructions (the entry point)
├── README.md                    # this file
├── scripts/
│   └── zendesk_api.py           # the CLI
└── references/
    └── api_endpoints.md         # endpoint + search-syntax reference
```
