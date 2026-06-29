# TestRail API v2 — Endpoint Reference

This is the per-endpoint detail behind the `testrail_api.py` CLI. The official
docs live at <https://support.testrail.com/hc/en-us/articles/7077083596436-API-Reference>.

## Base URL & authentication

```
<TESTRAIL_URL>/index.php?/api/v2/<endpoint>
```

- **Auth:** HTTP Basic with `TESTRAIL_USER` : `TESTRAIL_API_KEY` (API key from
  *My Settings → API Keys*, or the account password). Header form:
  `Authorization: Basic base64(user:key)`.
- **Query params:** appended with `&` because `?` already precedes `/api/v2`.
  Example: `get_cases/5&suite_id=12&limit=50&offset=0`.
- **Content type:** `application/json` for POST bodies.

## Pagination

TestRail **6.7+** wraps list responses:

```json
{ "offset": 0, "limit": 250, "size": 250,
  "_links": { "next": "/api/v2/get_cases/5&limit=250&offset=250", "prev": null },
  "cases": [ ... ] }
```

Follow `_links.next` (a path under `/api/v2/`) until it's `null`. Page size caps
at **250**. Older instances return a bare JSON array with no envelope. The CLI's
`get_list()` handles both; `--limit` is a client-side cap on total rows returned.

## Endpoints used by the CLI

### Projects
| CLI | Method | Endpoint |
|-----|--------|----------|
| `list-projects` | GET | `get_projects` (`&is_completed=0/1`) |
| `get-project` | GET | `get_project/{project_id}` |

### Suites & sections
| CLI | Method | Endpoint |
|-----|--------|----------|
| `list-suites` | GET | `get_suites/{project_id}` |
| `get-suite` | GET | `get_suite/{suite_id}` |
| `add-suite` | POST | `add_suite/{project_id}` — `{name, description?}` |
| `list-sections` | GET | `get_sections/{project_id}` (`&suite_id=`) |
| `add-section` | POST | `add_section/{project_id}` — `{name, suite_id?, parent_id?, description?}` |

> **Suite modes.** A project is single-suite, single-suite+baselines, or
> multi-suite (`suite_mode` 1/2/3). For multi-suite projects, `get_cases` and
> run creation generally require a `suite_id`.

### Cases
| CLI | Method | Endpoint |
|-----|--------|----------|
| `list-cases` | GET | `get_cases/{project_id}` (`&suite_id=&section_id=&limit=&offset=&filter=`) |
| `get-case` | GET | `get_case/{case_id}` |
| `add-case` | POST | `add_case/{section_id}` |
| `update-case` | POST | `update_case/{case_id}` |
| `update-cases` | POST | `update_cases/{suite_id}` — body includes `case_ids: []` |
| `delete-case` | POST | `delete_case/{case_id}` (irreversible) |

Common case fields: `title`, `template_id` (2 = "Test Case (Steps)"), `type_id`,
`priority_id` (1–4), `refs`, `custom_preconds`, and `custom_steps_separated`
(array of `{content, expected}`). Custom fields are prefixed `custom_` and vary
per instance — inspect an existing case with `get-case` to see the field names.

### Runs
| CLI | Method | Endpoint |
|-----|--------|----------|
| `list-runs` | GET | `get_runs/{project_id}` (`&is_completed=0/1`) |
| `get-run` | GET | `get_run/{run_id}` |
| `add-run` | POST | `add_run/{project_id}` — `{name, suite_id?, description?, milestone_id?, include_all, case_ids?}` |
| `update-run` | POST | `update_run/{run_id}` |
| `close-run` | POST | `close_run/{run_id}` (archives results; irreversible) |
| `delete-run` | POST | `delete_run/{run_id}` (irreversible) |

`include_all=true` puts every case in the suite into the run; set it `false` and
pass `case_ids` to scope the run. You can only add results for cases that are in
the run.

### Tests & results
| CLI | Method | Endpoint |
|-----|--------|----------|
| `list-tests` | GET | `get_tests/{run_id}` |
| `get-test` | GET | `get_test/{test_id}` |
| `add-result` | POST | `add_result/{test_id}` |
| `add-result-for-case` | POST | `add_result_for_case/{run_id}/{case_id}` |
| `add-results` (bulk) | POST | `add_results_for_cases/{run_id}` — `{results: [{case_id, status_id, ...}]}` |
| `get-results` | GET | `get_results/{test_id}` |
| `get-results-for-case` | GET | `get_results_for_case/{run_id}/{case_id}` |
| `get-results-for-run` | GET | `get_results_for_run/{run_id}` |

Result fields: `status_id` (1 Passed, 2 Blocked, 3 Untested\*, 4 Retest, 5
Failed; \*cannot be posted), `comment`, `elapsed` (`"30s"`, `"1m 45s"` — **never
"0s"**, TestRail rejects it), `defects` (comma-separated IDs), `version`, and any
`custom_*` result fields. A "test" is a case's instance inside a specific run;
`test_id` differs from `case_id`.

### Plans & milestones
| CLI | Method | Endpoint |
|-----|--------|----------|
| `list-plans` / `get-plan` | GET | `get_plans/{project_id}` / `get_plan/{plan_id}` |
| `add-plan` | POST | `add_plan/{project_id}` — `{name, description?, milestone_id?}` |
| `add-plan-entry` | POST | `add_plan_entry/{plan_id}` — `{suite_id, name?, include_all?, case_ids?}` |
| `close-plan` | POST | `close_plan/{plan_id}` |
| `list-milestones` / `get-milestone` | GET | `get_milestones/{project_id}` / `get_milestone/{milestone_id}` |
| `add-milestone` | POST | `add_milestone/{project_id}` — `{name, description?, due_on?}` |

A **plan** groups several runs (entries); each entry is a run over a suite. Use
plans when one test cycle spans multiple configurations/suites.

### Users & metadata
| CLI | Method | Endpoint |
|-----|--------|----------|
| `list-users` | GET | `get_users` (`/{project_id}` on newer instances) |
| `get-user` | GET | `get_user/{user_id}` |
| `get-user-by-email` | GET | `get_user_by_email&email=...` |
| `list-statuses` | GET | `get_statuses` |
| `list-priorities` | GET | `get_priorities` |
| `list-case-types` | GET | `get_case_types` |
| `list-templates` | GET | `get_templates/{project_id}` |

## Error envelope

Non-2xx responses return `{"error": "message"}`. Notable statuses:

- **400** — bad request (e.g. result for a case not in the run; malformed body).
- **401** — auth failed (bad/expired key, or API disabled instance-wide).
- **403** — no permission for the project/entity.
- **429** — rate limited (TestRail Cloud throttles; back off and retry).

## Playwright JSON report shape (consumed by `report-playwright`)

`npx playwright test --reporter=json` emits:

```jsonc
{
  "suites": [
    {
      "title": "file or describe title",
      "specs": [
        {
          "title": "test title (may contain C1234)",
          "ok": true,
          "tests": [
            { "results": [ { "status": "passed|failed|timedOut|interrupted|skipped",
                             "duration": 1234,  // ms
                             "error": { "message": "..." } } ] }
          ]
        }
      ],
      "suites": [ /* nested describes */ ]
    }
  ]
}
```

The parser walks `suites` recursively, collects `specs`, extracts `C\d+` case
IDs (spec title first, then enclosing describe titles), uses each test's **final**
`results` entry as the authoritative outcome, sums `duration` across attempts,
and flags a test as flaky when it passes only after an earlier non-pass attempt.
