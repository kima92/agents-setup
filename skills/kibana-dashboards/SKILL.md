---
name: kibana-dashboards
description: Build, edit, check and manage dashboards and visualizations on Kibana 9.4+ and OpenSearch Dashboards (incl. AWS OpenSearch Service /_dashboards) with a self-contained Python CLI (stdlib only, no `elastic` CLI). The backend is auto-detected from KIBANA_URL. You write a short backend-neutral spec; the script profiles the index, compiles Lens / visState JSON with the design defaults, packs the 48-column grid, runs every panel's query against the real data, backs up what it overwrites, writes, and reads it back. Use whenever the user wants to create, redesign, fix, copy, inspect or list a Kibana or OpenSearch dashboard, visualization, Lens chart, data view / index pattern, or dashboard controls — e.g. "make a dashboard for the charges index", "add a p95 latency panel to the ops dashboard", "why is this panel empty", "move my dashboard from the private tenant to global", "what's on dashboard X", "chart errors by service over the last week in Kibana". Trigger it too for "visualize this index", "build a KPI board in OpenSearch", or ES|QL-backed Kibana panels, even if the user never says "API". Read-only by default; writes need KIBANA_READ_ONLY=0.
compatibility: Python 3.8+ (stdlib only). Kibana 9.4+ (Dashboards/Visualizations APIs), Kibana 7.x/8.x (classic saved objects) or OpenSearch Dashboards 1.x–3.x. Network access to the Kibana/OSD URL.
metadata:
  based_on: elastic/agent-skills kibana-dashboards 0.4.0 (Apache-2.0)
---

# Kibana & OpenSearch Dashboards

`scripts/kibana_dashboards.py` does the mechanical work of building a dashboard so you don't spend tokens on it.
Your job is the judgement: what to measure, which chart shows it, how to group and title it. The script handles
the rest: profiling fields, writing native JSON, grid math, design defaults, query checks, backups and read-back.

It works on both backends and detects which one KIBANA_URL is:

| | Kibana 9.4+ | Kibana < 9.4 (7.x/8.x) | OpenSearch Dashboards |
|---|---|---|---|
| Writes | `/api/dashboards`, `/api/visualizations` (Lens) | classic saved objects, as OSD | saved objects: index-pattern, visualization, dashboard |
| Queries | ES\|QL | SQL / DSL (ES\|QL from 8.11) | PPL / SQL / DSL |
| Scoping | `--space` | `--space` | `--tenant private|global|<name>` |

Kibana below 9.4 has no Dashboards API, so the script writes the same classic visualizations it writes for OSD
(`status` says so). Existing Lens panels there show up in `get` as `lens:<type>` but are not decoded or validated.

## Setup

```bash
export KIBANA_URL="https://kibana.example.com"   # an AWS OpenSearch domain endpoint works as-is
export KIBANA_API_KEY="..."                       # or KIBANA_USERNAME + KIBANA_PASSWORD
# behind a reverse proxy with its own basic auth (nginx): KIBANA_NGINX_USERNAME + KIBANA_NGINX_PASSWORD,
# and the app login as KIBANA_USERNAME + KIBANA_PASSWORD (the script logs in for a session cookie)
# optional: ELASTICSEARCH_URL / OPENSEARCH_URL, KIBANA_SPACE, KIBANA_TENANT, KIBANA_INSECURE=1
```

Writes are refused until `KIBANA_READ_ONLY=0`. Before every overwrite or delete, the previous version is saved
to `~/.cache/kibana-dashboards/backups/`, and `apply <backup-file>` restores it. Never put credentials in a
command line; they come from the environment.

Run everything as `python3 scripts/kibana_dashboards.py <command>` (paths relative to this skill's directory).

## Workflow: new dashboard

1. **`status`**: backend, version, tenants/space, which version-gated features are on, write mode. One call.
2. **`discover <index>`**: a single batched request returns the field types, distinct counts and top values of
   keyword fields, numeric ranges with each field's role (measure / category code / id), the time field, the data's
   time span, docs in the last 7 days, matching index patterns, and suggested controls and metrics. Use this
   instead of reading mappings. It shows which fields are worth charting and whether the data is old: if the newest
   document is days old, persist a `time_range` that covers it, or the dashboard opens empty.
3. **Write a spec** (see below and [references/spec-format.md](references/spec-format.md)). Keep it in a file.
4. **`apply spec.json --dry-run`** runs lint, then a live data check that prints the value each panel will
   show (`OK charges  Charges=746.1k`, `WARN … EMPTY`, `ERROR unknown field 'x' -- did you mean …`), then a
   plan/diff. Fix what it reports and repeat. This is cheap: one batched query.
5. **`KIBANA_READ_ONLY=0 … apply spec.json`** does the same, plus backup, write and read-back verification, and
   prints the URL. On OpenSearch it also creates a missing index pattern with its field list. If an *existing*
   pattern lacks fields the panels use, the dry-run plan says so, and apply backs the pattern up before refreshing
   its field list. Mention it to the user. Default id is a slug of
   the title, so re-applying an edited spec updates the dashboard in place.
6. Report the id, title and URL. Mention any `WARN`/notes (dropped features, empty panels).

On OpenSearch, iterate in `--tenant private`, then promote with `copy <id> --to-tenant global` once the user is happy.

## The spec

```json
{
  "title": "Charges overview",
  "description": "Charge volume and totals from paymeng-charges by VAS, status and currency.",
  "index": "paymeng-charges",
  "time_range": "7d",
  "controls": ["vas_name", "charge_status", "charge_currency"],
  "sections": [
    {"title": "Key metrics", "panels": [
      {"chart": "metric", "metric": "count", "label": "Charges"},
      {"chart": "metric", "metric": "sum:charge_total", "label": "Charge total"},
      {"chart": "metric", "metric": "unique:charge_merchant_id", "label": "Merchants charged"}]},
    {"title": "Trends", "panels": [
      {"chart": "area", "title": "Charges over time", "metric": "count", "wide": true, "legend_stats": ["avg", "max"]},
      {"chart": "line", "title": "Charge total by currency", "metric": "sum:charge_total", "split": "charge_currency", "size": 4}]},
    {"title": "Breakdowns", "panels": [
      {"chart": "bar_horizontal", "title": "Charges by VAS", "metric": "count", "x": "vas_name"},
      {"chart": "donut", "title": "Charges by card brand", "metric": "count", "by": "charge_card_brand", "size": 6},
      {"chart": "table", "title": "Top charge descriptions", "metrics": ["count", "sum:charge_total"], "by": "charge_description", "size": 15}]}
  ]
}
```

Metrics: `count`, `avg:f`, `sum:f`, `min:f`, `max:f`, `median:f`, `unique:f`, `p95:f`, `last:f`. `x` is `time`
(default) or a field; `split` adds series; `by` sets slices/rows/terms; `filter` is per-panel KQL (OSD);
`format` is `bytes|percent|percent100|duration:ms|…`; `bands: [t1, t2]` colors a bounded metric.
Kibana panels can use `"esql": "FROM … | STATS …"`, and then fields are column names. Text fields resolve to
`.keyword` automatically.

Leave out `grid`/`w`/`h`. The packer sizes panels by chart type and tiles rows with no gaps. Set `wide: true` on
the one or two primary time series.

## Design: what you still decide

The compiler applies the mechanical rules: no axis titles, legend placement and visibility by series count, line
palette, areas switching to lines above 3 series, no titles on metric/gauge/tag-cloud panels, natural units from
field names, and version gates. The choices below are yours, so make them deliberately:

- **Composition:** summary metrics → trends → breakdowns. Use sections once there are about 6 or more panels, each
  holding only panels that fit its purpose. Let the fields that `discover` shows drive the panel count; every panel
  needs a reason.
- **Titles:** a non-empty dashboard `title` and a `description` derived from what the queries measure. Panel titles
  should be self-explanatory and not repeat what the axis or label already says.
- **Chart choice:** metric for a single number, line/area for trends, horizontal bar for top-N categories,
  donut/pie only under ~7 slices, table for precise multi-column rankings, heatmap for two bucket dimensions,
  tag cloud only for short strings. `validate` warns about pies with too many slices and areas with too many series.
- **Measures vs codes:** `discover` labels low-cardinality integer fields as `category` (status codes, currency
  ids). Split or filter by them; don't average them.
- **Controls:** 3–5 low-cardinality fields (start from `suggested controls`). None on a dashboard scoped to one entity.
- **Color:** only where it carries meaning. `bands` on bounded metrics (rates, percentages); never invent gauge
  `min`/`max`/`goal`; never invent hex colors (palette catalog: [references/color-palettes.md](references/color-palettes.md)).
- **Time:** without `time_range`, Kibana and OSD open on the last 15 minutes. `discover`'s span line tells you
  whether that would show nothing. If the newest data is old, say so and persist a range that covers it. If
  `discover` reports timestamps in the future, the data is probably stored in local time as UTC. Tell the user,
  because "last N hours" views will be offset.

Full guidance: [dashboard-design.md](references/dashboard-design.md) (layout, sections, controls) and
[chart-design.md](references/chart-design.md) (per-chart rules, formats, color). Read them for unusual requests
or when the user cares about polish.

## Editing an existing dashboard

- **Built from a spec:** edit the spec and `apply` it again. Same id, so the dashboard is updated in place; OSD
  panel visualizations the dashboard no longer uses are removed.
- **Inspecting / "why is it empty":** `validate --id <id> --since 1h` (OSD) runs every saved panel's
  aggregation in one batch and prints what each shows. Prefer it over hand-written queries.
- **Anything else:** `get <id>` prints a compact outline (panel ids, chart types, grid, queries, filters). Read it
  before fetching full JSON. `get <id> -o current.json` saves an editable copy (a Kibana body, or an OSD bundle
  with the JSON-string fields decoded). Change only what was asked, keep panel ids, run `layout current.json` if
  you resized or removed panels, then `apply current.json --dry-run` to see the diff, and `apply`.
- `PUT` and bulk-create replace whole objects. Never send a partial body.

## Command reference

| Command | What it does |
|---|---|
| `status` | Backend, version, data endpoint, tenants/space, feature gates, write mode |
| `discover <index> [--since 7d] [--fields re]` | Field profile and suggestions in one request |
| `query "<esql/ppl/sql>" [--since 7d]` | Run a query, print TSV. ES\|QL `?_tstart`/`?_tend` are substituted |
| `list [dashboards\|visualizations\|index-patterns] [--search s]` | Compact listing |
| `get <id> [--vis] [-o file] [--json]` | Outline (default) or editable copy |
| `build <spec> [-o file] [--offline --target kibana\|osd]` | Compile + lint, no writes |
| `lint <file>` | Design and schema checks on a spec, Kibana body or OSD bundle |
| `validate <file> [--since 30d]` | Run every panel's query; report values / empties / bad columns |
| `validate --id <dashboard> [--since 1h]` | OSD: same check for any saved dashboard ("why is this panel empty?") |
| `layout <file>` | Repack grid coordinates of a native body or bundle |
| `apply <file> [--id] [--dry-run] [--force] [--no-validate]` | **[write]** lint → validate → backup → write → verify |
| `copy <id> --to-tenant global \| --to-space s [-o x.ndjson]` | **[write]** export with references and import elsewhere |
| `delete <id> [--vis] [--with-panels] --yes` | **[write]** delete after backing up |
| `example` | Print a full example spec |

Global flags: `--tenant` (OSD), `--space` (Kibana). Exit codes: 0 ok, 1 lint/validate/verify problems,
2 API/usage error, 3 refused by read-only mode.

## Native Kibana bodies

`lint`, `validate`, `layout` and `apply` also accept a raw Kibana Dashboards-API body (`{title, panels, …}`) or a
Lens visualization body. Use this when the user hands you one, or when you need Lens settings the spec doesn't model
(`"kibana": {...}` on a spec panel deep-merges into the compiled config first). Schemas:
[dashboard-api-reference.md](references/dashboard-api-reference.md),
[lens-api-reference.md](references/lens-api-reference.md),
[chart-types-reference.md](references/chart-types-reference.md); complete examples in `assets/`.

## OpenSearch specifics

Read [references/opensearch-dashboards.md](references/opensearch-dashboards.md) when working on OSD:

- tenants, and why the view URL puts `security_tenant` before the `#`
- index-pattern field lists
- what classic visualizations can't do (no per-panel formats, legend stats or trend backgrounds; treemap renders
  as a donut). The compiler prints a note whenever it drops one of these.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `validate` says `EMPTY` | No docs in the range. Check the span from `discover` and set `time_range` (or `--since` to test) |
| OSD panel: "Could not locate that index-pattern-field" | Index pattern has a stale or empty field list. Re-run `apply`, which refreshes it |
| OSD "Could not locate that dashboard" | Wrong tenant. Use `--tenant` and the URL `apply` prints |
| Kibana 404 on `/api/dashboards` | Wrong space (Kibana < 9.4 is detected and uses saved objects) |
| "The proxy rejected KIBANA_NGINX_USERNAME/PASSWORD" | nginx basic-auth credentials are wrong or unset; 401 with no `kbn-name` header comes from the proxy |
| 400 on a Kibana write | Run `lint` on the compiled body (`build -o`). Column/operation mismatches and color steps are the usual causes |
| Multi-series chart without a legend | Lint error on native bodies; the spec compiler sets it for you |
| `read-only mode is on` | Expected. Ask the user, then re-run with `KIBANA_READ_ONLY=0` |
