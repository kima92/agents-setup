# OpenSearch Dashboards notes

OpenSearch Dashboards (OSD, including AWS OpenSearch Service's `/_dashboards`) is a fork of Kibana 7.10.
It has none of the Kibana 9.4 Dashboards/Visualizations APIs, Lens or ES|QL. Dashboards are three kinds of
saved object, which `apply` writes in one `_bulk_create` call:

- `index-pattern`: `title`, `timeFieldName`, **and a cached `fields` list**. A pattern without fields
  renders every panel as "Could not locate that index-pattern-field". `apply` creates patterns with OSD's
  own field list and refreshes a referenced pattern when a field a panel uses is missing from it.
- `visualization`: a `visState` (type + aggs + params) and a `searchSourceJSON` pointing at the index
  pattern. The spec compiler writes one per panel with id `<dashboard-id>--<panel-id>`.
- `dashboard`: `panelsJSON` (grid + panel refs), `optionsJSON`, time restore.

OSD stores `visState`, `panelsJSON`, `optionsJSON`, `uiStateJSON` and `searchSourceJSON` as JSON strings.
`get -o` writes a **bundle** with them decoded so they are readable and editable; `apply` re-encodes them.

## Tenants (security plugin)

Saved objects live in a tenant. `--tenant private` (`__user__`) is the user's own sandbox, `global` is shared
with everyone, and named tenants are shared with their members. Index patterns are per tenant too, so a
dashboard copied to a new tenant needs its patterns there (`copy` exports them as references).

- Build and iterate in `--tenant private`, then promote: `copy <id> --to-tenant global`.
- The view URL must put `?security_tenant=…` **before** the `#` (`/app/dashboards?security_tenant=private#/view/<id>`);
  inside the hash it is ignored and OSD reports the dashboard as missing. `apply`/`get` print the correct URL.

## Queries

There is no ES|QL. `query` runs PPL (`source=idx | where … | stats count() by f`) or SQL
(`SELECT … FROM idx`). `validate` uses aggregation DSL, the same aggregations the panels will run.

## Limitations of classic visualizations

- No per-panel number formats; formats come from the index pattern's `fieldFormatMap`.
- No legend statistics, metric trend backgrounds, gradient areas, treemap/waffle/mosaic (→ donut).
- vislib derives axis titles from the aggregation ("created_at per 3 hours"); they cannot be fully hidden.
- Gauges need a numeric range; without `min`/`max` the 0–100 bands are used (fine for percentages).
- Input controls on very large indices show a ⚠ "Terms list might be incomplete because the request is taking
  too long". This is OSD's autocomplete timeout (`opensearch_dashboards.yml`), not a broken control.

## Hand-editing a bundle

```json
{
  "kind": "osd_bundle",
  "dashboard": {"type": "dashboard", "id": "...", "attributes": {"title": "...", "panelsJSON": [
      {"gridData": {"x": 0, "y": 0, "w": 24, "h": 10, "i": "1"}, "panelIndex": "1",
       "embeddableConfig": {}, "panelRefName": "panel_0"}], "...": "..."},
    "references": [{"name": "panel_0", "type": "visualization", "id": "..."}]},
  "visualizations": [{"type": "visualization", "id": "...", "attributes": {"title": "...",
      "visState": {"type": "line", "aggs": [...], "params": {...}}, "...": "..."}, "references": [...]}]
}
```

Keep `panelRefName` ↔ `references[].name` in sync. `layout <bundle>` repacks grid coordinates after
resizing or removing panels. Deleting a panel from `panelsJSON` leaves its visualization object behind;
`apply` removes generated (`<dashboard-id>--…`) visualizations the dashboard no longer references.
