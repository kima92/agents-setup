# Kibana & OpenSearch Dashboards skill

A self-contained Python CLI (stdlib only) for building and managing dashboards on
**Kibana 9.4+** and **OpenSearch Dashboards** (including AWS OpenSearch Service),
packaged as a Claude skill. The backend is detected automatically from `KIBANA_URL`.

It is a standalone port of Elastic's
[`kibana-dashboards`](https://github.com/elastic/agent-skills/tree/main/skills/kibana/kibana-dashboards)
skill. The design guidance and API references are Elastic's (Apache-2.0) and live in
`references/`. The `elastic` CLI dependency is replaced by `scripts/kibana_dashboards.py`,
which does more than forward HTTP calls:

- **`discover`**: profiles an index in one request. You get field types, distinct counts and
  top values, numeric ranges (each field tagged as a measure, a category code or an id), the
  data's time span, and suggested controls and metrics.
- **specs**: you describe a dashboard in a few lines of JSON. The script compiles it to Lens
  JSON (Kibana) or `visState` saved objects (OpenSearch), applying the design rules and packing
  the grid.
- **`validate`**: runs every panel's query against the data in one batch and prints the value
  each panel will show, or flags it as empty or broken, before anything is saved.
- **`apply`**: one command for lint → validate → backup → write → read-back → URL. On
  OpenSearch it also creates or repairs index patterns, and removes panel visualizations the
  dashboard no longer uses.
- **`get`**: prints an outline of an existing dashboard instead of raw JSON. `-o` saves an
  editable, decoded copy.
- **`copy`**: moves a dashboard between OpenSearch tenants or Kibana spaces, for example from
  private to global.

It is **read-only by default**: writes need `KIBANA_READ_ONLY=0`. Before any overwrite or
delete, the previous version is backed up to `~/.cache/kibana-dashboards/backups/`, and
`apply <backup>` restores it.

## Install

```bash
npx skills add https://github.com/kima92/agents-setup --skill kibana-dashboards
```

## Setup

```bash
export KIBANA_URL="https://kibana.example.com"      # or an AWS OpenSearch domain endpoint
export KIBANA_API_KEY="..."                          # or KIBANA_USERNAME / KIBANA_PASSWORD
# optional
export ELASTICSEARCH_URL="https://es.example.com"    # data endpoint; default: KIBANA_URL root or the console proxy
export KIBANA_SPACE="team-a"                         # Kibana space
export KIBANA_TENANT="private"                       # OpenSearch tenant
export KIBANA_READ_ONLY=0                            # allow writes
```

## Quick start

```bash
cd skills/kibana-dashboards
python3 scripts/kibana_dashboards.py status
python3 scripts/kibana_dashboards.py discover logs-*
python3 scripts/kibana_dashboards.py example > my-dash.json      # edit it
python3 scripts/kibana_dashboards.py apply my-dash.json --dry-run
KIBANA_READ_ONLY=0 python3 scripts/kibana_dashboards.py apply my-dash.json
```

## Tested

- OpenSearch Dashboards 3.5 on AWS OpenSearch 2.19: `status`, `discover`, `validate`,
  `apply` (create, update, index-pattern creation and field repair), `get`, `list`, and
  `delete`, checked live in a private tenant. Rendering was verified in a browser.
- Kibana 9.4+: the compiler output is checked offline against Elastic's reference
  dashboards in `assets/`. Not yet exercised against a live Kibana.
