#!/usr/bin/env python3
"""Side-conversation report for Zendesk Support.

Analyzes side conversations that a support agent opens from a parent ticket and
routes to a target group (e.g. "KYC Support"). In Zendesk, a side conversation
routed to a group creates a child ticket in that group with
``via.channel = "side_conversation"`` -- those child tickets are the population
this report measures.

It answers four questions over a date range:

  1. How many side conversations are opened per day (calendar / working / active).
  2. How long they take from open to close (full resolution time: mean/median/p90).
  3. How many "ping-pongs" (message round-trips) each one has.
  4. What topics they are about (keyword taxonomy, tunable via --topics).

Outputs any of html / csv / md / json into a report directory, e.g.

    python3 side-conversation-report.py \\
        --start-date 2026-01-01 --end-date 2026-10-11 \\
        --group "KYC Support" \\
        --report .artifacts/side-conversation-report

Credentials come from the same environment variables as the sibling
``zendesk_api.py`` (ZENDESK_SUBDOMAIN / ZENDESK_EMAIL / ZENDESK_API_TOKEN) and
its transport/auth layer is reused directly. The script only ever reads from
Zendesk -- it never writes.

Standard library only; no pip install required.
"""

import argparse
import csv
import html
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import zendesk_api as zd  # noqa: E402  (reuse ZendeskClient + transport)

DEFAULT_GROUP = "KYC Support"
DEFAULT_CHANNEL = "side_conversation"
DEFAULT_REPORT_DIR = ".artifacts/side-conversation-report"
DEFAULT_BASENAME = "report"
DEFAULT_FORMATS = "html,csv,md,json"
DEFAULT_WEEKEND = "fri,sat"  # Israeli work week (Sun-Thu); override with --weekend
SHOW_MANY_CHUNK = 100
SOLVED_STATUSES = ("solved", "closed")
WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def eprint(message):
    """Write a diagnostic line to stderr (never pollutes machine-readable stdout)."""
    print(message, file=sys.stderr)


def parse_iso(value):
    """Parse a Zendesk ISO-8601 timestamp into an aware UTC datetime, or None."""
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def percentile(sorted_values, pct):
    """Linear-interpolation percentile over a pre-sorted list (pct in 0..100)."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_values[int(rank)]
    fraction = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction


def median(values):
    """Median of an unsorted numeric list, or None when empty."""
    if not values:
        return None
    return percentile(sorted(values), 50)


def mean(values):
    """Arithmetic mean, or None when empty."""
    return sum(values) / len(values) if values else None


def round1(value):
    """Round to one decimal, preserving None."""
    return None if value is None else round(value, 1)


# --------------------------------------------------------------------------- #
# Zendesk access (with light retry/backoff on 429 + 5xx)
# --------------------------------------------------------------------------- #

def call_with_retry(func, *args, **kwargs):
    """Run a client call, retrying transient 429/5xx with exponential backoff."""
    max_attempts = 5
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except zd.ApiError as err:
            status = getattr(err, "status_code", None)
            is_transient = status == 429 or (status is not None and 500 <= status < 600)
            if not is_transient or attempt == max_attempts:
                raise
            eprint("  rate-limited/transient (HTTP {}); retry {}/{} in {:.0f}s".format(
                status, attempt, max_attempts - 1, delay))
            time.sleep(delay)
            delay *= 2
    return None


def month_windows(start, end):
    """Yield (window_start, window_end) date pairs, one per calendar month.

    Zendesk Search caps a single query at 1000 results; chunking the range by
    month keeps each sub-query under that cap for high-volume groups.
    """
    cursor = start
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        window_end = min(end, next_month - timedelta(days=1))
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def build_query(group, channel, date_field, window_start, window_end):
    """Build a Zendesk Search query for side-conversation child tickets."""
    parts = [
        "type:ticket",
        'via:{}'.format(channel),
        'group:"{}"'.format(group),
        "{}>={}".format(date_field, window_start.isoformat()),
        "{}<={}".format(date_field, window_end.isoformat()),
    ]
    return " ".join(parts)


def search_all(client, query):
    """Run a search and follow next_page pagination, returning all result rows."""
    results = []
    data = call_with_retry(client.search, query, per_page=100)
    results.extend(data.get("results", []))
    nxt = data.get("next_page")
    while nxt:
        data = call_with_retry(client._get, nxt)
        results.extend(data.get("results", []))
        nxt = data.get("next_page")
    return results


def fetch_tickets(client, group, channel, date_field, start, end):
    """Fetch all matching side-conversation tickets across the date range."""
    seen = {}
    for window_start, window_end in month_windows(start, end):
        query = build_query(group, channel, date_field, window_start, window_end)
        eprint("  search {} -> {} : {}".format(window_start, window_end, query))
        for row in search_all(client, query):
            if row.get("result_type") == "ticket" and row.get("id"):
                seen[row["id"]] = row
    return list(seen.values())


def attach_metric_sets(client, tickets):
    """Re-fetch tickets via show_many with metric_sets sideload; index metrics by ticket id."""
    ids = [t["id"] for t in tickets]
    metrics_by_id = {}
    for i in range(0, len(ids), SHOW_MANY_CHUNK):
        chunk = ids[i:i + SHOW_MANY_CHUNK]
        data = call_with_retry(
            client._get, "/tickets/show_many.json",
            {"ids": ",".join(str(x) for x in chunk), "include": "metric_sets"})
        for metric in data.get("metric_sets", []):
            tid = metric.get("ticket_id")
            if tid:
                metrics_by_id[tid] = metric
    return metrics_by_id


def fetch_comment_counts(client, tickets, max_workers):
    """Fetch comment count + first comment body per ticket, concurrently."""
    result = {}

    def work(ticket_id):
        comments = call_with_retry(client.get_all_ticket_comments, ticket_id, sort_order="asc")
        first_body = ""
        for comment in comments:
            body = (comment.get("plain_body") or comment.get("body") or "").strip()
            if body:
                first_body = body
                break
        authors = {c.get("author_id") for c in comments if c.get("author_id")}
        return ticket_id, {"count": len(comments), "first_body": first_body, "participants": len(authors)}

    total = len(tickets)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(work, t["id"]): t["id"] for t in tickets}
        for future in as_completed(futures):
            ticket_id, info = future.result()
            result[ticket_id] = info
            done += 1
            if done % 50 == 0 or done == total:
                eprint("  comments {}/{}".format(done, total))
    return result


# --------------------------------------------------------------------------- #
# Topic classification
# --------------------------------------------------------------------------- #

def load_topics(path):
    """Load the ordered topic taxonomy; returns a list of category dicts."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    categories = data.get("categories", [])
    for category in categories:
        category["_keywords_lc"] = [k.lower() for k in category.get("keywords", [])]
    return categories


def classify_topic(text, categories):
    """Return the key of the first category whose keyword appears in text, else 'other'."""
    haystack = (text or "").lower()
    for category in categories:
        for keyword in category["_keywords_lc"]:
            if keyword and keyword in haystack:
                return category["key"]
    return "other"


# --------------------------------------------------------------------------- #
# Record assembly + summary computation
# --------------------------------------------------------------------------- #

def resolution_minutes(metric):
    """Pull calendar full-resolution minutes from a metric_set, or None."""
    if not metric:
        return None
    value = metric.get("full_resolution_time_in_minutes")
    if isinstance(value, dict):
        return value.get("calendar")
    return value


def build_records(tickets, metrics_by_id, comments_by_id, categories, tz):
    """Combine ticket + metrics + comments into flat per-conversation records."""
    records = []
    for ticket in tickets:
        tid = ticket["id"]
        metric = metrics_by_id.get(tid)
        comment_info = comments_by_id.get(tid, {})
        created = parse_iso(ticket.get("created_at"))
        solved = parse_iso((metric or {}).get("solved_at"))
        res_min = resolution_minutes(metric)
        status = ticket.get("status")
        subject = ticket.get("subject") or ""
        first_body = comment_info.get("first_body", "")
        comment_count = comment_info.get("count")
        if comment_count is None and metric is not None:
            replies = metric.get("replies")
            comment_count = (replies + 1) if isinstance(replies, int) else None
        local_day = created.astimezone(tz).date().isoformat() if created else None
        records.append({
            "id": tid,
            "created_at": ticket.get("created_at"),
            "solved_at": (metric or {}).get("solved_at"),
            "status": status,
            "day": local_day,
            "resolution_hours": round1(res_min / 60.0) if isinstance(res_min, (int, float)) else None,
            "comments": comment_count,
            "pingpongs": round1(comment_count / 2.0) if isinstance(comment_count, (int, float)) else None,
            "participants": comment_info.get("participants"),
            "submitter_id": ticket.get("submitter_id"),
            "topic": classify_topic(subject + "\n" + first_body, categories),
            "subject": subject,
        })
    return records


def working_days_between(start, end, weekend_indices):
    """Count days in [start, end] whose weekday is not a weekend day."""
    count = 0
    cursor = start
    while cursor <= end:
        if cursor.weekday() not in weekend_indices:
            count += 1
        cursor += timedelta(days=1)
    return count


def summarize(records, categories, start, end, weekend_indices):
    """Compute all four answers plus supporting breakdowns from the records."""
    total = len(records)
    by_day = {}
    by_month = {}
    for record in records:
        day = record["day"]
        if not day:
            continue
        by_day[day] = by_day.get(day, 0) + 1
        month = day[:7]
        by_month[month] = by_month.get(month, 0) + 1
    calendar_days = (end - start).days + 1
    working_days = working_days_between(start, end, weekend_indices)
    active_days = len(by_day)
    resolution_hours = [r["resolution_hours"] for r in records
                        if r["resolution_hours"] is not None and r["status"] in SOLVED_STATUSES]
    comment_counts = [r["comments"] for r in records if isinstance(r["comments"], int)]
    participants = [r["participants"] for r in records if isinstance(r["participants"], int)]
    open_pending = sum(1 for r in records if r["status"] not in SOLVED_STATUSES)
    single_roundtrip = sum(1 for c in comment_counts if c <= 2)
    topic_counts = {}
    order = [c["key"] for c in categories] + ["other"]
    for record in records:
        topic_counts[record["topic"]] = topic_counts.get(record["topic"], 0) + 1
    topics_ranked = sorted(
        ([k, topic_counts.get(k, 0)] for k in order if topic_counts.get(k, 0) > 0),
        key=lambda kv: kv[1], reverse=True)
    status_counts = {}
    for record in records:
        status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1
    comment_hist = {}
    for count in comment_counts:
        comment_hist[count] = comment_hist.get(count, 0) + 1
    return {
        "scope": {
            "total": total,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "calendar_days": calendar_days,
            "working_days": working_days,
            "active_days": active_days,
            "data_window": "{}..{}".format(
                min(by_day) if by_day else start.isoformat(),
                max(by_day) if by_day else end.isoformat()),
        },
        "q1_daily": {
            "per_calendar_day": round(total / calendar_days, 2) if calendar_days else None,
            "per_working_day": round(total / working_days, 2) if working_days else None,
            "per_active_day": round(total / active_days, 2) if active_days else None,
            "by_month": dict(sorted(by_month.items())),
        },
        "q2_resolution_hours": {
            "n_closed": len(resolution_hours),
            "mean": round1(mean(resolution_hours)),
            "median": round1(median(resolution_hours)),
            "p90": round1(percentile(sorted(resolution_hours), 90)) if resolution_hours else None,
            "still_open_or_pending": open_pending,
        },
        "q3_pingpong": {
            "n_with_comments": len(comment_counts),
            "avg_comments": round(mean(comment_counts), 2) if comment_counts else None,
            "median_comments": median(comment_counts),
            "avg_pingpongs": round(mean(comment_counts) / 2.0, 2) if comment_counts else None,
            "pct_single_roundtrip": round(100.0 * single_roundtrip / len(comment_counts), 1) if comment_counts else None,
            "avg_participants": round(mean(participants), 2) if participants else None,
            "comment_histogram": dict(sorted(comment_hist.items())),
        },
        "q4_topics": {k: v for k, v in topics_ranked},
        "status_counts": dict(sorted(status_counts.items(), key=lambda kv: -kv[1])),
    }


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #

def topic_label(categories, key, hebrew):
    """Human label for a topic key (falls back to the key itself)."""
    if key == "other":
        return "אחר / לא מסווג" if hebrew else "Other / unclassified"
    for category in categories:
        if category["key"] == key:
            return category.get("label_he") if hebrew and category.get("label_he") else category.get("label", key)
    return key


def write_json(path, summary, config):
    """Write the structured summary + run config as JSON."""
    payload = {"generated_at": config["generated_at"], "config": config["public"], **summary}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def write_csv(path, records):
    """Write one row per side conversation (raw data behind the report)."""
    fields = ["id", "created_at", "solved_at", "status", "day", "resolution_hours",
              "comments", "pingpongs", "participants", "submitter_id", "topic", "subject"]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in sorted(records, key=lambda r: r.get("created_at") or ""):
            writer.writerow(record)


def write_markdown(path, summary, categories, config):
    """Write a human-readable Markdown report with a mermaid topic pie."""
    s = summary
    hebrew = config["hebrew"]
    lines = []
    lines.append("# {}".format(config["title"]))
    lines.append("")
    lines.append("**Generated:** {} · **Scope:** `via:{}` → group `{}`".format(
        config["generated_at"], config["public"]["channel"], config["public"]["group"]))
    lines.append("**Range:** {} → {} ({} calendar days, {} with data) · **Total:** {} side conversations".format(
        s["scope"]["start"], s["scope"]["end"], s["scope"]["calendar_days"],
        s["scope"]["active_days"], s["scope"]["total"]))
    lines.append("")
    lines.append("## 1. Side conversations opened per day")
    lines.append("")
    lines.append("| Basis | Average/day |")
    lines.append("|---|---:|")
    lines.append("| Per calendar day | **{}** |".format(s["q1_daily"]["per_calendar_day"]))
    lines.append("| Per working day | **{}** |".format(s["q1_daily"]["per_working_day"]))
    lines.append("| Per active day | {} |".format(s["q1_daily"]["per_active_day"]))
    lines.append("")
    lines.append("**By month:** " + " · ".join(
        "{} {}".format(m, c) for m, c in s["q1_daily"]["by_month"].items()))
    lines.append("")
    lines.append("## 2. Open → close time (full resolution)")
    lines.append("")
    q2 = s["q2_resolution_hours"]
    lines.append("Measured on **{} closed** conversations (of {}).".format(q2["n_closed"], s["scope"]["total"]))
    lines.append("")
    lines.append("| Metric | Hours |")
    lines.append("|---|---:|")
    lines.append("| Mean | {} |".format(q2["mean"]))
    lines.append("| Median | **{}** |".format(q2["median"]))
    lines.append("| P90 | {} |".format(q2["p90"]))
    lines.append("")
    lines.append("> {} still open/pending (excluded from the timing stats). The median is the honest typical experience; the mean is skewed by a long tail.".format(
        q2["still_open_or_pending"]))
    lines.append("")
    lines.append("## 3. Ping-pongs per conversation")
    lines.append("")
    q3 = s["q3_pingpong"]
    lines.append("*One ping-pong = one message + one reply = 2 comments.*")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append("| Avg comments | {} |".format(q3["avg_comments"]))
    lines.append("| Median comments | {} |".format(q3["median_comments"]))
    lines.append("| Avg ping-pongs | **{}** |".format(q3["avg_pingpongs"]))
    lines.append("| Single round-trip (≤2 comments) | {}% |".format(q3["pct_single_roundtrip"]))
    lines.append("| Avg participants | {} |".format(q3["avg_participants"]))
    lines.append("")
    lines.append("## 4. Topic breakdown")
    lines.append("")
    lines.append("| # | Topic | Count | Share |")
    lines.append("|---|---|---:|---:|")
    total = s["scope"]["total"] or 1
    for i, (key, count) in enumerate(s["q4_topics"].items(), 1):
        lines.append("| {} | {} | {} | {}% |".format(
            i, topic_label(categories, key, hebrew), count, round(100.0 * count / total, 1)))
    lines.append("")
    lines.append("```mermaid")
    lines.append("pie showData")
    lines.append('    title Topic breakdown')
    for key, count in s["q4_topics"].items():
        label = topic_label(categories, key, False).replace('"', "'")
        lines.append('    "{}" : {}'.format(label, count))
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Topic classification is keyword-based (tunable via `--topics`); treat it as directional._")
    lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _hbars(items, max_value, label_fn):
    """Render a list of (key, value) as horizontal CSS bars."""
    rows = []
    safe_max = max_value or 1
    for key, value in items:
        pct = 100.0 * value / safe_max
        rows.append(
            '<div class="bar-row"><div class="name">{}</div>'
            '<div class="bar-track"><div class="bar-fill" style="width:{:.1f}%"></div></div>'
            '<div class="val">{}</div></div>'.format(html.escape(label_fn(key)), pct, value))
    return "\n".join(rows)


def _vbars(items):
    """Render a list of (label, value) as vertical CSS bars."""
    max_value = max((v for _, v in items), default=1) or 1
    cols = []
    for label, value in items:
        height = 100.0 * value / max_value
        cols.append(
            '<div class="vbar"><div class="n">{}</div>'
            '<div class="col" style="height:{:.1f}%"></div>'
            '<div class="x">{}</div></div>'.format(value, height, html.escape(label)))
    return '<div class="vbars">' + "\n".join(cols) + "</div>"


def _tile(label, value, unit="", sub=""):
    """Render one stat tile."""
    unit_html = '<small>{}</small>'.format(html.escape(unit)) if unit else ""
    sub_html = '<div class="sub">{}</div>'.format(html.escape(sub)) if sub else ""
    return ('<div class="tile"><div class="label">{}</div>'
            '<div class="value">{}{}</div>{}</div>').format(
        html.escape(label), html.escape(str(value)), unit_html, sub_html)


def write_html(path, summary, categories, config):
    """Render the visual dashboard from the HTML template."""
    s = summary
    hebrew = config["hebrew"]
    template_path = os.path.join(SCRIPT_DIR, "report_templates", "report.html.tmpl")
    with open(template_path, "r", encoding="utf-8") as handle:
        template = handle.read()
    q1, q2, q3 = s["q1_daily"], s["q2_resolution_hours"], s["q3_pingpong"]
    tiles = "".join([
        _tile("Total side conversations", s["scope"]["total"], "",
              "{} with data".format(s["scope"]["active_days"]) + " days"),
        _tile("Per working day", q1["per_working_day"], "/day",
              "{} / calendar day".format(q1["per_calendar_day"])),
        _tile("Median resolution", q2["median"] if q2["median"] is not None else "n/a", "h",
              "mean {}h · p90 {}h".format(q2["mean"], q2["p90"])),
        _tile("Avg ping-pongs", q3["avg_pingpongs"] if q3["avg_pingpongs"] is not None else "n/a", "",
              "{}% one round-trip".format(q3["pct_single_roundtrip"])),
    ])
    volume_chart = _vbars([(m, c) for m, c in q1["by_month"].items()]) or "<p class='hint'>No data.</p>"
    total = s["scope"]["total"] or 1
    topic_items = list(s["q4_topics"].items())
    topic_max = max((v for _, v in topic_items), default=1)
    topic_chart = _hbars(topic_items, topic_max, lambda k: topic_label(categories, k, hebrew)) \
        or "<p class='hint'>No data.</p>"
    hist_items = [("{} msg".format(k), v) for k, v in q3["comment_histogram"].items()]
    pingpong_chart = _vbars(hist_items[:12]) if hist_items else "<p class='hint'>Run without --no-comments for ping-pong detail.</p>"
    res_rows = "".join([
        "<tr><td>Mean</td><td class='num'>{}</td></tr>".format(q2["mean"]),
        "<tr><td>Median</td><td class='num'>{}</td></tr>".format(q2["median"]),
        "<tr><td>P90</td><td class='num'>{}</td></tr>".format(q2["p90"]),
        "<tr><td>Closed (measured)</td><td class='num'>{}</td></tr>".format(q2["n_closed"]),
        "<tr><td>Still open / pending</td><td class='num'>{}</td></tr>".format(q2["still_open_or_pending"]),
    ])
    resolution_block = (
        "<h3>Open → close time (hours)</h3>"
        "<p class='hint'>Full resolution time (created → solved), calendar clock. "
        "Median is the honest typical; mean is skewed by a long tail.</p>"
        "<table><thead><tr><th>Metric</th><th class='num'>Hours</th></tr></thead>"
        "<tbody>{}</tbody></table>".format(res_rows))
    topic_rows = "".join(
        "<tr><td>{}</td><td class='num'>{}</td><td class='num'>{}%</td></tr>".format(
            html.escape(topic_label(categories, k, hebrew)), v, round(100.0 * v / total, 1))
        for k, v in topic_items)
    topic_chart += ("<table style='margin-top:18px'><thead><tr><th>Topic</th>"
                    "<th class='num'>Count</th><th class='num'>Share</th></tr></thead>"
                    "<tbody>{}</tbody></table>".format(topic_rows))
    notes = ""
    if config.get("notes"):
        note_items = "".join("<li>{}</li>".format(html.escape(n)) for n in config["notes"])
        notes = '<div class="notes"><strong>Notes &amp; limitations</strong><ul>{}</ul></div>'.format(note_items)
    scope_line = ('Side conversations <code>via:{}</code> → group <code>{}</code> · '
                  '{} → {} · {} conversations').format(
        html.escape(config["public"]["channel"]), html.escape(config["public"]["group"]),
        s["scope"]["start"], s["scope"]["end"], s["scope"]["total"])
    footer = "Generated {} · Zendesk Support API · read-only · topic taxonomy: {}".format(
        config["generated_at"], os.path.basename(config["public"]["topics"]))
    replacements = {
        "{{DIR}}": "rtl" if hebrew else "ltr",
        "{{TITLE}}": html.escape(config["title"]),
        "{{SCOPE_LINE}}": scope_line,
        "{{L_HEADLINE}}": "תמצית" if hebrew else "Headline",
        "{{STAT_TILES}}": tiles,
        "{{L_VOLUME}}": "נפח" if hebrew else "Volume",
        "{{L_VOLUME_TITLE}}": "שיחות שנפתחו לפי חודש" if hebrew else "Opened per month",
        "{{L_VOLUME_HINT}}": "ספירה גולמית לכל חודש בטווח." if hebrew else "Raw count per calendar month in range.",
        "{{VOLUME_CHART}}": volume_chart,
        "{{L_TOPICS}}": "נושאים" if hebrew else "Topics",
        "{{L_TOPICS_TITLE}}": "פילוח נושאים" if hebrew else "Topic breakdown",
        "{{L_TOPICS_HINT}}": "סיווג מילות מפתח (ניתן לכוונון)." if hebrew else "Keyword classification (tunable via --topics).",
        "{{TOPIC_CHART}}": topic_chart,
        "{{L_PINGPONG}}": "פינג-פונג" if hebrew else "Ping-pong",
        "{{L_PINGPONG_TITLE}}": "התפלגות מספר ההודעות" if hebrew else "Message-count distribution",
        "{{L_PINGPONG_HINT}}": "פינג-פונג אחד = 2 הודעות." if hebrew else "One ping-pong = 2 comments.",
        "{{PINGPONG_CHART}}": pingpong_chart,
        "{{L_RESOLUTION}}": "זמן טיפול" if hebrew else "Resolution time",
        "{{RESOLUTION_BLOCK}}": resolution_block,
        "{{NOTES}}": notes,
        "{{FOOTER}}": html.escape(footer),
    }
    for needle, value in replacements.items():
        template = template.replace(needle, value)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(template)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Zendesk side-conversation report (volume, resolution time, ping-pong, topics).")
    parser.add_argument("--start-date", required=True, help="Window start, YYYY-MM-DD (inclusive).")
    parser.add_argument("--end-date", help="Window end, YYYY-MM-DD (inclusive). Default: today.")
    parser.add_argument("--group", default=DEFAULT_GROUP, help="Target group name. Default: %(default)s")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="via: channel. Default: %(default)s")
    parser.add_argument("--date-field", default="created", choices=["created", "updated", "solved"],
                        help="Date field the window filters on. Default: %(default)s")
    parser.add_argument("--report", default=DEFAULT_REPORT_DIR, dest="report_dir",
                        help="Output directory. Default: %(default)s")
    parser.add_argument("--basename", default=DEFAULT_BASENAME, help="Output file stem. Default: %(default)s")
    parser.add_argument("--formats", default=DEFAULT_FORMATS,
                        help="Comma list of html,csv,md,json. Default: %(default)s")
    parser.add_argument("--topics", default=os.path.join(SCRIPT_DIR, "topics.json"),
                        help="Path to topic taxonomy JSON. Default: bundled topics.json")
    parser.add_argument("--weekend", default=DEFAULT_WEEKEND,
                        help="Comma days treated as weekend for the working-day average "
                             "(mon..sun). Default: %(default)s (Israel).")
    parser.add_argument("--timezone", default="UTC",
                        help="IANA tz for day bucketing (e.g. Asia/Jerusalem). Default: UTC.")
    parser.add_argument("--no-comments", action="store_true",
                        help="Skip per-ticket comment fetch; approximate ping-pong from metric replies "
                             "and classify topics from the subject only (much faster).")
    parser.add_argument("--max-workers", type=int, default=6, help="Concurrency for comment fetch. Default: %(default)s")
    parser.add_argument("--limit", type=int, help="Cap the number of tickets analyzed (sampling/debug).")
    parser.add_argument("--title", help="Report title. Default is derived from the group.")
    parser.add_argument("--hebrew", action="store_true", help="Hebrew (RTL) labels in HTML + Markdown.")
    parser.add_argument("--note", action="append", default=[], dest="notes",
                        help="Add a note/limitation line to the report (repeatable).")
    parser.add_argument("--print-query", action="store_true", help="Print the search queries and exit.")
    return parser.parse_args(argv)


def resolve_timezone(name):
    """Return a tzinfo for the given IANA name, falling back to UTC."""
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        eprint("Warning: timezone {!r} unavailable; using UTC.".format(name))
        return timezone.utc


def main(argv):
    """Entry point: fetch, compute, render."""
    args = parse_args(argv)
    start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else datetime.now(timezone.utc).date()
    if end < start:
        eprint("Error: --end-date is before --start-date.")
        return 2
    weekend_indices = {WEEKDAY_INDEX[d.strip().lower()] for d in args.weekend.split(",")
                       if d.strip().lower() in WEEKDAY_INDEX}
    tz = resolve_timezone(args.timezone)
    if args.print_query:
        for window_start, window_end in month_windows(start, end):
            print(build_query(args.group, args.channel, args.date_field, window_start, window_end))
        return 0
    categories = load_topics(args.topics)
    try:
        client = zd.ZendeskClient()
    except zd.ConfigurationError as err:
        eprint("Configuration Error: {}".format(err))
        return 2
    eprint("Fetching side conversations: group={!r} channel={!r} {}..{}".format(
        args.group, args.channel, start, end))
    tickets = fetch_tickets(client, args.group, args.channel, args.date_field, start, end)
    eprint("Found {} tickets.".format(len(tickets)))
    if args.limit and len(tickets) > args.limit:
        tickets = tickets[:args.limit]
        eprint("Limited to {} tickets.".format(len(tickets)))
    if not tickets:
        eprint("No matching side conversations found for the given scope/range.")
    metrics_by_id = attach_metric_sets(client, tickets) if tickets else {}
    comments_by_id = {}
    if tickets and not args.no_comments:
        eprint("Fetching comments for {} tickets ({} workers)...".format(len(tickets), args.max_workers))
        comments_by_id = fetch_comment_counts(client, tickets, args.max_workers)
    records = build_records(tickets, metrics_by_id, comments_by_id, categories, tz)
    summary = summarize(records, categories, start, end, weekend_indices)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = args.title or ("שיחות צדדיות — {}".format(args.group) if args.hebrew
                           else "Side Conversations to {}".format(args.group))
    notes = list(args.notes)
    if args.no_comments:
        notes.append("Ping-pong approximated from metric replies and topics from subject only (--no-comments).")
    config = {
        "generated_at": generated_at,
        "title": title,
        "hebrew": args.hebrew,
        "notes": notes,
        "public": {
            "group": args.group, "channel": args.channel, "date_field": args.date_field,
            "start": start.isoformat(), "end": end.isoformat(), "timezone": args.timezone,
            "weekend": args.weekend, "topics": args.topics, "no_comments": args.no_comments,
        },
    }
    os.makedirs(args.report_dir, exist_ok=True)
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    written = []
    stem = os.path.join(args.report_dir, args.basename)
    if "json" in formats:
        write_json(stem + ".json", summary, config)
        written.append(stem + ".json")
    if "csv" in formats:
        write_csv(stem + ".csv", records)
        written.append(stem + ".csv")
    if "md" in formats:
        write_markdown(stem + ".md", summary, categories, config)
        written.append(stem + ".md")
    if "html" in formats:
        write_html(stem + ".html", summary, categories, config)
        written.append(stem + ".html")
    print(json.dumps({
        "ok": True,
        "scope": summary["scope"],
        "q1_daily": summary["q1_daily"],
        "q2_resolution_hours": summary["q2_resolution_hours"],
        "q3_pingpong": {k: v for k, v in summary["q3_pingpong"].items() if k != "comment_histogram"},
        "q4_topics": summary["q4_topics"],
        "written": written,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except zd.ConfigurationError as exc:
        eprint("Configuration Error: {}".format(exc))
        sys.exit(2)
    except zd.ApiError as exc:
        eprint(str(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        eprint("Interrupted.")
        sys.exit(130)
