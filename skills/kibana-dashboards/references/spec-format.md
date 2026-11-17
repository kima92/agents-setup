# Dashboard spec format

The spec is the short, backend-neutral JSON that `build` / `lint` / `validate` / `apply` compile into
either a Kibana 9.4+ Dashboards-API body or an OpenSearch Dashboards saved-object bundle. You describe
*what* each panel measures; the compiler writes the native JSON, sizes and packs the 48-column grid,
and applies the design defaults (legends, axis titles, number formats, palettes, version gates).

Print a complete example with `python3 scripts/kibana_dashboards.py example`.

## Contents

- [Dashboard keys](#dashboard-keys)
- [Panel keys](#panel-keys)
- [Metrics](#metrics)
- [Chart cheat sheet](#chart-cheat-sheet)
- [Formats](#formats)
- [Controls](#controls)
- [ES|QL panels (Kibana only)](#esql-panels-kibana-only)
- [Escape hatches](#escape-hatches)
- [What the compiler decides for you](#what-the-compiler-decides-for-you)
- [Backend differences](#backend-differences)

## Dashboard keys

| Key | Meaning |
| --- | --- |
| `id` | Saved-object id. Default: a slug of `title`, so re-applying the same spec updates in place instead of duplicating. |
| `title` | Required. Derived from what the panels measure. |
| `description` | Set on every new dashboard. |
| `index` | Default index pattern for every panel (e.g. `logs-*`, `paymeng-charges`). |
| `time_field` | Optional; otherwise the existing index pattern's time field, else a guess (`@timestamp`, `created_at`, …). |
| `time_range` | `"7d"` or `{"from": "now-7d", "to": "now"}`. Persisted so the dashboard opens on a range that has data. |
| `controls` | List of fields (or control objects) shown as filter dropdowns. |
| `panels` | Panels with no section. |
| `sections` | `[{"title": "...", "collapsed": false, "panels": [...]}]` — summary metrics → trends → breakdowns. |

A spec with `chart` at the top level (and no `panels`/`sections`) is a **standalone visualization**
(Kibana library viz / OSD visualization) rather than a dashboard.

## Panel keys

| Key | Meaning |
| --- | --- |
| `chart` | `metric`, `gauge`, `line`, `area`, `area_stacked`, `area_percentage`, `bar`, `bar_stacked`, `bar_percentage`, `bar_horizontal`, `bar_horizontal_stacked`, `bar_horizontal_percentage`, `pie`, `donut`, `treemap`, `waffle`, `mosaic`, `table`, `heatmap`, `tag_cloud`, `region_map`, `markdown` |
| `id` | Stable panel id. Default: slug of title/label. Keep ids stable across edits. |
| `title` | Panel title. Ignored for metric/gauge/tag_cloud/waffle (they label themselves). |
| `metric` / `metrics` | One metric string, or a list (see [Metrics](#metrics)). Default `count`. |
| `label` | Label for a single-metric panel (the metric tile's caption). |
| `x` | `"time"` (default for xy) or a field to bucket by (terms). |
| `split` | Field for a series breakdown (`{"field": ..., "size": n}` also works). |
| `by` | Field(s) for pie/donut slices, table rows, tag-cloud terms, region-map regions. |
| `y_by` | Heatmap's y-axis field (x defaults to time). |
| `size` | Top-N for terms buckets (default 10). |
| `filter` | KQL applied to this panel only, e.g. `"status:failed"`. (OSD only; on Kibana use `esql` + `WHERE`.) |
| `format` | Number format for the panel's metrics (see [Formats](#formats)). |
| `legend_stats` | xy only: `["avg","min","max"]` shown in the legend (Kibana). For one or two primary time series. |
| `wide` | Make an xy/table/markdown panel full width (the primary time series). |
| `trend` | Metric only: trend background (unbounded values). |
| `bands` | Metric only: `[t1, t2]` → 3 status bands, or `{"thresholds": [1, 5], "max": 100, "palette": "status", "higher_is_better": false}`. Never with `trend`. |
| `min` / `max` / `goal` | Gauge only — only when the user gave them. |
| `text` | Markdown panels. |
| `index`, `time_field` | Per-panel override of the dashboard defaults. |
| `w`, `h`, `new_row`, `grid` | Layout overrides. Leave them out; the packer sizes by chart type. |

## Metrics

| String | Aggregation |
| --- | --- |
| `count` | Document count |
| `avg:f` / `sum:f` / `min:f` / `max:f` / `median:f` | Field aggregation |
| `unique:f` | Distinct count (cardinality) |
| `p95:f`, `p99:f`, `p50:f` | Percentile |
| `last:f` | Last value by time |

Object form: `{"op": "avg", "field": "bytes", "label": "Avg size", "format": "bytes"}`.

Field names are resolved against the live mapping: a `text` field becomes its `.keyword` subfield
automatically, and an unknown field fails lint with "did you mean …".

## Chart cheat sheet

```json
{"chart": "metric", "metric": "count", "label": "Requests"}
{"chart": "metric", "metric": "avg:error_pct", "label": "Error rate", "format": "percent100", "bands": [1, 5]}
{"chart": "area", "title": "Requests over time", "metric": "count", "wide": true, "legend_stats": ["avg","min","max"]}
{"chart": "line", "title": "Latency p95 by service", "metric": "p95:latency_ms", "split": "service.name", "size": 5}
{"chart": "bar_horizontal", "title": "Top hosts by requests", "metric": "count", "x": "host.name"}
{"chart": "donut", "title": "Share by OS", "metric": "count", "by": "os.name", "size": 6}
{"chart": "table", "title": "Top URLs", "metrics": ["count", "sum:bytes"], "by": "url.path", "size": 20}
{"chart": "heatmap", "title": "Errors by service over time", "metric": "count", "y_by": "service.name"}
{"chart": "tag_cloud", "metric": "count", "by": "http.response.status_code"}
{"chart": "gauge", "metric": "avg:cpu_pct", "min": 0, "max": 100}
{"chart": "markdown", "text": "Data from the `orders` index; amounts are in agorot."}
```

## Formats

`bytes`, `bits`, `percent` (value is a 0–1 fraction), `percent100` (value already 0–100),
`number`, `compact`, `decimal`, `duration:ms` (any of `ps ns us ms s min h d w mo y`),
`suffix:<text>`, `none`, or a raw Kibana format object.

When `format` is omitted, the compiler infers one from the field name for sum/avg/min/max/percentile
metrics only: `*bytes*` → bytes, `*_ms` / `*_us` / `*_ns` / `*_seconds` → duration, `*pct` / `*percent` → percent100.
Counts and distinct counts are never formatted.

## Controls

```json
"controls": ["service.name", "env", {"field": "bytes", "type": "range", "title": "Response size"}]
```

- A string is an options-list dropdown. Pick 3–5 low-cardinality fields (`discover` suggests them).
- `{"type": "range"}` adds a numeric range slider. `{"type": "time"}` adds a time slider (Kibana only).
- Kibana: placed in `pinned_panels`. The data view they need is found by title or created on apply. On an
  all-ES|QL dashboard on 9.5+, values come from `FROM <index> | STATS BY <field>` instead.
- OSD: compiled into one full-width "input controls" panel at the top.

## ES|QL panels (Kibana only)

Add `esql` to a panel and the field keys become **column names**:

```json
{"chart": "line", "title": "Requests by status over time",
 "esql": "FROM logs-* | WHERE @timestamp <= ?_tend AND @timestamp > ?_tstart | STATS Requests = COUNT() BY BUCKET(@timestamp, 75, ?_tstart, ?_tend), status",
 "metric": "Requests", "split": "status"}
```

- `x` defaults to the first `BY` column; a `BUCKET(...)` x gets `scale: temporal` and a label automatically.
- `metric` defaults to the first `STATS` alias.
- `validate` runs each query (with `?_tstart`/`?_tend` substituted) and checks every referenced column exists.
- Trend backgrounds on ES|QL metrics need 9.5+ (dropped with a note on 9.4); see the Metric rules in
  [chart-design.md](chart-design.md#metric) for the query shapes the trendline needs.

## Escape hatches

- `"kibana": {...}` on a panel is deep-merged into the compiled Lens config (any Lens setting the spec
  does not model: custom colors, axis domains, styling).
- `"osd": {...}` is deep-merged into the compiled OSD `visState`.
- For anything else, `build -o out.json` the spec, edit the native file, and `apply out.json`.

## What the compiler decides for you

- Sizes: metric 12×5 (a run of 3 → 16 wide, 5–6 → 8, 7–8 → 6), gauge 12×8, xy/heatmap/tag cloud/treemap 24×10,
  pie 12×10, table 48×12, markdown 48×(4–9 by length). Rows fill left to right, heights in a row are equalized,
  and the last stretchable panel in a short row grows to fill it.
- XY: axis titles hidden; legend at the bottom, visible only with a split, several metrics or statistics
  (`grid` layout with statistics, `list` otherwise); line breakdowns use `elastic_line_optimized`; area charts
  with more than 3 series become lines; gradient area fill on Kibana 9.6+.
- Metric/gauge/tag cloud/waffle panels get no panel title.
- Gauge: 4 equal status bands.
- Version gates (Kibana): ES|QL trend backgrounds and ES|QL-sourced controls need 9.5+, gradient areas 9.6+.
  Below that, the setting is dropped and a note is printed.

## Backend differences

| Feature | Kibana 9.4+ | OpenSearch Dashboards |
| --- | --- | --- |
| Sections | Collapsible sections | Flattened; a slim markdown header per section |
| `format` | Per panel | Ignored (set field formats on the index pattern) |
| `legend_stats`, `trend` | Yes | Dropped with a note |
| `treemap`/`waffle`/`mosaic` | Yes | Rendered as a donut |
| `filter` (KQL) | Not on data-view panels — use `esql` | Yes |
| `esql` | Yes | No (PPL/SQL exist only for `query`) |
| Controls | `pinned_panels` | Input-controls panel |
| Visualizations | Inline in the dashboard | One saved object per panel, id `<dashboard-id>--<panel-id>` |
