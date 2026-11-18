#!/usr/bin/env python3
"""
Kibana / OpenSearch Dashboards CLI -- dashboards as compact specs.

One stdlib-only Python CLI that builds, checks, writes and inspects dashboards on
either backend, auto-detected from KIBANA_URL:

  * Kibana 9.4+          -> Dashboards / Visualizations APIs (Lens configs, ES|QL)
  * OpenSearch Dashboards -> saved-objects API (index-pattern, visualization with
                             visState, dashboard with panelsJSON)
  * Kibana < 9.4 (7.x/8.x) -> the same classic saved objects (no Dashboards API)

It is deliberately more than an HTTP facade. The expensive parts of building a
dashboard are done here once, deterministically, instead of by the model on
every run:

  discover  one round trip: field types, keyword cardinality + top values,
            numeric ranges, time span, docs in window, matching index
            patterns/data views, suggested controls and metrics
  build     compile a short backend-neutral *spec* into the native body,
            applying the design defaults (sizes, legends, axis titles, number
            formats, palettes, version gates) and packing the 48-column grid
  lint      design + schema rules on a spec, a Kibana body or an OSD bundle
  validate  run every panel's aggregation / ES|QL query against the data in one
            batched request and report the value each panel will show (or that
            it will be empty) -- before anything is saved
  apply     compile -> lint -> validate -> back up what is there -> write ->
            read back -> verify -> print the URL, in one command
  get       outline a dashboard (ids, chart types, grid, queries) instead of
            dumping its JSON; `-o` saves an editable, decoded copy
  copy      export a dashboard with its references and import it into another
            OSD tenant / Kibana space (e.g. promote from private to global)

Environment:
    KIBANA_URL          Kibana or OpenSearch Dashboards base URL. An AWS
                        OpenSearch domain endpoint works as-is (the dashboards
                        app at /_dashboards is found automatically).
    KIBANA_API_KEY      API key (encoded form, or "id:key"). ELASTIC_API_KEY
                        is accepted as an alias.
    KIBANA_USERNAME / KIBANA_PASSWORD
                        Basic auth, used when no API key is set.
    KIBANA_NGINX_USERNAME / KIBANA_NGINX_PASSWORD
                        Basic auth of a reverse proxy (nginx auth_basic) in front
                        of Kibana/OSD. The proxy then owns the Authorization
                        header, so the app login goes through a session login
                        with KIBANA_USERNAME / KIBANA_PASSWORD (no API key).
Optional:
    ELASTICSEARCH_URL / OPENSEARCH_URL
                        Data endpoint for queries. When unset, the KIBANA_URL
                        root is used if it is a cluster, otherwise the
                        Kibana/OSD console proxy.
    KIBANA_SPACE        Kibana space id (default: the default space).
    KIBANA_TENANT       OSD security tenant: private | global | <name>.
    KIBANA_READ_ONLY    ON by default. Every write is refused until this is
                        0/false/no/off.
    KIBANA_BACKUP_DIR   Where pre-write backups go
                        (default ~/.cache/kibana-dashboards/backups).
    KIBANA_INSECURE     1 to skip TLS verification (self-signed clusters).
    KIBANA_TIMEOUT      Per-request timeout in seconds (default 60).
"""

import argparse
import base64
import copy
import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

GRID_COLUMNS = 48
CACHE_DIR = os.path.expanduser("~/.cache/kibana-dashboards")
DETECT_TTL = 3600

# --- Design constants ---------------------------------------------------------

# Default (w, h) per chart, from the dashboard design guidelines.
SIZES = {
    "metric": (12, 5), "gauge": (12, 8),
    "line": (24, 10), "area": (24, 10), "area_stacked": (24, 10), "area_percentage": (24, 10),
    "bar": (24, 10), "bar_stacked": (24, 10), "bar_percentage": (24, 10),
    "bar_horizontal": (24, 10), "bar_horizontal_stacked": (24, 10),
    "bar_horizontal_percentage": (24, 10),
    "heatmap": (24, 10), "tag_cloud": (24, 10), "treemap": (24, 10), "waffle": (24, 10),
    "mosaic": (24, 10), "pie": (12, 10), "donut": (12, 10), "table": (48, 12),
    "markdown": (48, 4), "region_map": (24, 12),
}
XY_CHARTS = {"line", "area", "area_stacked", "area_percentage", "bar", "bar_stacked",
             "bar_percentage", "bar_horizontal", "bar_horizontal_stacked",
             "bar_horizontal_percentage"}
PARTITION_CHARTS = {"pie", "donut", "treemap", "waffle", "mosaic"}
ALL_CHARTS = set(SIZES)
STRETCHABLE = XY_CHARTS | {"table", "markdown", "heatmap", "tag_cloud", "treemap", "mosaic", "waffle"}
NO_TITLE_CHARTS = {"metric", "gauge", "tag_cloud", "waffle"}
# Metric widths that tile a row cleanly for a run of n consecutive metrics.
METRIC_RUN_WIDTH = {1: 12, 2: 12, 3: 16, 4: 12, 5: 8, 6: 8, 7: 6, 8: 6}

PALETTE_3 = {
    "status": ["#24c292", "#fcd883", "#f6726a"], "temperature": ["#61a2ff", "#ebeff5", "#f6726a"],
    "complementary": ["#61a2ff", "#f6f9fc", "#eaae01"], "red": ["#fdcdc9", "#fda198", "#f6726a"],
    "green": ["#bde8d8", "#7fd5b4", "#24c292"], "cool": ["#c4daff", "#95beff", "#61a2ff"],
    "warm": ["#ffcac3", "#fe9f96", "#f6726a"], "gray": ["#a2aec4", "#596883", "#1d2a3e"],
}
PALETTE_4 = {
    "status": ["#24c292", "#aee8d2", "#ffc9c2", "#f6726a"], "temperature": ["#61a2ff", "#cfe1ff", "#ffd4cf", "#f6726a"],
    "complementary": ["#61a2ff", "#accefe", "#f0d47f", "#eaae01"], "red": ["#fcd8d6", "#feb7b0", "#fc968d", "#f6726a"],
    "green": ["#cbece1", "#9fdfc6", "#6dd1ac", "#24c292"], "cool": ["#cfe1ff", "#adccff", "#89b7ff", "#61a2ff"],
    "warm": ["#ffd5cf", "#ffb5ac", "#fc948b", "#f6726a"], "gray": ["#b6c0d3", "#7b8aa4", "#485872", "#1d2a3e"],
}
PALETTE_5 = {
    "status": ["#24c292", "#aee8d2", "#fcd883", "#ffc9c2", "#f6726a"],
    "temperature": ["#61a2ff", "#cfe1ff", "#f6f9fc", "#ffd4cf", "#f6726a"],
    "complementary": ["#61a2ff", "#accefe", "#f6f9fc", "#f0d47f", "#eaae01"],
    "red": ["#fcdfdd", "#fec4bf", "#feaaa2", "#fb8f86", "#f6726a"],
    "green": ["#d4efe6", "#b1e4d1", "#8cd9bb", "#62cea6", "#24c292"],
    "cool": ["#d6e5ff", "#bad5ff", "#9fc4ff", "#82b3ff", "#61a2ff"],
    "warm": ["#ffdbd6", "#ffc2ba", "#ffa89f", "#fb8d84", "#f6726a"],
    "gray": ["#c2cbdb", "#92a0b8", "#667690", "#3f4e67", "#1d2a3e"],
}
PALETTE_ALIASES = {"negative": "red", "positive": "green"}
CATEGORICAL_PALETTES = {
    "default": ["#16C5C0", "#A6EDEA", "#61A2FF", "#BFDBFF", "#EE72A6"],
    "elastic_line_optimized": ["#16C5C0", "#61A2FF", "#EE72A6", "#EAAE01", "#F6726A"],
    "severity": ["#24C292", "#B5E5F2", "#FCD883", "#FF995E", "#EE4C48"],
}
LEGACY_PALETTES = {"eui_amsterdam", "kibana_v7_legacy", "elastic_brand_2023"}
CATALOG_HEX = {c.lower() for p in (PALETTE_3, PALETTE_4, PALETTE_5, CATEGORICAL_PALETTES)
               for cols in p.values() for c in cols}
DURATION_UNITS = {"ps", "ns", "us", "ms", "s", "min", "h", "d", "w", "mo", "y"}
LEGEND_STATS = {"min", "max", "avg", "median", "range", "last_value", "last_non_null_value",
                "first_value", "first_non_null_value", "difference", "difference_percentage",
                "count", "total", "standard_deviation", "variance", "distinct_count",
                "current_and_last_value"}

# Kibana settings that need a newer version than the 9.4 baseline.
GATES = {"esql_controls": (9, 5), "esql_trend": (9, 5), "gradient_areas": (9, 6)}

AGG_ALIASES = {
    "count": "count", "avg": "average", "average": "average", "mean": "average", "sum": "sum",
    "min": "min", "max": "max", "median": "median", "unique": "unique_count",
    "unique_count": "unique_count", "cardinality": "unique_count", "distinct": "unique_count",
    "last": "last_value", "last_value": "last_value",
}
AGG_LABEL = {"count": "Count", "average": "Average", "sum": "Total", "min": "Min", "max": "Max",
             "median": "Median", "unique_count": "Unique", "last_value": "Last",
             "percentile": "P"}
ID_LIKE = re.compile(r"(^|[._])(id|ids|uuid|guid|token|hash|key|trace|span|session|ip|email|phone|"
                     r"url|uri|path|message|msg|description|name)$|_id$|Id$", re.I)


# --- Errors / gate ------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class UsageError(Exception):
    pass


class ReadOnlyError(Exception):
    pass


_OFF = {"0", "false", "no", "off"}


def is_read_only():
    return os.environ.get("KIBANA_READ_ONLY", "1").strip().lower() not in _OFF


def require_writes(what):
    if is_read_only():
        raise ReadOnlyError(
            "Read-only mode is on (KIBANA_READ_ONLY defaults to 1): refusing to {}. "
            "Re-run with KIBANA_READ_ONLY=0 to allow writes.".format(what))


def note(msg):
    print(msg, file=sys.stderr)


# --- Small helpers ------------------------------------------------------------

def slug(text, maxlen=60):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "dashboard"


def humanize(field):
    parts = (field or "").split(".")
    if len(parts) > 1 and parts[-1] == "keyword":
        parts = parts[:-1]
    base = parts[-1]
    if len(base) <= 4 and len(parts) > 1:
        base = parts[-2] + " " + base
    words = re.sub(r"[_\-]+", " ", re.sub(r"([a-z])([A-Z])", r"\1 \2", base)).strip()
    return words[:1].upper() + words[1:] if words else field


def version_tuple(v):
    parts = re.findall(r"\d+", str(v or ""))[:3]
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))


def fmt_num(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        a = abs(v)
        if a >= 1e9:
            return "{:.2f}B".format(v / 1e9)
        if a >= 1e6:
            return "{:.2f}M".format(v / 1e6)
        if a >= 1e4:
            return "{:.1f}k".format(v / 1e3)
        if isinstance(v, float) and not v.is_integer():
            return "{:.4g}".format(v)
        return str(int(v))
    return str(v)


def clip(s, n=90):
    s = re.sub(r"\s+", " ", str(s))
    return s if len(s) <= n else s[: n - 1] + "…"


def load_json(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(obj, path=None):
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)


_REL = re.compile(r"^now(?:([+-])(\d+)([smhdwMy]))?(?:/[smhdwMy])?$")


def date_math_to_iso(expr, now=None):
    """Resolve 'now-7d'-style date math (or an ISO date) to an ISO-8601 string."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    m = _REL.match(str(expr).strip())
    if not m:
        return str(expr)
    if not m.group(1):
        return now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sign = 1 if m.group(1) == "+" else -1
    n, unit = int(m.group(2)), m.group(3)
    secs = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}[unit]
    t = now + sign * _dt.timedelta(seconds=n * secs)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def since_to_range(since):
    """'7d' / 'now-7d' -> {'from': 'now-7d', 'to': 'now'}"""
    s = str(since).strip()
    if not s.startswith("now"):
        s = "now-" + s
    return {"from": s, "to": "now"}


# --- HTTP ---------------------------------------------------------------------

def _ssl_context():
    if os.environ.get("KIBANA_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"}:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


class _Headers(dict):
    """Response headers as a dict; `_all` keeps repeated ones (Set-Cookie)."""

    def __init__(self, msg):
        super().__init__(dict(msg or {}))
        self._all = list(msg.items()) if msg is not None else []


def http(method, url, headers=None, body=None, timeout=None):
    """Return (status, text, response_headers). Never raises on HTTP status."""
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    timeout = timeout or float(os.environ.get("KIBANA_TIMEOUT", "60"))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace"), _Headers(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), _Headers(e.headers)
    except urllib.error.URLError as e:
        raise ApiError("Cannot reach {}: {}. Check KIBANA_URL, VPN/network access and TLS "
                       "(KIBANA_INSECURE=1 for self-signed certs).".format(
                           urllib.parse.urlsplit(url).netloc, e.reason))
    except TimeoutError:
        raise ApiError("Request to {} timed out after {}s (KIBANA_TIMEOUT).".format(
            urllib.parse.urlsplit(url).path, timeout))


def hdrs_kbn(hdrs):
    return any(k.lower() in ("kbn-name", "osd-name") for k in (hdrs or {}))


def _err_text(text):
    try:
        j = json.loads(text)
    except ValueError:
        return clip(text, 400)
    if isinstance(j, dict):
        e = j.get("error")
        if isinstance(e, dict):
            rc = e.get("root_cause") or []
            reason = e.get("reason") or (rc[0].get("reason") if rc else None)
            return "{}: {}".format(e.get("type", "error"), reason or json.dumps(e)[:300])
        msg = j.get("message") or e
        if isinstance(msg, str):
            return clip(msg, 600)
    return clip(text, 400)


class Client:
    """Talks to the dashboards app (Kibana / OSD) and to the data cluster."""

    def __init__(self, space=None, tenant=None):
        self.url = (os.environ.get("KIBANA_URL") or "").rstrip("/")
        if not self.url:
            raise UsageError("KIBANA_URL is not set. Export the Kibana / OpenSearch Dashboards "
                             "URL (an AWS OpenSearch domain endpoint works as-is).")
        if not re.match(r"^https?://", self.url):
            self.url = "https://" + self.url
        self.space = space if space is not None else os.environ.get("KIBANA_SPACE") or None
        t = tenant if tenant is not None else os.environ.get("KIBANA_TENANT") or None
        self.tenant = {"private": "__user__", "global": "global"}.get(t, t) if t else None
        # A reverse proxy with its own basic auth (nginx auth_basic) owns the Authorization header,
        # so the app credentials go through a session login (cookie) instead.
        pu, pp = os.environ.get("KIBANA_NGINX_USERNAME"), os.environ.get("KIBANA_NGINX_PASSWORD")
        self.proxy_auth = ("Basic " + base64.b64encode("{}:{}".format(pu, pp).encode()).decode()) if pu and pp else None
        self.cookie = None
        if self.proxy_auth:
            if not (os.environ.get("KIBANA_USERNAME") and os.environ.get("KIBANA_PASSWORD")):
                raise UsageError("KIBANA_NGINX_USERNAME/PASSWORD is set, so the app login must be "
                                 "KIBANA_USERNAME and KIBANA_PASSWORD (an API key cannot share the "
                                 "Authorization header with the proxy).")
            self.auth = self.proxy_auth
        else:
            self.auth = self._auth_header()
        self.info = None

    @staticmethod
    def _auth_header():
        key = os.environ.get("KIBANA_API_KEY") or os.environ.get("ELASTIC_API_KEY")
        if key:
            key = key.strip()
            if ":" in key:
                key = base64.b64encode(key.encode()).decode()
            return "ApiKey " + key
        user, pw = os.environ.get("KIBANA_USERNAME"), os.environ.get("KIBANA_PASSWORD")
        if user and pw:
            return "Basic " + base64.b64encode("{}:{}".format(user, pw).encode()).decode()
        raise UsageError("No credentials: set KIBANA_API_KEY (or ELASTIC_API_KEY), or "
                         "KIBANA_USERNAME and KIBANA_PASSWORD.")

    def _base_headers(self):
        h = {"Authorization": self.auth}
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    def _login(self, base):
        """Session login behind a proxy: Kibana's /internal/security/login, else OSD's /auth/login."""
        user, pw = os.environ["KIBANA_USERNAME"], os.environ["KIBANA_PASSWORD"]
        attempts = [
            ("/internal/security/login", {"providerType": "basic", "providerName": "basic",
                                          "currentURL": base + "/login",
                                          "params": {"username": user, "password": pw}}),
            ("/internal/security/login", {"username": user, "password": pw}),
            ("/auth/login", {"username": user, "password": pw}),
        ]
        best = None  # the most telling failure: an app-level answer beats a 404 from a wrong endpoint
        for path, body in attempts:
            st, text, hdrs = http("POST", base + path, {
                "Authorization": self.proxy_auth, "kbn-xsrf": "true", "osd-xsrf": "true",
                "Content-Type": "application/json"}, json.dumps(body))
            if st == 401 and not hdrs_kbn(hdrs):
                raise ApiError("The proxy (nginx) rejected KIBANA_NGINX_USERNAME/PASSWORD (401).", 401)
            if st in (200, 204):
                cookies = [v.split(";", 1)[0] for k, v in hdrs._all if k.lower() == "set-cookie"]
                if cookies:
                    self.cookie = "; ".join(cookies)
                    return
            if best is None or (best[1] == 404 and st != 404):
                best = (path, st, text)
            if st in (401, 403) and hdrs_kbn(hdrs):
                break  # the app answered: the credentials are wrong, not the endpoint
        path, st, text = best
        raise ApiError("Login as KIBANA_USERNAME failed at {} ({}): {}{}".format(
            path, st, _err_text(text), " -- check KIBANA_USERNAME/PASSWORD" if st in (400, 401, 403) else ""), st)

    def _ensure_session(self):
        if self.proxy_auth and not self.cookie:
            self._login(self.detect()["base"])

    # -- detection --

    def detect(self, refresh=False):
        if self.info:
            return self.info
        key = hashlib.sha1((self.url + self.auth).encode()).hexdigest()[:16]
        path = os.path.join(CACHE_DIR, "detect-{}.json".format(key))
        if not refresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < DETECT_TTL:
            try:
                self.info = load_json(path)
                return self.info
            except ValueError:
                pass
        info = self._probe()
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f)
        self.info = info
        return info

    def _probe(self):
        base_headers = {"Authorization": self.auth}
        if self.proxy_auth:
            if not self.cookie:
                self._login(self.url)
            base_headers = self._base_headers()
        cluster = None
        st, text, _ = http("GET", self.url + "/", base_headers)
        if st == 200:
            try:
                j = json.loads(text)
                if isinstance(j, dict) and "cluster_name" in j:
                    cluster = j
            except ValueError:
                pass
        candidates = [self.url]
        if cluster is not None:
            candidates = [self.url + "/_dashboards", self.url + "/_plugin/kibana"]
        last = None
        for base in candidates:
            st, text, hdrs = http("GET", base + "/api/status", base_headers)
            last = (st, text)
            if st in (401, 403):
                if self.proxy_auth and not hdrs_kbn(hdrs):
                    raise ApiError("The proxy rejected KIBANA_NGINX_USERNAME/PASSWORD ({}).".format(st), st)
                raise ApiError("Authentication failed at {} ({}): {}".format(
                    base + "/api/status", st, _err_text(text)), st)
            if st != 200:
                continue
            try:
                j = json.loads(text)
            except ValueError:
                continue
            ver = (j.get("version") or {}).get("number")
            lower_headers = {k.lower(): v for k, v in hdrs.items()}
            is_osd = ("osd-name" in lower_headers or "opensearch" in text[:4000].lower())
            # Kibana below 9.4 has no Dashboards API; it stores dashboards as classic saved objects
            # (visState / panelsJSON), the same format OSD forked from Kibana 7.10, so it takes the
            # saved-object path. info["product"] keeps what it really is (URLs, spaces, query languages).
            classic = is_osd or version_tuple(ver) < (9, 4, 0)
            info = {"backend": "osd" if classic else "kibana", "version": ver, "base": base,
                    "product": "opensearch" if is_osd else "kibana",
                    "flavor": (j.get("version") or {}).get("build_flavor")}
            if os.environ.get("ELASTICSEARCH_URL") or os.environ.get("OPENSEARCH_URL"):
                info["data"] = (os.environ.get("ELASTICSEARCH_URL") or os.environ["OPENSEARCH_URL"]).rstrip("/")
                info["data_mode"] = "direct"
            elif cluster is not None:
                info["data"], info["data_mode"] = self.url, "direct"
            else:
                info["data"], info["data_mode"] = base, "proxy"
            if cluster:
                info["cluster_version"] = (cluster.get("version") or {}).get("number")
                info["cluster_distribution"] = (cluster.get("version") or {}).get("distribution") or (
                    "opensearch" if "opensearch" in json.dumps(cluster).lower() else "elasticsearch")
            return info
        raise ApiError("Could not find a Kibana / OpenSearch Dashboards status endpoint under {} "
                       "(last response {}: {}).".format(self.url, last[0] if last else "?",
                                                         _err_text(last[1]) if last else ""))

    @property
    def backend(self):
        return self.detect()["backend"]

    def scope(self):
        """'space X' on Kibana, 'tenant X' on OSD -- for messages."""
        if self.product == "kibana":
            return "space " + (self.space or "default")
        return "tenant " + ({"__user__": "private"}.get(self.tenant, self.tenant) or "default")

    @property
    def product(self):
        info = self.detect()
        return info.get("product") or ("opensearch" if info["backend"] == "osd" else "kibana")

    @property
    def version(self):
        return version_tuple(self.detect().get("version"))

    # -- dashboards app --

    def _app_headers(self, extra=None):
        self._ensure_session()
        h = {"Authorization": self.auth, "kbn-xsrf": "true", "osd-xsrf": "true",
             "Content-Type": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        if self.backend == "kibana":
            h["Elastic-Api-Version"] = "2023-10-31"
        if self.product == "opensearch" and self.tenant:
            h["securitytenant"] = self.tenant
        h.update(extra or {})
        return h

    def app(self, method, path, body=None, params=None, ok=(200, 201), raw=False, headers=None):
        info = self.detect()
        prefix = "/s/" + self.space if (self.space and self.product == "kibana" and self.space != "default") else ""
        url = info["base"] + prefix + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body, ensure_ascii=False)
        st, text, _ = http(method, url, self._app_headers(headers), body)
        if st not in ok:
            hint = ""
            if st == 404 and "/api/dashboards" in path and info["backend"] == "kibana":
                hint = " (Kibana < 9.4 has no Dashboards API, or the id/space is wrong)"
            if st == 401 and self.proxy_auth and self.cookie:
                self.cookie = None  # session expired: log in again once
                self._ensure_session()
                st, text, _ = http(method, url, self._app_headers(headers), body)
                if st in ok:
                    return text if raw else (json.loads(text) if text.strip() else {})
            if st == 403 and self.product == "opensearch":
                hint = " (check the user's role in tenant {!r})".format(self.tenant or "default")
            raise ApiError("{} {} -> {}: {}{}".format(method, prefix + path, st, _err_text(text), hint), st)
        if raw:
            return text
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return {"_raw": text}

    # -- data cluster --

    def data(self, method, path, body=None, ndjson=False, ok=(200,)):
        info = self.detect()
        ctype = "application/x-ndjson" if ndjson else "application/json"
        if body is not None and not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False)
        if info["data_mode"] == "direct":
            url = info["data"] + path
            headers = {"Authorization": self.auth, "Content-Type": ctype}
            if self.proxy_auth and info["data"].startswith(info["base"]):
                self._ensure_session()
                headers["Cookie"] = self.cookie
        else:
            url = info["data"] + "/api/console/proxy?" + urllib.parse.urlencode(
                {"path": path, "method": method})
            headers = self._app_headers({"Content-Type": ctype})
            method = "POST"
        st, text, _ = http(method, url, headers, body)
        if st not in ok:
            raise ApiError("{} {} -> {}: {}".format(method, path, st, _err_text(text)), st)
        return json.loads(text) if text.strip() else {}

    def view_url(self, kind, oid):
        info = self.detect()
        if self.product == "kibana":
            prefix = "/s/" + self.space if self.space and self.space != "default" else ""
            if info["backend"] == "osd":  # classic saved objects on Kibana < 9.4
                app = "dashboards#/view/" if kind == "dashboard" else "visualize#/edit/"
                return info["base"] + prefix + "/app/" + app + urllib.parse.quote(oid, safe="")
            app = "dashboards#/view/" if kind == "dashboard" else "lens#/edit/"
            return info["base"] + prefix + "/app/" + app + oid
        tenant = {"__user__": "private", "": "global"}.get(self.tenant, self.tenant)
        # The tenant parameter must precede the '#': OSD ignores it inside the hash route.
        app, route = ("dashboards", "#/view/") if kind == "dashboard" else ("visualize", "#/edit/")
        q = "?security_tenant=" + urllib.parse.quote(tenant) if tenant else ""
        return info["base"] + "/app/" + app + q + route + urllib.parse.quote(oid, safe="")


# --- Field catalog ------------------------------------------------------------

class Fields:
    """field_caps for an index pattern: name -> {type, aggregatable, searchable}."""

    def __init__(self, caps=None):
        self.caps = caps or {}

    @classmethod
    def load(cls, client, pattern):
        path = "/{}/_field_caps?fields=*&ignore_unavailable=true&allow_no_indices=true".format(
            urllib.parse.quote(pattern, safe="*,-"))
        j = client.data("GET", path)
        caps = {}
        for name, types in (j.get("fields") or {}).items():
            if name.startswith("_"):
                continue
            ts = [t for t in types if not t.startswith("_")]
            if not ts:
                continue
            t = ts[0] if len(ts) == 1 else ("keyword" if "keyword" in ts else ts[0])
            v = types[ts[0]]
            caps[name] = {"type": t, "aggregatable": any(types[x].get("aggregatable") for x in ts),
                          "conflict": len(ts) > 1, "types": ts, "searchable": v.get("searchable", True)}
        f = cls(caps)
        f.indices = j.get("indices") or []
        return f

    def __bool__(self):
        return bool(self.caps)

    def type_of(self, name):
        return (self.caps.get(name) or {}).get("type")

    def is_numeric(self, name):
        return self.type_of(name) in {"long", "integer", "short", "byte", "double", "float",
                                      "half_float", "scaled_float", "unsigned_long"}

    def resolve(self, name, need_agg=True, problems=None, where=""):
        """Map a user field name to an aggregatable one (text -> .keyword)."""
        if not self.caps or not name:
            return name
        c = self.caps.get(name)
        if c and (c["aggregatable"] or not need_agg):
            return name
        kw = name + ".keyword"
        if need_agg and kw in self.caps and self.caps[kw]["aggregatable"]:
            return kw
        if problems is not None:
            if c:
                problems.append(("error", where, "field {!r} ({}) is not aggregatable".format(name, c["type"])))
            else:
                close = difflib.get_close_matches(name, list(self.caps), n=3, cutoff=0.6)
                problems.append(("error", where, "unknown field {!r}{}".format(
                    name, " -- did you mean " + ", ".join(close) + "?" if close else "")))
        return name

    def time_fields(self):
        return [n for n, c in self.caps.items() if c["type"] in ("date", "date_nanos")]


def guess_time_field(fields, preferred=None):
    if preferred:
        return preferred
    tf = fields.time_fields() if fields else []
    for cand in ("@timestamp", "timestamp", "created_at", "createdAt", "date", "time", "event.created"):
        if cand in tf:
            return cand
    return tf[0] if tf else "@timestamp"


def guess_format(op, field):
    """Natural-unit format from a field name; None when ambiguous."""
    if op in ("count", "unique_count") or not field:
        return None
    f = field.lower()
    if "bytes" in f or f.endswith("_size") or "memory" in f:
        return {"type": "bytes", "decimals": 1}
    if re.search(r"(^|[._])bits?($|[._])", f):
        return {"type": "bits", "decimals": 1}
    m = re.search(r"[._](ms|us|ns|sec|secs|seconds|millis|micros|nanos)$", f) or \
        re.search(r"(?:latency|duration|took|elapsed)(ms|us|ns)$", f)
    if m:
        unit = {"millis": "ms", "micros": "us", "nanos": "ns", "sec": "s", "secs": "s",
                "seconds": "s"}.get(m.group(1), m.group(1))
        return {"type": "duration", "from": unit, "to": "auto"}
    if re.search(r"(^|[._])(latency|duration|took|elapsed)$", f):
        return None  # a duration, but the stored unit is unknown -- ask or check the data
    if re.search(r"(pct|percent|percentage)$", f):
        return {"type": "number", "decimals": 1, "suffix": "%"}
    return None


FORMAT_SHORTCUTS = {
    "bytes": {"type": "bytes", "decimals": 1}, "bits": {"type": "bits", "decimals": 1},
    "percent": {"type": "percent", "decimals": 1, "compact": True},
    "percent100": {"type": "number", "decimals": 1, "suffix": "%"},
    "number": {"type": "number", "decimals": 0}, "compact": {"type": "number", "decimals": 1, "compact": True},
    "decimal": {"type": "number", "decimals": 2},
}


def parse_format(fmt):
    if fmt is None or isinstance(fmt, dict):
        return fmt
    s = str(fmt).strip()
    if s in ("none", "raw", ""):
        return False
    if s.startswith("duration"):
        unit = s.split(":", 1)[1] if ":" in s else "ms"
        return {"type": "duration", "from": unit, "to": "auto"}
    if s.startswith("currency") or s.startswith("suffix:"):
        return {"type": "number", "decimals": 2, "suffix": " " + s.split(":", 1)[1] if ":" in s else ""}
    if s in FORMAT_SHORTCUTS:
        return dict(FORMAT_SHORTCUTS[s])
    raise UsageError("unknown format {!r} (use bytes, bits, percent, percent100, number, compact, "
                     "decimal, duration:<unit>, suffix:<text>, or a format object)".format(fmt))


# --- Spec model ---------------------------------------------------------------

def is_spec(doc):
    if not isinstance(doc, dict):
        return False
    if "chart" in doc:
        return True
    items = list(doc.get("panels") or [])
    for s in doc.get("sections") or []:
        items += s.get("panels") or []
    return any(isinstance(p, dict) and "chart" in p for p in items) or (
        "sections" in doc and not doc.get("panels"))


def panel_ref(refs, panel):
    """A panel's reference. OSD/Kibana 7.10 names it `panelRefName`; Kibana 7.13+ prefixes the
    panel index (`<panelIndex>:panel_<panelIndex>`)."""
    name = panel.get("panelRefName")
    return refs.get(name) or refs.get("{}:{}".format(panel.get("panelIndex"), name))


def is_osd_bundle(doc):
    return isinstance(doc, dict) and "dashboard" in doc and "visualizations" in doc


def parse_metric(m, problems, where):
    """'count' | 'avg:field' | 'p95:field' | {op, field, label, format} -> dict"""
    if isinstance(m, dict):
        d = dict(m)
        if "column" in d:
            return d
        op = AGG_ALIASES.get(str(d.get("op", "count")).lower(), d.get("op"))
        d["op"] = op
        if re.match(r"^p\d", str(d.get("op"))):
            d["percentile"], d["op"] = float(str(d["op"])[1:]), "percentile"
        return d
    s = str(m).strip()
    op, _, field = s.partition(":")
    op_l = op.lower()
    pm = re.match(r"^p(\d+(?:\.\d+)?)$", op_l)
    if pm:
        pv = float(pm.group(1))
        return {"op": "percentile", "field": field, "percentile": int(pv) if pv.is_integer() else pv}
    if op_l in AGG_ALIASES:
        d = {"op": AGG_ALIASES[op_l]}
        if field:
            d["field"] = field
        elif d["op"] != "count":
            problems.append(("error", where, "metric {!r} needs a field ({}:<field>)".format(s, op_l)))
        return d
    problems.append(("error", where, "unknown metric {!r} (count, avg:f, sum:f, min:f, max:f, median:f, "
                                     "unique:f, p95:f, last:f)".format(s)))
    return {"op": "count"}


def metric_label(m):
    if m.get("label"):
        return m["label"]
    if m.get("column"):
        return None
    if m["op"] == "count":
        return "Count"
    if m["op"] == "percentile":
        p = m.get("percentile", 95)
        return "P{} {}".format(int(p) if float(p).is_integer() else p, humanize(m["field"]).lower())
    return "{} {}".format(AGG_LABEL[m["op"]], humanize(m.get("field", "")).lower())


def metric_hint(raw):
    m = raw.get("metric") or raw.get("metrics")
    m = m[0] if isinstance(m, list) and m else m
    if isinstance(m, str):
        return raw.get("chart", "") + "-" + m.replace(":", "-")
    return None


def as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


class Panel:
    """Normalized spec panel."""

    def __init__(self, raw, spec, idx, problems):
        self.raw = raw
        self.id = raw.get("id") or slug(raw.get("title") or raw.get("label") or metric_hint(raw) or
                                        raw.get("chart", "p"), 40)
        self.chart = raw.get("chart")
        if self.chart not in ALL_CHARTS:
            problems.append(("error", self.where, "unknown chart {!r} (one of: {})".format(
                self.chart, ", ".join(sorted(ALL_CHARTS)))))
            self.chart = "bar"
        self.title = raw.get("title")
        self.index = raw.get("index") or spec.get("index")
        self.time_field = raw.get("time_field") or spec.get("time_field")
        self.esql = raw.get("esql")
        self.filter = raw.get("filter")
        self.size = int(raw.get("size", 10))
        self.format = parse_format(raw.get("format"))
        ms = raw.get("metrics") if "metrics" in raw else raw.get("metric", raw.get("y", "count"))
        if self.esql:
            given = any(k in raw for k in ("metric", "metrics", "y"))
            self.metrics = [m if isinstance(m, dict) else {"column": m} for m in as_list(ms)] if given else []
        else:
            self.metrics = [parse_metric(m, problems, self.where) for m in as_list(ms)]
        if raw.get("label") and len(self.metrics) == 1:
            self.metrics[0]["label"] = raw["label"]
        self.x = raw.get("x")
        self.split = raw.get("split") or raw.get("breakdown")
        self.by = as_list(raw.get("by") or raw.get("rows") or raw.get("group_by"))
        self.y_field = raw.get("y_by")  # heatmap y bucket
        self.legend_stats = as_list(raw.get("legend_stats"))
        self.trend = bool(raw.get("trend"))
        self.bands = raw.get("bands")
        self.text = raw.get("text") or raw.get("content")
        self.w = raw.get("w")
        self.h = raw.get("h")
        self.grid = raw.get("grid")
        self.wide = bool(raw.get("wide") or raw.get("primary"))
        self.kibana_extra = raw.get("kibana") or {}
        self.osd_extra = raw.get("osd") or {}
        if self.esql and not self.metrics:
            self.metrics = [{"column": c} for c in esql_stats_aliases(self.esql)[:1]]
        if self.chart == "markdown" and not self.text:
            problems.append(("error", self.where, "markdown panel needs 'text'"))
        if self.chart in XY_CHARTS and not self.x and not self.esql:
            self.x = "time"
        if self.chart in PARTITION_CHARTS | {"tag_cloud"} and not self.by and not self.esql:
            problems.append(("error", self.where, "{} needs 'by': <field>".format(self.chart)))
        if self.chart == "heatmap" and not (self.y_field or self.split):
            problems.append(("error", self.where, "heatmap needs 'x' and 'y_by'"))
        for s in self.legend_stats:
            if s not in LEGEND_STATS:
                problems.append(("error", self.where, "unknown legend statistic {!r}".format(s)))

    @property
    def where(self):
        return self.id

    @property
    def is_time_x(self):
        return self.x in ("time", "@time", None) and self.chart in XY_CHARTS | {"heatmap"} and not self.esql

    def series_count(self):
        n = max(1, len(self.metrics))
        if self.split:
            n *= self.size if not isinstance(self.split, dict) else int(self.split.get("size", self.size))
        return n


def esql_stats_aliases(query):
    """Best-effort: column aliases defined by the last STATS clause of an ES|QL query."""
    segs = [s.strip() for s in re.split(r"\|(?=(?:[^\"`]*[\"`][^\"`]*[\"`])*[^\"`]*$)", query)]
    out = []
    for seg in segs:
        m = re.match(r"(?is)^stats\s+(.*)$", seg)
        if not m:
            m2 = re.match(r"(?is)^eval\s+(.*)$", seg)
            if m2:
                for part in _split_top(m2.group(1)):
                    a = re.match(r"\s*(`[^`]+`|[\w.@]+)\s*=", part)
                    if a:
                        out.append(a.group(1).strip("`"))
            continue
        body = re.split(r"(?i)\s+by\s+", m.group(1), maxsplit=1)
        out = []
        for part in _split_top(body[0]):
            a = re.match(r"\s*(`[^`]+`|[\w.@]+)\s*=", part)
            out.append(a.group(1).strip("`") if a else part.strip())
        if len(body) > 1:
            for part in _split_top(body[1]):
                a = re.match(r"\s*(`[^`]+`|[\w.@]+)\s*=", part)
                out.append(a.group(1).strip("`") if a else part.strip())
    return out


def esql_by_columns(query):
    segs = [s.strip() for s in query.split("|")]
    for seg in reversed(segs):
        m = re.match(r"(?is)^stats\s+(.*)$", seg)
        if m:
            body = re.split(r"(?i)\s+by\s+", m.group(1), maxsplit=1)
            if len(body) < 2:
                return []
            cols = []
            for part in _split_top(body[1]):
                a = re.match(r"\s*(`[^`]+`|[\w.@]+)\s*=", part)
                cols.append(a.group(1).strip("`") if a else part.strip())
            return cols
    return []


def _split_top(s):
    out, depth, cur, q = [], 0, "", None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in "\"`'":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    if cur.strip():
        out.append(cur)
    return out


class Spec:
    def __init__(self, doc):
        self.doc = doc
        self.problems = []
        self.standalone = "chart" in doc
        self.title = doc.get("title")
        self.description = doc.get("description")
        self.id = doc.get("id")
        self.index = doc.get("index")
        self.time_field = doc.get("time_field")
        tr = doc.get("time_range")
        if isinstance(tr, str):
            tr = since_to_range(tr)
        self.time_range = tr
        self.controls = [c if isinstance(c, dict) else {"field": c} for c in as_list(doc.get("controls"))]
        self.sections = []
        idx = 0
        if self.standalone:
            self.sections.append({"title": None, "panels": [Panel(doc, doc, 0, self.problems)]})
            return
        if doc.get("panels"):
            ps = []
            for p in doc["panels"]:
                ps.append(Panel(p, doc, idx, self.problems))
                idx += 1
            self.sections.append({"title": None, "panels": ps})
        for s in doc.get("sections") or []:
            ps = []
            for p in s.get("panels") or []:
                ps.append(Panel(p, doc, idx, self.problems))
                idx += 1
            self.sections.append({"title": s.get("title"), "collapsed": bool(s.get("collapsed")), "panels": ps})
        if not self.title:
            self.problems.append(("error", "dashboard", "missing 'title'"))
        if not self.description:
            self.problems.append(("warn", "dashboard", "no 'description' -- set one on every new dashboard"))
        seen = {}
        for p in self.panels():
            if p.raw.get("id"):
                continue
            n = seen.get(p.id, 0)
            seen[p.id] = n + 1
            if n:
                p.id = "{}-{}".format(p.id, n + 1)
        ids = [p.id for p in self.panels()]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            self.problems.append(("error", "dashboard", "duplicate panel ids: " + ", ".join(sorted(dup))))
        for p in self.panels():
            if p.chart != "markdown" and not p.index and not p.esql:
                self.problems.append(("error", p.where, "no index: set top-level 'index' or the panel's"))

    def panels(self):
        return [p for s in self.sections for p in s["panels"]]

    def indices(self):
        out = []
        for p in self.panels():
            if p.index and not p.esql and p.index not in out:
                out.append(p.index)
        for c in self.controls:
            ix = c.get("index") or self.index
            if ix and ix not in out:
                out.append(ix)
        return out

    def all_esql(self):
        ps = [p for p in self.panels() if p.chart != "markdown"]
        return bool(ps) and all(p.esql for p in ps)


# --- Layout -------------------------------------------------------------------

def default_size(p, run_len=1):
    w, h = SIZES.get(p.chart, (24, 10))
    if p.chart == "metric":
        w = METRIC_RUN_WIDTH.get(run_len, 6)
    if p.chart == "markdown" and p.text:
        lines = p.text.count("\n") + 1 + len(p.text) // 160
        h = max(3, min(9, lines + 2))
    if p.wide and p.chart in STRETCHABLE:
        w = 48
    return int(p.w or w), int(p.h or h)


def pack(items):
    """items: list of dicts with w,h (and fixed flags) -> sets x,y. Rows fill left->right;
    a row's y advances by its tallest panel; heights in a row are equalized; the last
    stretchable panel absorbs leftover columns."""
    rows, cur, x = [], [], 0
    for it in items:
        it["w"] = max(1, min(GRID_COLUMNS, it["w"]))
        if cur and (x + it["w"] > GRID_COLUMNS or it.get("break_before")):
            rows.append(cur)
            cur, x = [], 0
        it["x"] = x
        x += it["w"]
        cur.append(it)
    if cur:
        rows.append(cur)
    y = 0
    for row in rows:
        used = sum(i["w"] for i in row)
        if used < GRID_COLUMNS:
            stretch = [i for i in row if i.get("stretchable") and not i.get("fixed_w")]
            if stretch:
                extra = GRID_COLUMNS - used
                per = extra // len(stretch)
                for k, i in enumerate(stretch):
                    i["w"] += per + (extra - per * len(stretch) if k == len(stretch) - 1 else 0)
                xx = 0
                for i in row:
                    i["x"] = xx
                    xx += i["w"]
        hmax = max(i["h"] for i in row)
        for i in row:
            if not i.get("fixed_h"):
                i["h"] = hmax
            i["y"] = y
        y += hmax
    return y


def layout_section(panels):
    """Assign grid to spec panels, honoring explicit grids."""
    if all(p.grid for p in panels):
        return [dict(p.grid) for p in panels]
    items = []
    i = 0
    while i < len(panels):
        p = panels[i]
        if p.chart == "metric":
            j = i
            while j < len(panels) and panels[j].chart == "metric":
                j += 1
            run = j - i
            for k in range(i, j):
                w, h = default_size(panels[k], min(run, 8))
                items.append({"w": w, "h": h, "fixed_w": bool(panels[k].w), "fixed_h": bool(panels[k].h),
                              "stretchable": False})
            i = j
            continue
        w, h = default_size(p)
        items.append({"w": w, "h": h, "fixed_w": bool(p.w), "fixed_h": bool(p.h),
                      "stretchable": p.chart in STRETCHABLE, "break_before": bool(p.raw.get("new_row"))})
        i += 1
    pack(items)
    return [{"x": it["x"], "y": it["y"], "w": it["w"], "h": it["h"]} for it in items]


def reflow_grids(grids_with_types):
    """Reflow arbitrary panels [(grid, chart_type)] keeping order (by y, x) and sizes."""
    order = sorted(range(len(grids_with_types)), key=lambda k: (grids_with_types[k][0].get("y", 0),
                                                                 grids_with_types[k][0].get("x", 0)))
    items = []
    for k in order:
        g, t = grids_with_types[k]
        items.append({"k": k, "w": int(g.get("w", 24)), "h": int(g.get("h", 10)), "fixed_h": True,
                      "stretchable": False})
    pack(items)
    out = [None] * len(grids_with_types)
    for it in items:
        out[it["k"]] = {"x": it["x"], "y": it["y"], "w": it["w"], "h": it["h"]}
    return out


# --- Kibana 9.4+ compiler -----------------------------------------------------

class Ctx:
    """Live context for compiling: backend version, field catalogs, object ids."""

    def __init__(self, backend="kibana", version=(9, 4, 0), fields=None, index_ids=None):
        self.backend = backend
        self.version = version
        self.version_str = ".".join(str(v) for v in version)
        self.patterns = {}
        self.fields = fields or {}          # pattern -> Fields
        self.index_ids = index_ids or {}    # pattern -> data view / index-pattern id
        self.notes = []

    def gate(self, key):
        return self.backend == "kibana" and self.version >= GATES[key] + (0,)

    def f(self, pattern):
        return self.fields.get(pattern) or Fields()


def _k_format(p, m):
    if p.format is False:
        return None
    if p.format:
        return p.format
    if m.get("format") is not None:
        return parse_format(m["format"]) or None
    return guess_format(m.get("op"), m.get("field"))


def _k_metric(p, m, ctx, problems, primary=None):
    if "column" in m:
        out = {"column": m["column"]}
        if m.get("label"):
            out["label"] = m["label"]
    else:
        out = {"operation": m["op"]}
        if m.get("field"):
            out["field"] = ctx.f(p.index).resolve(m["field"], need_agg=True, problems=problems, where=p.where)
        if m["op"] == "percentile":
            out["percentile"] = m.get("percentile", 95)
        if m["op"] == "last_value":
            out["time_field"] = p.time_field
        out["label"] = metric_label(m)
    fmt = _k_format(p, m)
    if fmt:
        out["format"] = fmt
    if primary:
        out = dict({"type": primary}, **out)
    return out


def _k_bucket(p, field, ctx, problems, size=None):
    if p.esql:
        return {"column": field}
    if field in ("time", None):
        return {"operation": "date_histogram", "field": p.time_field}
    f = ctx.f(p.index).resolve(field, need_agg=True, problems=problems, where=p.where)
    return {"operation": "terms", "fields": [f], "limit": int(size or p.size)}


def _k_data_source(p):
    if p.esql:
        return {"type": "esql", "query": p.esql}
    return {"type": "data_view_spec", "index_pattern": p.index, "time_field": p.time_field}


def _bands_steps(bands, n, palette="status", reverse=False, top=None, bottom=0):
    pal = PALETTE_ALIASES.get(palette, palette)
    cols = {3: PALETTE_3, 4: PALETTE_4, 5: PALETTE_5}[n].get(pal)
    if not cols:
        raise UsageError("unknown palette {!r} (status, temperature, complementary, negative, positive, "
                         "cool, warm, gray)".format(palette))
    cols = list(reversed(cols)) if reverse else list(cols)
    th = list(bands)
    if len(th) != n - 1:
        raise UsageError("{} bands need {} thresholds, got {}".format(n, n - 1, th))
    edges = [bottom] + th + [top]
    steps = []
    for i in range(n):
        s = {"gte": edges[i], "color": cols[i]}
        if i < n - 1:
            s["lt"] = edges[i + 1]
        elif top is not None:
            s["lte"] = top
        steps.append(s)
    return steps


def compile_kibana_panel(p, ctx, problems):
    """Return a Lens config (the `config` of a vis panel / a standalone viz body)."""
    if p.chart == "markdown":
        return None
    if p.filter and not p.esql:
        problems.append(("error", p.where, "'filter' on a data-view panel is not supported by the Kibana "
                                           "compiler -- use 'esql' with a WHERE clause"))
    ds = _k_data_source(p)
    title = p.title
    cfg = {}
    chart = p.chart
    if chart == "metric":
        cfg = {"type": "metric", "data_source": ds}
        ms = [_k_metric(p, p.metrics[0], ctx, problems, "primary")]
        if len(p.metrics) > 1:
            sec = _k_metric(p, p.metrics[1], ctx, problems, "secondary")
            ms.append(sec)
        if p.trend and p.bands:
            problems.append(("error", p.where, "metric cannot have both 'trend' and 'bands' (Lens colors the "
                                               "background instead of the value)"))
        if p.trend:
            if p.esql and not ctx.gate("esql_trend"):
                ctx.notes.append("{}: trend background needs Kibana 9.5+ on ES|QL; omitted".format(p.where))
            else:
                ms[0]["background_chart"] = {"type": "trend"}
        if p.bands:
            b = p.bands if isinstance(p.bands, dict) else {"thresholds": p.bands}
            ms[0]["apply_color_to"] = "value"
            ms[0]["color"] = {"type": "dynamic", "range": "absolute",
                              "steps": _bands_steps(b["thresholds"], 3, b.get("palette", "status"),
                                                    b.get("higher_is_better", False), b.get("max"), b.get("min", 0))}
        cfg["metrics"] = ms
        title = None
    elif chart in XY_CHARTS:
        layer_type = chart
        series = p.series_count()
        if chart.startswith("area") and series > 3:
            layer_type = "line"
            ctx.notes.append("{}: {} series is too many for areas; using a line chart".format(p.where, series))
        layer = {"type": layer_type, "data_source": ds}
        if p.esql:
            x_col = p.x
            if not x_col:
                byc = esql_by_columns(p.esql)
                x_col = byc[0] if byc else None
            if not x_col:
                problems.append(("error", p.where, "ES|QL xy panel needs 'x' (a column)"))
            layer["x"] = {"column": x_col}
            time_x = bool(x_col and re.match(r"(?i)^(BUCKET|TBUCKET|DATE_TRUNC)\(", x_col))
            if time_x:
                tm = re.search(r"\(\s*([\w.@]+)", x_col)
                layer["x"]["label"] = tm.group(1) if tm else "@timestamp"
        else:
            layer["x"] = _k_bucket(p, p.x, ctx, problems)
            time_x = p.x == "time"
        layer["y"] = [_k_metric(p, m, ctx, problems) for m in p.metrics]
        if p.split:
            sp = p.split if isinstance(p.split, dict) else {"field": p.split}
            layer["breakdown_by"] = _k_bucket(p, sp.get("field") or sp.get("column"), ctx, problems, sp.get("size"))
            if layer_type == "line":
                layer["breakdown_by"]["color"] = {"mode": "categorical", "palette": "elastic_line_optimized",
                                                  "mapping": []}
        elif layer_type == "line" and len(layer["y"]) > 1:
            for k, y in enumerate(layer["y"]):
                y["color"] = {"type": "static", "color": CATEGORICAL_PALETTES["elastic_line_optimized"][k % 5]}
        multi = bool(p.split) or len(layer["y"]) > 1
        legend = {"position": "bottom", "visibility": "visible" if (multi or p.legend_stats) else "hidden",
                  "layout": {"type": "grid" if p.legend_stats else "list"}}
        if p.legend_stats:
            legend["statistics"] = p.legend_stats
        axis = {"x": {"title": {"visible": False}}, "y": {"title": {"visible": False}}}
        if time_x and p.esql:
            axis["x"].update({"scale": "temporal", "domain": {"type": "fit", "rounding": False}})
        cfg = {"type": "xy", "axis": axis, "legend": legend, "layers": [layer]}
        if layer_type.startswith("area"):
            if ctx.gate("gradient_areas"):
                cfg["styling"] = {"areas": {"fill": "gradient"}}
            else:
                ctx.notes.append("{}: gradient area fill needs Kibana 9.6+; solid fill".format(p.where))
    elif chart in PARTITION_CHARTS:
        cfg = {"type": "pie" if chart == "donut" else chart, "data_source": ds,
               "metrics": [_k_metric(p, m, ctx, problems) for m in p.metrics],
               "group_by": [_k_bucket(p, b, ctx, problems) for b in p.by]}
        if chart == "donut":
            cfg["styling"] = {"donut_hole": "m"}
        if chart == "waffle":
            title = None
    elif chart == "table":
        rows = []
        for b in p.by:
            r = _k_bucket(p, b, ctx, problems)
            if "operation" in r:
                r["rank_by"] = {"type": "metric", "metric_index": 0, "direction": "desc"}
            rows.append(r)
        cfg = {"type": "data_table", "data_source": ds,
               "metrics": [_k_metric(p, m, ctx, problems) for m in p.metrics], "rows": rows}
    elif chart == "heatmap":
        cfg = {"type": "heatmap", "data_source": ds, "metric": _k_metric(p, p.metrics[0], ctx, problems),
               "x": _k_bucket(p, p.x or "time", ctx, problems),
               "y": _k_bucket(p, p.y_field or p.split, ctx, problems)}
    elif chart == "tag_cloud":
        cfg = {"type": "tag_cloud", "data_source": ds, "metric": _k_metric(p, p.metrics[0], ctx, problems),
               "tag_by": _k_bucket(p, p.by[0] if p.by else None, ctx, problems)}
        title = None
    elif chart == "gauge":
        m = _k_metric(p, p.metrics[0], ctx, problems)
        b = p.raw.get("bands") or [25, 50, 75]
        m["color"] = {"type": "dynamic", "range": "percentage",
                      "steps": _bands_steps(b, 4, p.raw.get("palette", "status"), False, 100, 0)}
        cfg = {"type": "gauge", "data_source": ds, "metric": m}
        for k in ("min", "max", "goal"):
            if k in p.raw:
                cfg[k] = p.raw[k] if isinstance(p.raw[k], dict) else {"value": p.raw[k]}
        title = None
    elif chart == "region_map":
        cfg = {"type": "region_map", "data_source": ds, "metric": _k_metric(p, p.metrics[0], ctx, problems),
               "region": _k_bucket(p, p.by[0] if p.by else None, ctx, problems)}
    if title:
        cfg["title"] = title
    deep_merge(cfg, p.kibana_extra)
    return cfg


def deep_merge(dst, src):
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def compile_kibana(spec, ctx):
    problems = list(spec.problems)
    if spec.standalone:
        p = spec.panels()[0]
        cfg = compile_kibana_panel(p, ctx, problems)
        if p.title and p.chart in NO_TITLE_CHARTS:
            cfg["title"] = p.title  # standalone library items keep a name
        return {"kind": "kibana_visualization", "id": spec.id, "body": cfg}, problems
    body = {"title": spec.title or "", "description": spec.description or "", "panels": []}
    if spec.time_range:
        body["time_range"] = spec.time_range
    outer_y = 0
    for s in spec.sections:
        grids = layout_section(s["panels"])
        panels = []
        for p, g in zip(s["panels"], grids):
            if p.chart == "markdown":
                panels.append({"type": "markdown", "id": p.id, "grid": g, "config": {"content": p.text}})
            else:
                panels.append({"type": "vis", "id": p.id, "grid": g,
                               "config": compile_kibana_panel(p, ctx, problems)})
        if s["title"]:
            body["panels"].append({"title": s["title"], "collapsed": s.get("collapsed", False),
                                   "grid": {"y": outer_y}, "panels": panels})
            outer_y += 1
        else:
            for pn in panels:
                pn["grid"]["y"] += outer_y
            body["panels"].extend(panels)
            outer_y = max([pn["grid"]["y"] + pn["grid"]["h"] for pn in panels] + [outer_y])
    if spec.controls:
        body["pinned_panels"] = []
        esql_mode = spec.all_esql() and ctx.gate("esql_controls")
        for c in spec.controls:
            ctype = c.get("type", "options")
            if ctype in ("time", "time_slider"):
                body["pinned_panels"].append({"type": "time_slider_control", "config": {}})
                continue
            ix = c.get("index") or spec.index
            field = ctx.f(ix).resolve(c["field"], True, problems, "control " + c["field"])
            conf = {"title": c.get("title") or humanize(c["field"])}
            if ctype == "range":
                conf.update({"data_view_id": "{{data_view:%s}}" % ix, "field_name": field})
                body["pinned_panels"].append({"type": "range_slider_control", "width": "medium", "grow": True,
                                              "config": conf})
                continue
            if esql_mode:
                src = c.get("esql") or "FROM {} | STATS BY {}".format(ix, field)
                conf.update({"values_source": "esql", "esql_query": src, "selected_options": []})
            else:
                conf.update({"data_view_id": "{{data_view:%s}}" % ix, "field_name": field})
            body["pinned_panels"].append({"type": "options_list_control", "width": "medium", "grow": True,
                                          "config": conf})
    return {"kind": "kibana_dashboard", "id": spec.id or slug(spec.title), "body": body}, problems


# --- OpenSearch Dashboards compiler ------------------------------------------

def _o_agg_metric(aid, m, p, ctx, problems):
    op = m["op"]
    params = {}
    if m.get("field"):
        params["field"] = ctx.f(p.index).resolve(m["field"], True, problems, p.where)
    t = {"count": "count", "average": "avg", "sum": "sum", "min": "min", "max": "max", "median": "median",
         "unique_count": "cardinality", "percentile": "percentiles", "last_value": "top_hits"}[op]
    if op == "percentile":
        params["percents"] = [m.get("percentile", 95)]
    if op == "last_value":
        params.update({"aggregate": "concat", "size": 1, "sortField": p.time_field, "sortOrder": "desc"})
    label = metric_label(m)
    if label and (op != "count" or m.get("label")):
        params["customLabel"] = label
    return {"id": str(aid), "enabled": True, "type": t, "schema": "metric", "params": params}


def _o_bucket(aid, schema, field, p, ctx, problems, size=None, order_by="1"):
    if field in ("time", None):
        return {"id": str(aid), "enabled": True, "type": "date_histogram", "schema": schema,
                "params": {"field": p.time_field, "interval": "auto", "min_doc_count": 1,
                           "extended_bounds": {}}}
    f = ctx.f(p.index).resolve(field, True, problems, p.where)
    return {"id": str(aid), "enabled": True, "type": "terms", "schema": schema,
            "params": {"field": f, "size": int(size or p.size), "order": "desc", "orderBy": order_by,
                       "otherBucket": False, "missingBucket": False, "customLabel": humanize(f)}}


def _o_xy_params(p, chart, metric_aggs, multi):
    horizontal = chart.startswith("bar_horizontal")
    series_type = "line" if chart == "line" else ("area" if chart.startswith("area") else "histogram")
    mode = "normal"
    if chart.endswith("_stacked") or chart.endswith("_percentage") or (series_type == "histogram" and p.split):
        mode = "stacked"
    cat_pos, val_pos = ("left", "bottom") if horizontal else ("bottom", "left")
    params = {
        "type": "horizontal_bar" if horizontal else series_type,
        "grid": {"categoryLines": False, "valueAxis": "ValueAxis-1" if not horizontal else None},
        "categoryAxes": [{"id": "CategoryAxis-1", "type": "category", "position": cat_pos, "show": True,
                          "style": {}, "scale": {"type": "linear"},
                          # horizontal bars: flat, unfiltered names (vislib rotates and drops every other one)
                          "labels": ({"show": True, "rotate": 0, "filter": False, "truncate": 200} if horizontal
                                     else {"show": True, "filter": True, "truncate": 100}),
                          "title": {"text": ""}}],
        "valueAxes": [{"id": "ValueAxis-1", "name": "LeftAxis-1" if not horizontal else "BottomAxis-1",
                       "type": "value", "position": val_pos, "show": True, "style": {},
                       "scale": {"type": "linear", "mode": "percentage" if chart.endswith("_percentage") else "normal"},
                       "labels": {"show": True, "rotate": 0, "filter": not horizontal, "truncate": 100},
                       "title": {"text": ""}}],
        "seriesParams": [],
        "addTooltip": True, "addLegend": bool(multi or p.legend_stats), "legendPosition": "bottom",
        "times": [], "addTimeMarker": False, "labels": {"show": False},
        "thresholdLine": {"show": False, "value": 10, "width": 1, "style": "full", "color": "#E7664C"},
    }
    if params["grid"]["valueAxis"] is None:
        del params["grid"]["valueAxis"]
    for a in metric_aggs:
        sp = {"show": True, "type": series_type, "mode": mode,
              "data": {"label": a["params"].get("customLabel") or "Count", "id": a["id"]},
              "valueAxis": "ValueAxis-1", "drawLinesBetweenPoints": True, "lineWidth": 2,
              "interpolate": "linear", "showCircles": series_type == "line"}
        params["seriesParams"].append(sp)
    return params


def compile_osd_panel(p, ctx, problems):
    """-> (visState dict, search query string)"""
    chart = p.chart
    title = p.title or (metric_label(p.metrics[0]) if p.metrics else humanize(chart))
    if p.esql:
        problems.append(("error", p.where, "ES|QL panels are Kibana-only; on OpenSearch Dashboards use "
                                           "metric/x/split/by fields"))
        return None
    if p.format:
        ctx.notes.append("{}: per-panel number formats are not supported by classic OSD visualizations; "
                         "set the field format on the index pattern instead".format(p.where))
    aggs, params = [], {}
    if chart == "markdown":
        return {"title": p.title or "Notes", "type": "markdown", "aggs": [],
                "params": {"fontSize": 12, "openLinksInNewTab": False, "markdown": p.text}}, ""
    metric_aggs = [_o_agg_metric(i + 1, m, p, ctx, problems) for i, m in enumerate(p.metrics)]
    aggs.extend(metric_aggs)
    nid = len(aggs) + 1
    if chart == "metric":
        vtype = "metric"
        params = {"addTooltip": True, "addLegend": False, "type": "metric",
                  "metric": {"percentageMode": False, "useRanges": False, "colorSchema": "Green to Red",
                             "metricColorMode": "None", "colorsRange": [{"from": 0, "to": 10000}],
                             "labels": {"show": True}, "invertColors": False,
                             "style": {"bgFill": "#000", "bgColor": False, "labelColor": False, "subText": "",
                                       "fontSize": 36 if (p.w or 12) >= 12 else 28}}}
        if p.bands:
            b = p.bands if isinstance(p.bands, dict) else {"thresholds": p.bands}
            th = list(b["thresholds"])
            edges = [b.get("min", 0)] + th + [b.get("max", th[-1] * 10 if th else 100)]
            params["metric"].update({"metricColorMode": "Labels", "useRanges": True,
                                     "colorsRange": [{"from": edges[i], "to": edges[i + 1]} for i in range(3)],
                                     "invertColors": bool(b.get("higher_is_better"))})
        if p.trend:
            ctx.notes.append("{}: trend backgrounds do not exist in OSD metric visualizations; omitted".format(p.where))
    elif chart in XY_CHARTS:
        series = p.series_count()
        eff = chart
        if chart.startswith("area") and series > 3:
            eff = "line"
            ctx.notes.append("{}: {} series is too many for areas; using a line chart".format(p.where, series))
        aggs.append(_o_bucket(nid, "segment", p.x, p, ctx, problems))
        nid += 1
        if p.split:
            sp = p.split if isinstance(p.split, dict) else {"field": p.split}
            aggs.append(_o_bucket(nid, "group", sp.get("field"), p, ctx, problems, sp.get("size")))
        params = _o_xy_params(p, eff, metric_aggs, bool(p.split) or len(metric_aggs) > 1)
        vtype = params["type"]
        if p.legend_stats:
            ctx.notes.append("{}: legend statistics are Kibana-only; omitted".format(p.where))
    elif chart in PARTITION_CHARTS:
        if chart in ("treemap", "waffle", "mosaic"):
            ctx.notes.append("{}: {} is not a classic OSD visualization; rendered as a donut".format(p.where, chart))
        vtype = "pie"
        for b in p.by:
            aggs.append(_o_bucket(nid, "segment", b, p, ctx, problems))
            nid += 1
        params = {"type": "pie", "addTooltip": True, "addLegend": True, "legendPosition": "right",
                  "isDonut": chart != "pie",
                  "labels": {"show": False, "values": True, "last_level": True, "truncate": 100}}
    elif chart == "table":
        vtype = "table"
        for b in p.by:
            aggs.append(_o_bucket(nid, "bucket", b, p, ctx, problems))
            nid += 1
        params = {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False, "showTotal": False,
                  "totalFunc": "sum", "percentageCol": "", "sort": {"columnIndex": None, "direction": None}}
    elif chart == "heatmap":
        vtype = "heatmap"
        aggs.append(_o_bucket(nid, "segment", p.x or "time", p, ctx, problems))
        aggs.append(_o_bucket(nid + 1, "group", p.y_field or p.split, p, ctx, problems))
        params = {"type": "heatmap", "addTooltip": True, "addLegend": True, "enableHover": False,
                  "legendPosition": "right", "times": [], "colorsNumber": 4, "colorSchema": "Greens",
                  "setColorRange": False, "colorsRange": [], "invertColors": False, "percentageMode": False,
                  "valueAxes": [{"show": False, "id": "ValueAxis-1", "type": "value",
                                 "scale": {"type": "linear", "defaultYExtents": False},
                                 "labels": {"show": False, "rotate": 0, "overwriteColor": False,
                                            "color": "black"}}]}
    elif chart == "tag_cloud":
        vtype = "tagcloud"
        aggs.append(_o_bucket(nid, "segment", p.by[0] if p.by else None, p, ctx, problems))
        params = {"scale": "linear", "orientation": "single", "minFontSize": 18, "maxFontSize": 72,
                  "showLabel": True}
    elif chart == "gauge":
        vtype = "gauge"
        lo, hi = p.raw.get("min"), p.raw.get("max")
        if lo is None or hi is None:
            ctx.notes.append("{}: OSD gauges need a range; none given, so the default 0-100 bands are used "
                             "(set 'min'/'max' if the value is not a 0-100 percentage)".format(p.where))
            lo, hi = 0, 100
        q = (hi - lo) / 4.0
        ranges = [{"from": lo + q * i, "to": lo + q * (i + 1)} for i in range(4)]
        params = {"type": "gauge", "addTooltip": True, "addLegend": False, "isDisplayWarning": False,
                  "gauge": {"alignment": "automatic", "extendRange": True, "percentageMode": False,
                            "gaugeType": "Arc", "gaugeStyle": "Full", "backStyle": "Full",
                            "orientation": "vertical", "colorSchema": "Green to Red", "gaugeColorMode": "Labels",
                            "colorsRange": ranges, "invertColors": False,
                            "labels": {"show": True, "color": "black"},
                            "scale": {"show": True, "labels": False, "color": "rgba(105,112,125,0.2)"},
                            "type": "meter",
                            "style": {"bgWidth": 0.9, "width": 0.9, "mask": False, "bgMask": False,
                                      "maskBars": 50, "bgFill": "rgba(105,112,125,0.2)", "bgColor": True,
                                      "subText": "", "fontSize": 60}}}
    else:
        problems.append(("error", p.where, "{} is not supported on OpenSearch Dashboards".format(chart)))
        return None
    vis = {"title": title, "type": vtype, "aggs": aggs, "params": params}
    deep_merge(vis, p.osd_extra)
    return vis, p.filter or ""


def _osd_vis_object(vid, vis, query, index_ref, description=""):
    ssj = {"query": {"query": query or "", "language": "kuery"}, "filter": []}
    refs = []
    if index_ref:
        ssj["indexRefName"] = "kibanaSavedObjectMeta.searchSourceJSON.index"
        refs.append({"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern",
                     "id": index_ref})
    return {"type": "visualization", "id": vid,
            "attributes": {"title": vis["title"], "visState": vis, "uiStateJSON": {},
                           "description": description, "version": 1,
                           "kibanaSavedObjectMeta": {"searchSourceJSON": ssj}},
            "references": refs}


def _controls_vis(spec, ctx, problems):
    controls, refs = [], []
    for i, c in enumerate(spec.controls):
        ctype = c.get("type", "options")
        if ctype in ("time", "time_slider"):
            ctx.notes.append("time slider controls are Kibana-only; omitted")
            continue
        ix = c.get("index") or spec.index
        field = ctx.f(ix).resolve(c["field"], ctype != "range", problems, "control " + c["field"])
        ref = "control_{}_index_pattern".format(len(controls))
        ctl = {"id": str(len(controls) + 1), "fieldName": field, "parent": "",
               "label": c.get("title") or humanize(c["field"]), "type": "range" if ctype == "range" else "list",
               "indexPatternRefName": ref}
        ctl["options"] = ({"decimalPlaces": 0, "step": 1} if ctype == "range" else
                          {"type": "terms", "multiselect": True, "dynamicOptions": True, "size": 5,
                           "order": "desc"})
        controls.append(ctl)
        refs.append({"name": ref, "type": "index-pattern", "id": ctx.index_ids.get(ix) or "{{index_pattern:%s}}" % ix})
    if not controls:
        return None
    vis = {"title": "Filters", "type": "input_control_vis", "aggs": [],
           "params": {"controls": controls, "updateFiltersOnChange": True, "useTimeFilter": True,
                      "pinFilters": False}}
    return vis, refs


def compile_osd(spec, ctx):
    problems = list(spec.problems)
    dash_id = spec.id or slug(spec.title or "visualization")
    vis_objs = []

    def index_ref(p):
        return ctx.index_ids.get(p.index) or "{{index_pattern:%s}}" % p.index

    if spec.standalone:
        p = spec.panels()[0]
        out = compile_osd_panel(p, ctx, problems)
        if out:
            vis, q = out
            vis_objs.append(_osd_vis_object(dash_id, vis, q, index_ref(p) if p.chart != "markdown" else None))
        return {"kind": "osd_bundle", "dashboard": None, "visualizations": vis_objs}, problems

    placed = []  # (grid, vis_obj, hide_title) in one coordinate space; OSD has no sections
    y = 0
    if spec.controls:
        cv = _controls_vis(spec, ctx, problems)
        if cv:
            vis, refs = cv
            obj = _osd_vis_object(dash_id + "--controls", vis, "", None)
            obj["references"] = refs
            placed.append(({"x": 0, "y": y, "w": 48, "h": 5}, obj, True))
            y += 5
    multi = len([s for s in spec.sections if s["panels"]]) > 1
    for s in spec.sections:
        if not s["panels"]:
            continue
        if s["title"] and multi:
            vis = {"title": s["title"], "type": "markdown", "aggs": [],
                   "params": {"fontSize": 12, "openLinksInNewTab": False, "markdown": "### " + s["title"]}}
            placed.append(({"x": 0, "y": y, "w": 48, "h": 3},
                           _osd_vis_object(dash_id + "--section-" + slug(s["title"], 30), vis, "", None), True))
            y += 3
        base = y
        for p, g in zip(s["panels"], layout_section(s["panels"])):
            out = compile_osd_panel(p, ctx, problems)
            if not out:
                continue
            vis, q = out
            obj = _osd_vis_object(dash_id + "--" + p.id, vis, q, index_ref(p) if p.chart != "markdown" else None)
            g = dict(g, y=g["y"] + base)
            y = max(y, g["y"] + g["h"])
            placed.append((g, obj, p.chart in NO_TITLE_CHARTS or (p.chart == "markdown" and not p.title)))
    panels, refs = [], []
    for i, (g, obj, hide) in enumerate(placed):
        pi = str(i + 1)
        panels.append({"version": ctx.version_str, "gridData": dict(g, i=pi), "panelIndex": pi,
                       "embeddableConfig": {"hidePanelTitles": True} if hide else {},
                       "panelRefName": "panel_{}".format(i)})
        refs.append({"name": "panel_{}".format(i), "type": "visualization", "id": obj["id"]})
        vis_objs.append(obj)
    attrs = {"title": spec.title or "", "description": spec.description or "", "panelsJSON": panels,
             "optionsJSON": {"hidePanelTitles": False, "useMargins": True}, "version": 1,
             "timeRestore": bool(spec.time_range),
             "kibanaSavedObjectMeta": {"searchSourceJSON": {"query": {"query": "", "language": "kuery"},
                                                            "filter": []}}}
    if spec.time_range:
        attrs.update({"timeFrom": spec.time_range["from"], "timeTo": spec.time_range.get("to", "now"),
                      "refreshInterval": {"pause": True, "value": 0}})
    dash = {"type": "dashboard", "id": dash_id, "attributes": attrs, "references": refs}
    return {"kind": "osd_bundle", "dashboard": dash, "visualizations": vis_objs}, problems


# OSD stores these attributes as JSON strings; bundles keep them decoded for readability.
OSD_JSON_ATTRS = {"visualization": ["visState", "uiStateJSON"], "dashboard": ["panelsJSON", "optionsJSON"]}


def osd_encode(obj):
    o = copy.deepcopy(obj)
    a = o["attributes"]
    for k in OSD_JSON_ATTRS.get(o["type"], []):
        if k in a and not isinstance(a[k], str):
            a[k] = json.dumps(a[k], ensure_ascii=False)
    meta = a.get("kibanaSavedObjectMeta")
    if meta and not isinstance(meta.get("searchSourceJSON"), str):
        meta["searchSourceJSON"] = json.dumps(meta.get("searchSourceJSON") or {}, ensure_ascii=False)
    return o


def osd_decode(obj):
    o = {k: copy.deepcopy(obj.get(k)) for k in ("type", "id", "attributes", "references") if k in obj}
    a = o.get("attributes") or {}
    for k in OSD_JSON_ATTRS.get(o.get("type"), []):
        if isinstance(a.get(k), str):
            try:
                a[k] = json.loads(a[k])
            except ValueError:
                pass
    meta = a.get("kibanaSavedObjectMeta")
    if meta and isinstance(meta.get("searchSourceJSON"), str):
        try:
            meta["searchSourceJSON"] = json.loads(meta["searchSourceJSON"])
        except ValueError:
            pass
    return o


# --- Lint ---------------------------------------------------------------------

def lint_grid(panels, where, problems, label=lambda p: p.get("id")):
    """panels: list of dicts with 'grid' {x,y,w,h}, 'chart'."""
    cells = {}
    rows = {}
    for p in panels:
        g = p.get("grid") or {}
        if not all(k in g for k in ("x", "y", "w", "h")):
            problems.append(("error", "{}{}".format(where, label(p)), "grid needs x, y, w, h"))
            continue
        x, y, w, h = int(g["x"]), int(g["y"]), int(g["w"]), int(g["h"])
        name = "{}{}".format(where, label(p))
        if x < 0 or x + w > GRID_COLUMNS:
            problems.append(("error", name, "x+w = {} exceeds the 48-column grid".format(x + w)))
        if GRID_COLUMNS % w and w not in (16, 20, 28, 32, 36, 40):
            problems.append(("info", name, "w={} does not divide 48 (prefer 6, 8, 12, 16, 24, 48)".format(w)))
        ch = p.get("chart")
        if ch in ("metric", "gauge") and w >= 48:
            problems.append(("warn", name, "{} panel is full-width; keep it 6-16 columns".format(ch)))
        for cx in range(x, min(x + w, GRID_COLUMNS)):
            for cy in range(y, y + h):
                if (cx, cy) in cells and cells[(cx, cy)] != name:
                    problems.append(("error", name, "overlaps {}".format(cells[(cx, cy)])))
                    break
                cells[(cx, cy)] = name
            else:
                continue
            break
        rows.setdefault(y, []).append((x, w, h, name))
    for y, items in rows.items():
        hs = {h for _, _, h, _ in items}
        if len(items) > 1 and len(hs) > 1:
            problems.append(("warn", "{}row y={}".format(where, y),
                             "panels in a row have different heights {}".format(sorted(hs))))
        used = sum(w for _, w, _, _ in items)
        if 0 < used < GRID_COLUMNS and len(items) > 1:
            problems.append(("info", "{}row y={}".format(where, y), "row uses {}/48 columns".format(used)))


def _is_multi_series(layer):
    return bool(layer.get("breakdown_by")) or len(layer.get("y") or []) > 1


def lint_lens_config(cfg, name, problems, version=None):
    if not isinstance(cfg, dict):
        return
    t = cfg.get("type")
    if t in NO_TITLE_CHARTS and cfg.get("title"):
        problems.append(("warn", name, "{} panels show their own label; drop the panel title".format(t)))
    if t == "xy":
        axis = cfg.get("axis") or {}
        for ax in ("x", "y"):
            if ((axis.get(ax) or {}).get("title") or {}).get("visible", True) is not False:
                problems.append(("warn", name, "set axis.{}.title.visible=false (no axis titles)".format(ax)))
        legend = cfg.get("legend") or {}
        multi = any(_is_multi_series(l) for l in cfg.get("layers") or [])
        vis = legend.get("visibility")
        if (multi or legend.get("statistics")) and vis != "visible":
            problems.append(("error", name, "multi-series/statistics chart needs legend.visibility='visible' "
                                            "(the API defaults it to hidden)"))
        if not multi and not legend.get("statistics") and vis == "visible":
            problems.append(("info", name, "single-series chart: legend.visibility='hidden' is preferred"))
        if legend.get("position") not in (None, "bottom"):
            problems.append(("info", name, "legend position {!r}; bottom is preferred".format(legend.get("position"))))
        lay = (legend.get("layout") or {}).get("type")
        if legend.get("statistics") and lay != "grid":
            problems.append(("warn", name, "legend with statistics should use layout.type='grid'"))
        if not legend.get("statistics") and lay != "list":
            problems.append(("warn", name, "set legend.layout.type='list' (API default is grid)"))
        for s in legend.get("statistics") or []:
            if s not in LEGEND_STATS:
                problems.append(("error", name, "unknown legend statistic {!r}".format(s)))
        for l in cfg.get("layers") or []:
            if "data_source" not in l and l.get("type") not in ("annotations", "reference_lines"):
                problems.append(("error", name, "every xy layer needs its own data_source"))
            q = ((l.get("data_source") or {}).get("query")) or ""
            xcol = (l.get("x") or {}).get("column") or ""
            if re.match(r"(?i)^(BUCKET|TBUCKET|DATE_TRUNC)\(", xcol) and (axis.get("x") or {}).get("scale") != "temporal":
                problems.append(("warn", name, "time x axis needs axis.x.scale='temporal'"))
            if str(l.get("type", "")).startswith("area"):
                n = len(l.get("y") or [])
                if l.get("breakdown_by"):
                    n *= int((l["breakdown_by"].get("limit") or 4))
                if n > 3:
                    problems.append(("warn", name, "areas with >3 series hide each other; use line"))
                if version and version < GATES["gradient_areas"] + (0,):
                    pass
                elif not ((cfg.get("styling") or {}).get("areas") or {}).get("fill") == "gradient":
                    problems.append(("info", name, "area series: styling.areas.fill='gradient' (9.6+)"))
            if l.get("type") == "line" and l.get("breakdown_by"):
                pal = ((l["breakdown_by"].get("color") or {}).get("palette"))
                if pal != "elastic_line_optimized":
                    problems.append(("info", name, "line breakdowns: use the elastic_line_optimized palette"))
            if q and "?_tstart" not in q and re.search(r"(?i)bucket\(", q):
                problems.append(("warn", name, "time-series ES|QL without ?_tstart/?_tend ignores the time picker"))
    if t == "metric":
        for m in cfg.get("metrics") or []:
            if m.get("apply_color_to") and not m.get("color"):
                problems.append(("error", name, "apply_color_to without color tints the value green"))
            if m.get("background_chart") and (m.get("color") or m.get("apply_color_to")):
                problems.append(("error", name, "background_chart + color turns the tile into a colored block"))
            q = ((cfg.get("data_source") or {}).get("query")) or ""
            if m.get("background_chart", {}).get("type") == "trend" and q:
                if re.match(r"(?is)^\s*TS\s", q) and re.search(r"(?i)BY\s+BUCKET\(", q):
                    problems.append(("error", name, "trend on a TS query must not group by BUCKET (API adds TBUCKET)"))
                if re.match(r"(?is)^\s*FROM\s", q) and "?_tstart" not in q:
                    problems.append(("warn", name, "trend on a FROM query needs a ?_tstart/?_tend filter"))
                if version and version < GATES["esql_trend"] + (0,):
                    problems.append(("warn", name, "ES|QL trend backgrounds need Kibana 9.5+"))
            if (cfg.get("data_source") or {}).get("type") == "esql" and m.get("operation"):
                problems.append(("error", name, "ES|QL metrics reference a column, not an operation"))
    if t == "gauge":
        pass
    _lint_colors(cfg, name, problems)
    _lint_formats(cfg, name, problems)


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _lint_colors(cfg, name, problems):
    for o in _walk(cfg):
        if o.get("type") == "legacy_dynamic":
            problems.append(("error", name, "type 'legacy_dynamic' is deprecated"))
        pal = o.get("palette")
        if isinstance(pal, str) and pal in LEGACY_PALETTES:
            problems.append(("warn", name, "legacy palette {!r}".format(pal)))
        steps = o.get("steps")
        if isinstance(steps, list) and steps and isinstance(steps[0], dict):
            for i, s in enumerate(steps):
                if i > 0 and "gte" not in s:
                    problems.append(("error", name, "color step {} needs 'gte'".format(i)))
                if i < len(steps) - 1 and "lt" not in s:
                    problems.append(("error", name, "color step {} needs 'lt'".format(i)))
                if i > 0 and "gte" in s and "lt" in steps[i - 1] and steps[i - 1]["lt"] != s["gte"]:
                    problems.append(("error", name, "color steps are not continuous at {}".format(s["gte"])))
                c = str(s.get("color", "")).lower()
                if c and c not in CATALOG_HEX:
                    problems.append(("info", name, "color {} is not from the Kibana palette catalog".format(c)))


def _lint_formats(cfg, name, problems):
    for o in _walk(cfg):
        f = o.get("format")
        if isinstance(f, dict) and f.get("type") == "duration":
            if f.get("from") not in DURATION_UNITS:
                problems.append(("error", name, "format.duration.from must be one of {}".format(
                    ", ".join(sorted(DURATION_UNITS)))))


def lint_kibana_body(body, problems, version=None, kind="dashboard"):
    if kind == "visualization":
        lint_lens_config(body, "viz", problems, version)
        return
    if not body.get("title"):
        problems.append(("error", "dashboard", "empty title"))
    if "panels" not in body:
        problems.append(("error", "dashboard", "missing 'panels' (use [] for an empty dashboard)"))
    if "data" in body and "panels" not in body:
        problems.append(("error", "dashboard", "body is wrapped in 'data' -- send it flat"))
    top, sections = [], []
    for p in body.get("panels") or []:
        if "panels" in p and "config" not in p:
            sections.append(p)
        else:
            top.append(p)

    def chart_of(p):
        return (p.get("config") or {}).get("type") if p.get("type") == "vis" else p.get("type")

    lint_grid([dict(p, chart=chart_of(p)) for p in top], "", problems)
    ys = []
    for s in sections:
        ys.append((s.get("grid") or {}).get("y"))
        sp = s.get("panels") or []
        if not sp:
            problems.append(("warn", "section {!r}".format(s.get("title")), "empty section"))
        lint_grid([dict(p, chart=chart_of(p)) for p in sp], "{}/".format(s.get("title")), problems)
        kinds = {chart_of(p) for p in sp}
        if "metric" in kinds and kinds - {"metric", "gauge", "markdown"} and re.search(
                r"(?i)key|summary|kpi|overview|metric", s.get("title") or ""):
            problems.append(("info", "section {!r}".format(s.get("title")), "summary section mixes metrics "
                                                                             "with other chart types"))
    n_vis = sum(1 for p in top if p.get("type") == "vis") + sum(len(s.get("panels") or []) for s in sections)
    if n_vis >= 6 and not sections:
        problems.append(("info", "dashboard", "{} panels and no sections -- consider sections".format(n_vis)))
    for p in top + [q for s in sections for q in s.get("panels") or []]:
        if p.get("type") == "vis":
            lint_lens_config(p.get("config"), p.get("id") or "?", problems, version)
    for c in body.get("pinned_panels") or []:
        conf = c.get("config") or {}
        if c.get("type") == "options_list_control":
            if not (conf.get("data_view_id") and conf.get("field_name")) and conf.get("values_source") != "esql":
                problems.append(("error", "control", "options list needs data_view_id+field_name or an ES|QL source"))
            if conf.get("values_source") == "esql" and version and version < GATES["esql_controls"] + (0,):
                problems.append(("error", "control", "ES|QL-sourced controls need Kibana 9.5+"))
        if "{{data_view:" in json.dumps(conf):
            problems.append(("info", "control", "data view placeholder -- resolved (found or created) on apply"))


def lint_osd_bundle(bundle, problems):
    d = bundle.get("dashboard")
    vis = {v["id"]: v for v in bundle.get("visualizations") or []}
    if not d:
        return
    a = d["attributes"]
    if not a.get("title"):
        problems.append(("error", "dashboard", "empty title"))
    panels = a.get("panelsJSON")
    if isinstance(panels, str):
        panels = json.loads(panels)
    refs = {r["name"]: r for r in d.get("references") or []}
    items = []
    for p in panels or []:
        r = panel_ref(refs, p)
        vtype = None
        if r and r["id"] in vis:
            vs = vis[r["id"]]["attributes"].get("visState")
            vs = json.loads(vs) if isinstance(vs, str) else vs
            vtype = vs.get("type")
        elif not r:
            problems.append(("error", "panel " + str(p.get("panelIndex")), "panelRefName has no reference"))
        items.append({"grid": p.get("gridData"), "chart": vtype, "id": "{}({})".format(p.get("panelIndex"), vtype)})
    lint_grid(items, "", problems)


# --- Validation against data --------------------------------------------------

def _dsl_metric(m, p, fields):
    f = fields.resolve(m.get("field"), True) if m.get("field") else None
    op = m.get("op")
    if op == "count":
        return None
    return {"average": {"avg": {"field": f}}, "sum": {"sum": {"field": f}}, "min": {"min": {"field": f}},
            "max": {"max": {"field": f}}, "median": {"percentiles": {"field": f, "percents": [50]}},
            "unique_count": {"cardinality": {"field": f}},
            "percentile": {"percentiles": {"field": f, "percents": [m.get("percentile", 95)]}},
            "last_value": {"max": {"field": p.time_field}}}.get(op)


def _kql_to_query_string(kql):
    q = re.sub(r"\s+and\s+", " AND ", kql, flags=re.I)
    q = re.sub(r"\s+or\s+", " OR ", q, flags=re.I)
    q = re.sub(r"(^|\s)not\s+", r"\1NOT ", q, flags=re.I)
    return q


def panel_dsl(p, fields, time_range):
    must = [{"range": {p.time_field: {"gte": time_range["from"], "lte": time_range.get("to", "now")}}}]
    if p.filter:
        must.append({"query_string": {"query": _kql_to_query_string(p.filter), "analyze_wildcard": True}})
    maggs = {}
    for i, m in enumerate(p.metrics):
        d = _dsl_metric(m, p, fields)
        if d:
            maggs["m{}".format(i)] = d
    buckets = []
    if p.chart in XY_CHARTS or p.chart == "heatmap":
        buckets.append(p.x or "time")
        if p.split or p.y_field:
            sp = p.split if not isinstance(p.split, dict) else p.split.get("field")
            buckets.append(p.y_field or sp)
    elif p.chart in PARTITION_CHARTS | {"table", "tag_cloud", "region_map"}:
        buckets.extend(p.by[:2])
    aggs = maggs
    for k, b in reversed(list(enumerate(buckets))):
        if b == "time":
            node = {"auto_date_histogram": {"field": p.time_field, "buckets": 20}}
        else:
            node = {"terms": {"field": fields.resolve(b, True), "size": p.size}}
            m0 = maggs.get("m0") or {}
            if m0 and not ("percentiles" in m0):
                node["terms"]["order"] = {"m0": "desc"}  # rank like the panel does
        if aggs:
            node["aggs"] = aggs
        aggs = {"b{}".format(k): node}
    body = {"size": 0, "track_total_hits": True, "timeout": "20s", "query": {"bool": {"filter": must}}}
    if aggs:
        body["aggs"] = aggs
    return body, buckets


def _metric_value(node, i, m):
    if m.get("op") == "count":
        return node.get("doc_count")
    v = node.get("m{}".format(i)) or {}
    if "values" in v:
        vals = list((v["values"] or {}).values())
        return vals[0] if vals else None
    return v.get("value")


def summarize_result(p, res, buckets):
    total = (res.get("hits") or {}).get("total")
    total = total.get("value") if isinstance(total, dict) else total
    if res.get("timed_out"):
        return "warn", "query timed out (partial results)"
    if not total:
        return "warn", "EMPTY -- 0 documents match in this time range"
    aggs = res.get("aggregations") or {}
    root = dict(aggs, doc_count=total)
    if not buckets:
        vals = ["{}={}".format(metric_label(m) or m.get("column"), fmt_num(_metric_value(root, i, m)))
                for i, m in enumerate(p.metrics)]
        bad = [m for i, m in enumerate(p.metrics) if _metric_value(root, i, m) is None]
        return ("warn" if bad else "ok"), ", ".join(vals) + ("  (null: field has no values)" if bad else "")
    b0 = (aggs.get("b0") or {}).get("buckets") or []
    if not b0:
        return "warn", "EMPTY -- {} docs but no buckets (field missing/unmapped?)".format(fmt_num(total))
    if buckets[0] == "time":
        desc = "{} time buckets".format(len(b0))
        if (aggs.get("b0") or {}).get("interval"):
            desc += " ({})".format(aggs["b0"]["interval"])
    else:
        tops = ", ".join("{}({})".format(clip(b.get("key_as_string", b.get("key")), 24),
                                         fmt_num(_metric_value(b, 0, p.metrics[0]) if p.metrics else b["doc_count"]))
                         for b in b0[:4])
        desc = "{} buckets: {}{}".format(len(b0), tops, "…" if len(b0) > 4 else "")
    if len(buckets) > 1:
        sub = set()
        for b in b0:
            for sb in (b.get("b1") or {}).get("buckets") or []:
                sub.add(sb.get("key_as_string", sb.get("key")))
        desc += "; {} series ({})".format(len(sub), ", ".join(clip(s, 16) for s in list(sub)[:4]))
        if p.chart.startswith("area") and len(sub) > 3:
            return "warn", desc + " -- >3 series on an area chart"
    if p.chart in ("pie", "donut") and len(b0) > 7:
        return "warn", desc + " -- pie with >7 slices; prefer a bar chart"
    return "ok", "{} docs, {}".format(fmt_num(total), desc)


def esql_substitute(query, time_range):
    start = date_math_to_iso(time_range["from"])
    end = date_math_to_iso(time_range.get("to", "now"))
    q = query.replace("?_tstart", 'TO_DATETIME("{}")'.format(start))
    return q.replace("?_tend", 'TO_DATETIME("{}")'.format(end))


def validate_spec(client, spec, ctx, time_range=None, out=print):
    """Run each panel's query; print one line per panel. Returns (#warn, #error)."""
    tr = time_range or spec.time_range or {"from": "now-15m", "to": "now"}
    n_warn = n_err = 0
    if not spec.time_range and not spec.standalone:
        out("note: no time_range -- the dashboard will open on the last 15 minutes; checking {}".format(
            tr["from"]))
    lines = []
    reqs = []
    for p in spec.panels():
        if p.chart == "markdown":
            continue
        if p.esql:
            q = esql_substitute(p.esql, tr)
            try:
                res = client.data("POST", "/_query?format=json", {"query": q})
            except ApiError as e:
                lines.append(("error", p.where, "ES|QL failed: " + str(e)))
                continue
            cols = [c["name"] for c in res.get("columns") or []]
            need = [m.get("column") for m in p.metrics if m.get("column")]
            if p.x:
                need.append(p.x)
            if p.split:
                need.append(p.split if isinstance(p.split, str) else p.split.get("column"))
            need += [b for b in p.by]
            missing = [c for c in need if c and c not in cols]
            vals = res.get("values") or []
            if missing:
                lines.append(("error", p.where, "columns {} not in query output {}".format(missing, cols)))
            elif not vals:
                lines.append(("warn", p.where, "EMPTY -- query returned no rows for {}".format(tr["from"])))
            else:
                first = dict(zip(cols, vals[0]))
                lines.append(("ok", p.where, "{} rows; first: {}".format(
                    len(vals), clip(", ".join("{}={}".format(k, fmt_num(v)) for k, v in first.items()), 110))))
            continue
        fields = ctx.f(p.index)
        if fields and p.time_field and p.time_field not in fields.caps:
            lines.append(("error", p.where, "time field {!r} not in {}".format(p.time_field, p.index)))
            continue
        body, buckets = panel_dsl(p, fields, tr)
        reqs.append((p, body, buckets))
    if reqs:
        nd = ""
        for p, body, _ in reqs:
            nd += json.dumps({"index": p.index, "ignore_unavailable": True}) + "\n" + json.dumps(body) + "\n"
        try:
            res = client.data("POST", "/_msearch", nd, ndjson=True)
            responses = res.get("responses") or []
        except ApiError:
            responses = []
            for p, body, _ in reqs:
                try:
                    responses.append(client.data("POST", "/{}/_search".format(p.index), body))
                except ApiError as e:
                    responses.append({"error": {"reason": str(e)}})
        for (p, body, buckets), r in zip(reqs, responses):
            if r.get("error"):
                lines.append(("error", p.where, "query failed: " + _err_text(json.dumps(r))))
                continue
            lvl, msg = summarize_result(p, r, buckets)
            lines.append((lvl, p.where, msg))
    order = {p.where: i for i, p in enumerate(spec.panels())}
    for lvl, w, msg in sorted(lines, key=lambda t: order.get(t[1], 0)):
        out("  {:5} {:28} {}".format(lvl.upper(), clip(w, 28), msg))
        n_warn += lvl == "warn"
        n_err += lvl == "error"
    return n_warn, n_err


# --- Remote lookups -----------------------------------------------------------

def list_index_patterns(client):
    if client.backend == "kibana":
        j = client.app("GET", "/api/data_views")
        return [{"id": d["id"], "title": d.get("title"), "time_field": d.get("timeFieldName"),
                 "name": d.get("name")} for d in j.get("data_view") or []]
    j = client.app("GET", "/api/saved_objects/_find", params={"type": "index-pattern", "per_page": 1000,
                                                              "fields": ["title", "timeFieldName"]})
    return [{"id": o["id"], "title": o["attributes"].get("title"),
             "time_field": o["attributes"].get("timeFieldName")} for o in j.get("saved_objects") or []]


def build_ctx(client, spec, need_fields=True):
    info = client.detect()
    ctx = Ctx(info["backend"], version_tuple(info.get("version")))
    ctx.version_str = info.get("version") or "3.0.0"
    pats = {}
    try:
        for ip in list_index_patterns(client):
            pats.setdefault(ip["title"], ip)
    except ApiError as e:
        note("warning: could not list index patterns: {}".format(e))
    ctx.patterns = pats
    for ix in spec.indices():
        if ix in pats:
            ctx.index_ids[ix] = pats[ix]["id"]
        if need_fields:
            try:
                ctx.fields[ix] = Fields.load(client, ix)
                if not ctx.fields[ix].caps:
                    spec.problems.append(("error", "index", "{!r} matches no indices or has no fields".format(ix)))
            except ApiError as e:
                spec.problems.append(("warn", "index", "field_caps for {!r} failed: {}".format(ix, e)))
    for p in spec.panels():
        if not p.time_field and p.index and not p.esql:
            ip = pats.get(p.index)
            p.time_field = (ip or {}).get("time_field") or guess_time_field(ctx.fields.get(p.index))
    return ctx


def offline_ctx(spec, backend, version):
    ctx = Ctx(backend, version_tuple(version))
    ctx.version_str = version
    for p in spec.panels():
        if not p.time_field and p.index:
            p.time_field = "@timestamp"
    return ctx


# --- Output helpers -----------------------------------------------------------

def print_problems(problems, show_info=True):
    seen = set()
    n = {"error": 0, "warn": 0, "info": 0}
    for lvl, where, msg in problems:
        if (lvl, where, msg) in seen or (lvl == "info" and not show_info):
            continue
        seen.add((lvl, where, msg))
        n[lvl] = n.get(lvl, 0) + 1
        print("  {:5} {:28} {}".format(lvl.upper(), clip(where, 28), msg))
    return n


def outline_kibana(body, out=print):
    out("title: {}".format(body.get("title")))
    if body.get("description"):
        out("description: {}".format(clip(body["description"], 160)))
    if body.get("time_range"):
        out("time_range: {from} -> {to}".format(**body["time_range"]))
    for c in body.get("pinned_panels") or []:
        conf = c.get("config") or {}
        out("control: {} {}{}".format(c.get("type"), conf.get("field_name") or conf.get("esql_query") or "",
                                      " ({})".format(conf.get("title")) if conf.get("title") else ""))

    def line(p, ind):
        g = p.get("grid") or {}
        cfg = p.get("config") or {}
        ct = cfg.get("type") if p.get("type") == "vis" else p.get("type")
        if p.get("type") == "vis" and cfg.get("ref_id"):
            ct = "ref:" + cfg["ref_id"]
        src = ""
        ds = cfg.get("data_source") or ((cfg.get("layers") or [{}])[0].get("data_source")) or {}
        if ds.get("query"):
            src = "esql: " + ds["query"]
        elif ds.get("index_pattern") or ds.get("ref_id"):
            src = ds.get("index_pattern") or "dv:" + ds.get("ref_id")
        if p.get("type") == "markdown":
            src = "md: " + (cfg.get("content") or "")
        out("{}{:22} {:16} {:>2}x{:<2} @{},{}  {}  {}".format(
            ind, clip(p.get("id", "?"), 22), ct or "?", g.get("w"), g.get("h"), g.get("x"), g.get("y"),
            clip(cfg.get("title") or "", 34), clip(src, 70)))

    for p in body.get("panels") or []:
        if "panels" in p and "config" not in p:
            out("section {!r} (y={}{})".format(p.get("title"), (p.get("grid") or {}).get("y"),
                                               ", collapsed" if p.get("collapsed") else ""))
            for q in p["panels"]:
                line(q, "  ")
        else:
            line(p, "")


def outline_osd(bundle, out=print):
    d = bundle.get("dashboard")
    vis = {v["id"]: (v if v.get("missing") else osd_decode(v)) for v in bundle.get("visualizations") or []}
    if not d:
        for v in vis.values():
            out("visualization {} {}".format(v["id"], v["attributes"].get("title")))
        return
    d = osd_decode(d)
    a = d["attributes"]
    out("title: {}".format(a.get("title")))
    if a.get("description"):
        out("description: {}".format(clip(a["description"], 160)))
    if a.get("timeRestore"):
        out("time_range: {} -> {}".format(a.get("timeFrom"), a.get("timeTo")))
    refs = {r["name"]: r for r in d.get("references") or []}
    for p in a.get("panelsJSON") or []:
        g = p.get("gridData") or {}
        r = panel_ref(refs, p) or {}
        v = vis.get(r.get("id"))
        desc, vt, title = "", r.get("type", "?"), p.get("title") or ""
        lens = (p.get("embeddableConfig") or {}).get("attributes") if p.get("type") == "lens" else None
        if lens is None and v and v.get("type") == "lens":
            lens = v.get("attributes") or {}
        if v and v.get("missing"):
            desc = "MISSING {} {}".format(r.get("type"), r.get("id"))
        elif lens is not None:
            vt = "lens:" + str(lens.get("visualizationType") or "?").replace("lns", "").lower()
            title = title or (p.get("embeddableConfig") or {}).get("title") or lens.get("title") or ""
            desc = "(Lens: not decoded; open it in Kibana to edit)"
        elif v:
            vs = v["attributes"].get("visState") or {}
            vt = vs.get("type")
            title = title or v["attributes"].get("title")
            parts = []
            for agg in vs.get("aggs") or []:
                pr = agg.get("params") or {}
                parts.append("{}{}".format(agg["type"], "(" + pr["field"] + ")" if pr.get("field") else ""))
            if vt == "input_control_vis":
                parts = [c.get("fieldName") for c in (vs.get("params") or {}).get("controls") or []]
            if vt == "markdown":
                parts = [clip((vs.get("params") or {}).get("markdown") or "", 50)]
            q = ((v["attributes"].get("kibanaSavedObjectMeta") or {}).get("searchSourceJSON") or {})
            q = (q.get("query") or {}).get("query") if isinstance(q, dict) else ""
            desc = " ".join(parts) + ("  filter: " + q if q else "")
        elif r.get("id"):
            desc = "(not loaded: {})".format(r.get("id"))
        out("{:4} {:18} {:>2}x{:<2} @{},{}  {}  {}".format(
            str(p.get("panelIndex")), vt or "?", g.get("w"), g.get("h"), g.get("x"), g.get("y"),
            clip(title, 34), clip(desc, 80)))


# --- Backups ------------------------------------------------------------------

def backup(client, kind, oid, payload):
    d = os.environ.get("KIBANA_BACKUP_DIR") or os.path.join(CACHE_DIR, "backups")
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    where = client.tenant or client.space or "default"
    path = os.path.join(d, "{}-{}-{}-{}-{}.json".format(client.backend, slug(where, 20), kind, slug(oid, 60), stamp))
    dump_json(payload, path)
    return path


# --- Remote fetch -------------------------------------------------------------

def fetch_kibana(client, kind, oid):
    path = "/api/dashboards/" if kind == "dashboard" else "/api/visualizations/"
    try:
        j = client.app("GET", path + urllib.parse.quote(oid, safe=""))
    except ApiError as e:
        if e.status == 404:
            return None
        raise
    body = j.get("data") or {}
    return dict({"id": j.get("id", oid)}, **body)


def fetch_osd_bundle(client, oid, kind="dashboard"):
    try:
        d = client.app("GET", "/api/saved_objects/{}/{}".format(kind, urllib.parse.quote(oid, safe="")))
    except ApiError as e:
        if e.status == 404:
            return None
        raise
    if kind != "dashboard":
        return {"kind": "osd_bundle", "dashboard": None, "visualizations": [osd_decode(d)]}
    refs = [r for r in d.get("references") or [] if r.get("type") in ("visualization", "search", "lens")]
    vis = []
    if refs:
        got = client.app("POST", "/api/saved_objects/_bulk_get",
                         [{"type": r["type"], "id": r["id"]} for r in refs])
        for o in got.get("saved_objects") or []:
            if o.get("error"):
                vis.append({"type": o.get("type"), "id": o.get("id"), "missing": True,
                            "error": o["error"].get("message")})
            else:
                vis.append(osd_decode(o))
    return {"kind": "osd_bundle", "dashboard": osd_decode(d), "visualizations": vis}


# --- Commands -----------------------------------------------------------------

def cmd_status(client, args):
    info = client.detect(refresh=True)
    v = version_tuple(info.get("version"))
    product = client.product
    print("backend:   {} {}{}".format("Kibana" if product == "kibana" else "OpenSearch Dashboards",
                                      info.get("version"),
                                      " (classic saved objects: Kibana < 9.4 has no Dashboards API)"
                                      if product == "kibana" and info["backend"] == "osd" else ""))
    if client.proxy_auth:
        print("auth:      proxy basic auth (KIBANA_NGINX_*) + session login as KIBANA_USERNAME")
    print("app url:   {}".format(info["base"]))
    print("data:      {} ({})".format(info["data"] if info["data_mode"] == "direct" else "console proxy",
                                      info["data_mode"]))
    if info.get("cluster_version"):
        print("cluster:   {} {}".format(info.get("cluster_distribution"), info["cluster_version"]))
    if info["backend"] == "kibana":
        print("space:     {}".format(client.space or "default"))
        if True:
            gates = ", ".join("{} {}".format(k, "on" if v >= g + (0,) else "off (needs {}.{})".format(*g))
                              for k, g in GATES.items())
            print("features:  {}".format(gates))
        print("queries:   ES|QL")
    elif product == "kibana":
        try:
            me = client.app("GET", "/internal/security/me")
            print("user:      {}  roles: {}".format(me.get("username"), ", ".join(me.get("roles") or [])))
        except ApiError:
            pass
        print("space:     {}".format(client.space or "default"))
        print("queries:   {}DSL".format("ES|QL, SQL, " if v >= (8, 11, 0) else "SQL, "))
    else:
        try:
            acct = client.data("GET", "/_plugins/_security/api/account") if info["data_mode"] == "direct" else {}
        except ApiError:
            acct = {}
        if acct:
            tenants = ", ".join(k for k, ok in (acct.get("tenants") or {}).items() if ok)
            print("user:      {}  roles: {}".format(acct.get("user_name"), ", ".join(acct.get("roles") or [])))
            print("tenants:   {}  (private tenant = 'private')".format(tenants))
        print("tenant:    {}".format({"__user__": "private"}.get(client.tenant, client.tenant) or
                                     "default (global unless the user prefers another)"))
        print("queries:   PPL, SQL, DSL (no ES|QL)")
    print("writes:    {}".format("DISABLED (KIBANA_READ_ONLY=1, the default)" if is_read_only() else "enabled"))
    return 0


def cmd_list(client, args):
    what = args.what
    if what in ("index-patterns", "data-views", "patterns"):
        for ip in list_index_patterns(client):
            print("{:40} {:40} time={}".format(ip["id"], clip(ip["title"], 40), ip.get("time_field")))
        return 0
    kind = "dashboard" if what.startswith("dash") else "visualization"
    if client.backend == "kibana":
        path = "/api/dashboards" if kind == "dashboard" else "/api/visualizations"
        params = {"per_page": args.limit}
        if args.search:
            params["query"] = args.search
        j = client.app("GET", path, params=params)
        items = j.get("dashboards") or j.get("data") or j.get("items") or []
        for it in items:
            data = it.get("data") or it.get("attributes") or it
            meta = it.get("meta") or {}
            print("{:40} {:50} {}".format(it.get("id"), clip(data.get("title", ""), 50),
                                          (meta.get("updated_at") or "")[:10]))
        total = (j.get("meta") or {}).get("total")
        if total is not None and total > len(items):
            note("{} of {} shown (--limit / --search)".format(len(items), total))
        return 0
    params = {"type": kind, "per_page": args.limit, "fields": ["title"], "sort_field": "updated_at"}
    if args.search:
        params["search"] = args.search + "*"
        params["search_fields"] = "title"
    j = client.app("GET", "/api/saved_objects/_find", params=params)
    for o in j.get("saved_objects") or []:
        print("{:44} {:50} {}".format(o["id"], clip(o["attributes"].get("title", ""), 50),
                                      (o.get("updated_at") or "")[:10]))
    if (j.get("total") or 0) > len(j.get("saved_objects") or []):
        note("{} of {} shown (--limit / --search)".format(len(j.get("saved_objects") or []), j.get("total")))
    return 0


def cmd_get(client, args):
    kind = "visualization" if args.vis else "dashboard"
    if client.backend == "kibana":
        obj = fetch_kibana(client, kind, args.id)
        if obj is None:
            raise ApiError("{} {!r} not found{}".format(kind, args.id, " in space " + client.space if client.space else ""), 404)
        if args.output:
            dump_json(obj, args.output)
            note("saved write-ready body to {} (the top-level 'id' is used and stripped by apply)".format(args.output))
        if args.json:
            dump_json(obj)
        elif kind == "dashboard":
            outline_kibana(obj)
        else:
            print(json.dumps(obj, indent=2, ensure_ascii=False)[:4000])
    else:
        b = fetch_osd_bundle(client, args.id, kind)
        if b is None:
            raise ApiError("{} {!r} not found in {}".format(kind, args.id, client.scope()), 404)
        if args.output:
            dump_json(b, args.output)
            note("saved decoded bundle ({} visualizations) to {} -- edit and `apply` it".format(
                len(b["visualizations"]), args.output))
        if args.json:
            dump_json(b)
        else:
            outline_osd(b)
            missing = [v["id"] for v in b["visualizations"] if v.get("missing")]
            if missing:
                print("MISSING referenced objects: " + ", ".join(missing))
    if not args.json:
        print("url: " + client.view_url(kind, args.id))
    return 0


def cmd_discover(client, args):
    pattern = args.index
    fields = Fields.load(client, pattern)
    if not fields.caps:
        raise ApiError("{!r} matches no indices (or no mapped fields)".format(pattern))
    pats = []
    try:
        pats = [ip for ip in list_index_patterns(client) if ip["title"] == pattern or
                re.fullmatch(re.escape(ip["title"]).replace(r"\*", ".*"), pattern) or
                re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), ip["title"] or "")]
    except ApiError:
        pass
    tf = args.time_field or next((ip["time_field"] for ip in pats if ip.get("time_field")), None) or \
        guess_time_field(fields)
    window = since_to_range(args.since)
    flt = re.compile(args.fields) if args.fields else None
    names = sorted(n for n in fields.caps if not flt or flt.search(n))
    kw = [n for n in names if fields.caps[n]["type"] in ("keyword", "constant_keyword", "ip", "boolean")
          and fields.caps[n]["aggregatable"]]
    num = [n for n in names if fields.is_numeric(n) and fields.caps[n]["aggregatable"]]
    dates = [n for n in names if fields.caps[n]["type"] in ("date", "date_nanos")]
    texts = [n for n in names if fields.caps[n]["type"] in ("text", "match_only_text")]
    other = [n for n in names if n not in set(kw) | set(num) | set(dates) | set(texts)
             and fields.caps[n]["type"] not in ("object", "nested")]
    kw_probe = [n for n in kw if fields.caps[n]["type"] != "boolean"][: args.max_fields]
    num_probe = num[: args.max_fields]
    head = json.dumps({"index": pattern, "ignore_unavailable": True})
    span_body = {"size": 0, "track_total_hits": True, "timeout": "20s",
                 "aggs": {"tmin": {"min": {"field": tf}}, "tmax": {"max": {"field": tf}}}}
    wa = {}
    for i, n in enumerate(kw_probe):
        wa["c{}".format(i)] = {"cardinality": {"field": n, "precision_threshold": 1000}}
        wa["t{}".format(i)] = {"terms": {"field": n, "size": 5}}
    for i, n in enumerate(num_probe):
        wa["s{}".format(i)] = {"stats": {"field": n}}
        wa["n{}".format(i)] = {"cardinality": {"field": n, "precision_threshold": 100}}
    win_body = {"size": 0, "track_total_hits": True, "timeout": "30s",
                "query": {"range": {tf: {"gte": window["from"], "lte": "now"}}}, "aggs": wa}
    nd = head + "\n" + json.dumps(span_body) + "\n" + head + "\n" + json.dumps(win_body) + "\n"
    res = client.data("POST", "/_msearch", nd, ndjson=True).get("responses") or [{}, {}]
    span, win = res[0], res[1] if len(res) > 1 else {}
    for r in (span, win):
        if r.get("error"):
            raise ApiError("discover query failed: " + _err_text(json.dumps(r)))

    def tot(r):
        t = (r.get("hits") or {}).get("total")
        return t.get("value") if isinstance(t, dict) else t

    sa = span.get("aggregations") or {}
    aa = win.get("aggregations") or {}
    report = {"index": pattern, "indices": len(getattr(fields, "indices", [])), "time_field": tf,
              "docs_total": tot(span), "docs_in_window": tot(win), "window": window["from"],
              "span": [sa.get("tmin", {}).get("value_as_string"), sa.get("tmax", {}).get("value_as_string")],
              "index_patterns": pats, "keyword": [], "numeric": [], "date": dates, "text": [], "other": other}
    for i, n in enumerate(kw_probe):
        card = (aa.get("c{}".format(i)) or {}).get("value")
        top = [(b.get("key_as_string", b["key"]), b["doc_count"]) for b in (aa.get("t{}".format(i)) or {}).get("buckets") or []]
        report["keyword"].append({"field": n, "cardinality": card, "top": top})
    for n in kw:
        if n not in kw_probe:
            report["keyword"].append({"field": n, "cardinality": None, "top": []})
    for i, n in enumerate(num_probe):
        st = aa.get("s{}".format(i)) or {}
        fmt = guess_format("sum", n)
        card = (aa.get("n{}".format(i)) or {}).get("value")
        role = "measure"
        if not st.get("count"):
            role = "empty"
        elif ID_LIKE.search(n) or re.search(r"(?i)(^|_)(id|number|no)$", n):
            role = "id"
        elif card is not None and card <= 50 and fields.caps[n]["type"] in ("long", "integer", "short", "byte"):
            role = "category"
        report["numeric"].append({"field": n, "type": fields.caps[n]["type"], "min": st.get("min"),
                                  "max": st.get("max"), "avg": st.get("avg"), "count": st.get("count"),
                                  "cardinality": card, "role": role, "format": fmt})
    for n in num:
        if n not in num_probe:
            report["numeric"].append({"field": n, "type": fields.caps[n]["type"]})
    for n in texts:
        k = n + ".keyword"
        report["text"].append({"field": n, "keyword": k if k in fields.caps else None})
    ctrl = [k for k in report["keyword"] if k["cardinality"] and 2 <= k["cardinality"] <= 200
            and not ID_LIKE.search(k["field"])]
    ctrl += [{"field": n["field"], "cardinality": n["cardinality"]} for n in report["numeric"]
             if n.get("role") == "category" and (n["cardinality"] or 0) >= 2]
    ctrl.sort(key=lambda k: k["cardinality"])
    report["suggested_controls"] = [k["field"] for k in ctrl[:5]]
    mets = ["count"]
    for nm in report["numeric"]:
        if nm.get("role") == "measure":
            fmt = (nm.get("format") or {}).get("type")
            mets.append(("avg:" if fmt == "duration" else "sum:") + nm["field"])
            if fmt != "duration":
                mets.append("avg:" + nm["field"])
    report["suggested_metrics"] = mets[:8]
    if args.json:
        dump_json(report)
        return 0
    print("index {}  ({} backing indices)  time field: {}".format(pattern, report["indices"], tf))
    print("docs: {} total, {} in last {}   span: {} -> {}".format(
        fmt_num(report["docs_total"]), fmt_num(report["docs_in_window"]), args.since,
        (report["span"][0] or "?")[:19], (report["span"][1] or "?")[:19]))
    if report["span"][1]:
        try:
            last = _dt.datetime.fromisoformat(report["span"][1][:19])
            age = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - last
            if age.total_seconds() < -600:
                print("NOTE: newest document is {:.1f}h in the FUTURE -- timestamps are probably local time stored "
                      "as UTC; 'last N hours' panels will lag by that offset".format(-age.total_seconds() / 3600))
            if age.days >= 1:
                print("NOTE: newest document is {} days old -- persist a time_range that covers the data".format(age.days))
        except ValueError:
            pass
    if pats:
        print("index patterns: " + "; ".join("{} [{}] time={}".format(p["title"], p["id"], p.get("time_field"))
                                             for p in pats))
    else:
        print("index patterns: none (apply will create one on OSD / a data view for Kibana controls)")
    print("\nkeyword fields (distinct values in window, top values):")
    for k in report["keyword"]:
        if k["cardinality"] is None:
            continue
        tops = ", ".join("{}({})".format(clip(v, 20), fmt_num(c)) for v, c in k["top"][:4])
        print("  {:38} {:>7}  {}".format(clip(k["field"], 38), fmt_num(k["cardinality"]), tops))
    rest = [k["field"] for k in report["keyword"] if k["cardinality"] is None]
    if rest:
        print("  (+{} more not probed: {})".format(len(rest), clip(", ".join(rest), 200)))
    print("\nnumeric fields (min / avg / max in window, distinct, role):")
    for nm in report["numeric"]:
        if "min" not in nm:
            continue
        if nm["role"] == "empty":
            print("  {:38} {:8} no values in window".format(clip(nm["field"], 38), nm["type"]))
            continue
        print("  {:38} {:8} {:>9} / {:>9} / {:>9} {:>7} {}{}".format(
            clip(nm["field"], 38), nm["type"], fmt_num(nm["min"]), fmt_num(nm["avg"]), fmt_num(nm["max"]),
            fmt_num(nm["cardinality"]), nm["role"],
            "  format: " + json.dumps(nm["format"]) if nm.get("format") else ""))
    rest = [n["field"] for n in report["numeric"] if "min" not in n]
    if rest:
        print("  (+{} more not probed: {})".format(len(rest), clip(", ".join(rest), 200)))
    if dates:
        print("\ndate fields: " + ", ".join(dates))
    if texts:
        print("text fields: " + ", ".join("{}{}".format(t["field"], " (agg via .keyword)" if t["keyword"] else
                                                         " (not aggregatable)") for t in report["text"]))
    if other:
        print("other: " + clip(", ".join("{}:{}".format(n, fields.caps[n]["type"]) for n in other), 300))
    print("\nsuggested controls: " + (", ".join(report["suggested_controls"]) or "none"))
    print("suggested metrics:  " + ", ".join(report["suggested_metrics"]))
    return 0


def cmd_query(client, args):
    q = args.query if args.query != "-" else sys.stdin.read()
    lang = args.lang
    if lang == "auto":
        if client.backend == "kibana" or (client.product == "kibana" and client.version >= (8, 11, 0)):
            lang = "esql"
        elif client.product == "kibana":
            lang = "sql"
        else:
            lang = "sql" if re.match(r"(?i)^\s*(select|show|describe)\b", q) else "ppl"
    tr = since_to_range(args.since)
    if lang == "esql":
        res = client.data("POST", "/_query?format=json", {"query": esql_substitute(q, tr)})
        cols = [c["name"] for c in res.get("columns") or []]
        rows = res.get("values") or []
    elif lang == "sql" and client.product == "kibana":
        res = client.data("POST", "/_sql?format=json", {"query": q})
        cols = [c["name"] for c in res.get("columns") or []]
        rows = res.get("rows") or []
    elif lang in ("ppl", "sql"):
        res = client.data("POST", "/_plugins/_{}".format(lang), {"query": q})
        cols = [c.get("alias") or c["name"] for c in res.get("schema") or []]
        rows = res.get("datarows") or []
    else:
        res = client.data("POST", "/{}/_search".format(args.index or "*"), json.loads(q))
        dump_json(res if args.full else {k: res.get(k) for k in ("hits", "aggregations") if k in res})
        return 0
    if args.format == "json":
        dump_json([dict(zip(cols, r)) for r in rows[: args.limit]])
    else:
        print("\t".join(cols))
        for r in rows[: args.limit]:
            print("\t".join("" if v is None else str(v) for v in r))
    if len(rows) > args.limit:
        note("{} rows; showing {} (--limit)".format(len(rows), args.limit))
    return 0


def load_input(path):
    doc = load_json(path)
    if is_osd_bundle(doc):
        return "osd_bundle", doc
    if is_spec(doc):
        return "spec", doc
    if isinstance(doc, dict) and doc.get("kind") in ("kibana_dashboard", "kibana_visualization"):
        return doc["kind"], doc
    if isinstance(doc, dict) and ("panels" in doc or "pinned_panels" in doc):
        return "kibana_dashboard", {"kind": "kibana_dashboard", "id": doc.get("id"),
                                    "body": {k: v for k, v in doc.items() if k not in ("id", "spaces", "meta")}}
    if isinstance(doc, dict) and doc.get("type") in ALL_CHARTS | {"xy", "data_table"}:
        return "kibana_visualization", {"kind": "kibana_visualization", "id": doc.get("id"),
                                        "body": {k: v for k, v in doc.items() if k not in ("id", "meta")}}
    raise UsageError("could not tell what {} is: expected a spec (panels with 'chart'), a Kibana dashboard "
                     "body (panels with 'grid'/'config'), or an OSD bundle (from `get -o`)".format(path))


def compile_input(client, path, args, need_live=True):
    """-> (compiled dict, problems, ctx, spec|None)"""
    kind, doc = load_input(path)
    if kind == "spec":
        if getattr(args, "id", None):
            doc["id"] = args.id
        spec = Spec(doc)
        if client is not None and need_live:
            ctx = build_ctx(client, spec)
        else:
            backend = getattr(args, "target", None) or "kibana"
            ctx = offline_ctx(spec, backend, getattr(args, "version", None) or ("9.6.0" if backend == "kibana" else "3.0.0"))
        if ctx.backend == "kibana":
            compiled, problems = compile_kibana(spec, ctx)
        else:
            compiled, problems = compile_osd(spec, ctx)
        problems = list(spec.problems) + [p for p in problems if p not in spec.problems]
        problems += [("info", "compile", n) for n in ctx.notes]
        return compiled, problems, ctx, spec
    problems = []
    if getattr(args, "id", None):
        doc["id"] = args.id
    ctx = None
    if client is not None and need_live:
        info = client.detect()
        want = "osd" if kind == "osd_bundle" else "kibana"
        if info["backend"] != want:
            raise UsageError("{} is a {} object but KIBANA_URL is {}".format(path, want, info["backend"]))
        ctx = Ctx(info["backend"], version_tuple(info.get("version")))
    if kind == "osd_bundle":
        doc = dict(doc, kind="osd_bundle")
        lint_osd_bundle(doc, problems)
    elif kind == "kibana_dashboard":
        lint_kibana_body(doc["body"], problems, ctx.version if ctx else None)
    else:
        lint_kibana_body(doc["body"], problems, ctx.version if ctx else None, kind="visualization")
    return doc, problems, ctx, None


def _summary_of(compiled):
    if compiled["kind"] == "kibana_dashboard":
        outline_kibana(compiled["body"])
    elif compiled["kind"] == "osd_bundle":
        outline_osd(compiled)
    else:
        print(json.dumps(compiled["body"], indent=2, ensure_ascii=False)[:3000])


def cmd_build(client, args):
    compiled, problems, ctx, spec = compile_input(client if not args.offline else None, args.file, args,
                                                  need_live=not args.offline)
    if compiled["kind"] == "kibana_dashboard" and spec is not None:
        lint_kibana_body(compiled["body"], problems, ctx.version if ctx else None)
    elif compiled["kind"] == "osd_bundle" and spec is not None:
        lint_osd_bundle(compiled, problems)
    out = args.output
    payload = compiled
    if compiled["kind"].startswith("kibana"):
        payload = dict({"id": compiled.get("id")}, **compiled["body"]) if compiled.get("id") else compiled["body"]
    if out:
        dump_json(payload, out)
        note("wrote {} ({})".format(out, compiled["kind"]))
    elif args.json:
        dump_json(payload)
    if not args.json:
        _summary_of(compiled)
        n = print_problems(problems, not args.quiet)
        print("lint: {error} error(s), {warn} warning(s)".format(**n))
    return 1 if any(p[0] == "error" for p in problems) else 0


def cmd_lint(client, args):
    args.offline = client is None
    args.output, args.json = None, False
    compiled, problems, ctx, spec = compile_input(client, args.file, args, need_live=client is not None)
    if spec is not None:
        if compiled["kind"] == "kibana_dashboard":
            lint_kibana_body(compiled["body"], problems, ctx.version if ctx else None)
        elif compiled["kind"] == "osd_bundle":
            lint_osd_bundle(compiled, problems)
    n = print_problems(problems, not args.quiet)
    print("lint: {error} error(s), {warn} warning(s), {info} note(s)".format(**n))
    return 1 if n["error"] else 0


def cmd_layout(client, args):
    """Reflow grid coordinates of a Kibana body or OSD bundle in place (sizes kept)."""
    kind, doc = load_input(args.file)
    if kind == "spec":
        raise UsageError("specs are laid out automatically by build/apply; layout is for native bodies")
    raw = load_json(args.file)
    if kind == "kibana_dashboard":
        def chart(p):
            return (p.get("config") or {}).get("type") if p.get("type") == "vis" else p.get("type")
        top = [p for p in raw.get("panels") or [] if not ("panels" in p and "config" not in p)]
        new = reflow_grids([(p["grid"], chart(p)) for p in top])
        for p, g in zip(top, new):
            p["grid"] = g
        y = max([g["y"] + g["h"] for g in new] + [0])
        for s in [p for p in raw.get("panels") or [] if "panels" in p and "config" not in p]:
            sp = s["panels"]
            for p, g in zip(sp, reflow_grids([(p["grid"], chart(p)) for p in sp])):
                p["grid"] = g
            s["grid"] = {"y": y}
            y += 1
    else:
        d = raw["dashboard"]["attributes"]
        pj = json.loads(d["panelsJSON"]) if isinstance(d["panelsJSON"], str) else d["panelsJSON"]
        new = reflow_grids([(p["gridData"], None) for p in pj])
        for p, g in zip(pj, new):
            g["i"] = p["gridData"].get("i", p.get("panelIndex"))
            p["gridData"] = g
        d["panelsJSON"] = pj
    dump_json(raw, args.output or args.file)
    note("reflowed {} -> {}".format(args.file, args.output or args.file))
    return 0


_OSD_METRIC = {"avg": "avg", "sum": "sum", "min": "min", "max": "max", "cardinality": "cardinality",
               "value_count": "value_count"}


def validate_osd_bundle(client, bundle, time_range=None, out=print):
    """Run every saved visualization's aggs (one _msearch) and report what each panel shows."""
    d = bundle.get("dashboard")
    a = (d or {}).get("attributes") or {}
    tr = time_range or ({"from": a["timeFrom"], "to": a.get("timeTo", "now")} if a.get("timeRestore") else
                        {"from": "now-15m", "to": "now"})
    if not time_range and not a.get("timeRestore"):
        out("note: dashboard has no stored time range -- checking the default last 15 minutes")
    vis = [v for v in bundle.get("visualizations") or [] if not v.get("missing")]
    ip_ids = {r["id"] for v in vis for r in v.get("references") or []
              if r.get("name") == "kibanaSavedObjectMeta.searchSourceJSON.index"}
    ips = {}
    if ip_ids:
        got = client.app("POST", "/api/saved_objects/_bulk_get", [{"type": "index-pattern", "id": i} for i in ip_ids])
        for o in got.get("saved_objects") or []:
            if not o.get("error"):
                ips[o["id"]] = o["attributes"]
    reqs, lines = [], []
    for v in vis:
        v = osd_decode(v)
        va = v["attributes"]
        vs = va.get("visState") or {}
        name = va.get("title") or v["id"]
        if vs.get("type") in ("markdown", "input_control_vis") or not vs.get("aggs"):
            continue
        ref = next((r["id"] for r in v.get("references") or []
                    if r.get("name") == "kibanaSavedObjectMeta.searchSourceJSON.index"), None)
        ip = ips.get(ref)
        if not ip:
            lines.append(("error", name, "index pattern {} not found in this {} (deleted?)".format(
                ref, "space" if client.product == "kibana" else "tenant")))
            continue
        tf = ip.get("timeFieldName")
        must = [{"range": {tf: {"gte": tr["from"], "lte": tr.get("to", "now")}}}] if tf else []
        ssj = ((va.get("kibanaSavedObjectMeta") or {}).get("searchSourceJSON") or {})
        q = ((ssj.get("query") or {}).get("query") or "") if isinstance(ssj, dict) else ""
        if q:
            must.append({"query_string": {"query": _kql_to_query_string(q), "analyze_wildcard": True}})
        metrics, buckets, skipped = {}, [], []
        for ag in vs["aggs"]:
            if ag.get("enabled") is False:
                continue
            t, pr = ag["type"], ag.get("params") or {}
            if ag.get("schema") == "metric":
                if t == "count":
                    continue
                if t in _OSD_METRIC:
                    metrics["m" + ag["id"]] = {_OSD_METRIC[t]: {"field": pr.get("field")}}
                elif t in ("median", "percentiles"):
                    metrics["m" + ag["id"]] = {"percentiles": {"field": pr.get("field"),
                                                               "percents": pr.get("percents") or [50]}}
                else:
                    skipped.append(t)
            elif t == "date_histogram":
                buckets.append(("time", {"auto_date_histogram": {"field": pr.get("field") or tf, "buckets": 20}}))
            elif t == "terms":
                buckets.append((pr.get("field"), {"terms": {"field": pr.get("field"), "size": int(pr.get("size", 5))}}))
            else:
                skipped.append(t)
        aggs = metrics
        for k, (_, node) in reversed(list(enumerate(buckets))):
            node = dict(node)
            if aggs:
                node["aggs"] = aggs
            aggs = {"b{}".format(k): node}
        body = {"size": 0, "track_total_hits": True, "timeout": "20s", "query": {"bool": {"filter": must}}}
        if aggs:
            body["aggs"] = aggs
        reqs.append((name, ip["title"], body, buckets, list(metrics), skipped))
    if reqs:
        nd = "".join(json.dumps({"index": t, "ignore_unavailable": True}) + "\n" + json.dumps(b) + "\n"
                     for _, t, b, _, _, _ in reqs)
        resp = client.data("POST", "/_msearch", nd, ndjson=True).get("responses") or []
        for (name, _, _, buckets, mkeys, skipped), r in zip(reqs, resp):
            if r.get("error"):
                lines.append(("error", name, "query failed: " + _err_text(json.dumps(r))))
                continue
            total = (r.get("hits") or {}).get("total")
            total = total.get("value") if isinstance(total, dict) else total
            ag = r.get("aggregations") or {}
            if not total:
                lines.append(("warn", name, "EMPTY -- 0 documents in {}".format(tr["from"])))
                continue

            def mval(node, k):
                x = node.get(k) or {}
                if "values" in x:
                    vals = list((x["values"] or {}).values())
                    return vals[0] if vals else None
                return x.get("value")

            if not buckets:
                vals = ["count={}".format(fmt_num(total))] + ["{}={}".format(k[1:], fmt_num(mval(ag, k))) for k in mkeys]
                lines.append(("ok", name, ", ".join(vals)))
            else:
                b0 = (ag.get("b0") or {}).get("buckets") or []
                if not b0:
                    lines.append(("warn", name, "EMPTY -- {} docs but no buckets".format(fmt_num(total))))
                    continue
                if buckets[0][0] == "time":
                    desc = "{} time buckets ({})".format(len(b0), (ag.get("b0") or {}).get("interval", "auto"))
                else:
                    desc = "{} buckets: {}".format(len(b0), ", ".join(
                        "{}({})".format(clip(b.get("key_as_string", b.get("key")), 22), fmt_num(b["doc_count"]))
                        for b in b0[:4]))
                if len(buckets) > 1:
                    series = {sb.get("key") for b in b0 for sb in (b.get("b1") or {}).get("buckets") or []}
                    desc += "; {} series".format(len(series))
                lines.append(("ok", name, "{} docs, {}".format(fmt_num(total), desc)))
            if skipped:
                lines.append(("info", name, "not checked: {}".format(", ".join(skipped))))
    n = {"ok": 0, "warn": 0, "error": 0, "info": 0}
    for lvl, w, msg in lines:
        out("  {:5} {:34} {}".format(lvl.upper(), clip(w, 34), msg))
        n[lvl] += 1
    return n["warn"], n["error"]


def cmd_validate(client, args):
    if args.id:
        if client.backend != "osd":
            raise UsageError("validate --id is for OpenSearch dashboards; on Kibana use `get -o` then validate the body")
        b = fetch_osd_bundle(client, args.id)
        if b is None:
            raise ApiError("dashboard {!r} not found in {}".format(args.id, client.scope()), 404)
        w, e = validate_osd_bundle(client, b, since_to_range(args.since) if args.since else None)
        print("validate: {} error(s), {} warning(s)".format(e, w))
        return 1 if e else 0
    if not args.file:
        raise UsageError("validate needs a file or --id")
    kind, doc = load_input(args.file)
    tr = since_to_range(args.since) if args.since else None
    if kind == "osd_bundle":
        w, e = validate_osd_bundle(client, doc, tr)
        print("validate: {} error(s), {} warning(s)".format(e, w))
        return 1 if e else 0
    if kind == "spec":
        spec = Spec(doc)
        ctx = build_ctx(client, spec)
        bad = [p for p in spec.problems if p[0] == "error"]
        if bad:
            print_problems(bad)
        w, e = validate_spec(client, spec, ctx, tr)
        print("validate: {} error(s), {} warning(s)".format(e + len(bad), w))
        return 1 if e or bad else 0
    if kind == "kibana_dashboard":
        # Native body: run ES|QL panels; data-view panels are schema-checked by lint only.
        tr = tr or doc["body"].get("time_range") or {"from": "now-15m", "to": "now"}
        n_err = 0
        for p in iter_kibana_panels(doc["body"]):
            cfg = p.get("config") or {}
            layers = cfg.get("layers") or [cfg]
            for l in layers:
                ds = l.get("data_source") or cfg.get("data_source") or {}
                if ds.get("type") != "esql":
                    continue
                try:
                    res = client.data("POST", "/_query?format=json", {"query": esql_substitute(ds["query"], tr)})
                except ApiError as e:
                    print("  ERROR {:28} {}".format(clip(p.get("id"), 28), e))
                    n_err += 1
                    continue
                cols = {c["name"] for c in res.get("columns") or []}
                refs = [o["column"] for o in _walk(l) if isinstance(o.get("column"), str)]
                missing = [c for c in refs if c not in cols]
                rows = len(res.get("values") or [])
                lvl = "ERROR" if missing else ("WARN" if not rows else "OK")
                n_err += bool(missing)
                print("  {:5} {:28} {}".format(lvl, clip(p.get("id"), 28),
                                               "missing columns {}".format(missing) if missing else "{} rows".format(rows)))
        return 1 if n_err else 0
    raise UsageError("validate takes a spec or a Kibana dashboard body")


def iter_kibana_panels(body):
    for p in body.get("panels") or []:
        if "panels" in p and "config" not in p:
            for q in p["panels"]:
                yield q
        else:
            yield p


def _resolve_kibana_placeholders(client, body, created):
    text = json.dumps(body)
    pats = set(re.findall(r"\{\{data_view:([^}]+)\}\}", text))
    if not pats:
        return body
    existing = {ip["title"]: ip for ip in list_index_patterns(client)}
    for pat in pats:
        if pat in existing:
            dv = existing[pat]["id"]
        else:
            require_writes("create a data view for {!r}".format(pat))
            j = client.app("POST", "/api/data_views/data_view",
                           {"data_view": {"title": pat, "name": pat, "timeFieldName": "@timestamp"}})
            dv = (j.get("data_view") or {}).get("id")
            created.append("data view {} [{}]".format(pat, dv))
        text = text.replace("{{data_view:%s}}" % pat, dv)
    return json.loads(text)


def osd_pattern_fields(client, pattern):
    """The cached field list OSD expects on an index-pattern (built by OSD itself)."""
    j = client.app("GET", "/api/index_patterns/_fields_for_wildcard",
                   params={"pattern": pattern, "meta_fields": ["_source", "_id", "_index", "_score"]})
    out = []
    for f in j.get("fields") or []:
        out.append(dict({"count": 0, "scripted": False}, **f))
    return out


def osd_check_patterns(client, bundle, repair, created, plan=None):
    """Make sure every referenced index pattern has a field list that covers the fields the panels use.
    OSD renders 'Could not locate that index-pattern-field' otherwise. Returns warnings."""
    used = {}  # pattern id -> set(fields)
    for v in bundle.get("visualizations") or []:
        vs = (v.get("attributes") or {}).get("visState") or {}
        vs = json.loads(vs) if isinstance(vs, str) else vs
        refs = {r["name"]: r["id"] for r in v.get("references") or [] if r.get("type") == "index-pattern"}
        main = refs.get("kibanaSavedObjectMeta.searchSourceJSON.index")
        for a in vs.get("aggs") or []:
            for key in ("field", "sortField"):
                f = (a.get("params") or {}).get(key)
                if f and main:
                    used.setdefault(main, set()).add(f if isinstance(f, str) else f.get("name"))
        for c in (vs.get("params") or {}).get("controls") or []:
            rid = refs.get(c.get("indexPatternRefName"))
            if rid and c.get("fieldName"):
                used.setdefault(rid, set()).add(c["fieldName"])
    if not used:
        return []
    got = client.app("POST", "/api/saved_objects/_bulk_get", [{"type": "index-pattern", "id": i} for i in used])
    warnings = []
    for o in got.get("saved_objects") or []:
        if o.get("error"):
            warnings.append("index pattern {} is not visible in this {} -- its panels will error".format(
                o["id"], client.scope()))
            continue
        a = o["attributes"]
        if client.product == "kibana":
            # Kibana 7.11+ stores an empty field list and loads fields at runtime: check the live
            # mapping, never rewrite the pattern.
            names = {f.get("name") for f in osd_pattern_fields(client, a["title"])}
            missing = sorted(used[o["id"]] - names)
            if missing:
                warnings.append("index {} has no field(s) {} -- those panels will error".format(
                    a.get("title"), ", ".join(missing)))
            continue
        try:
            fields = json.loads(a.get("fields") or "[]")
        except ValueError:
            fields = []
        names = {f.get("name") for f in fields}
        missing = sorted(used[o["id"]] - names)
        if missing and plan is not None:
            plan.append("refresh the field list of existing index pattern {} [{}] (missing: {}); "
                        "it is backed up first".format(a.get("title"), o["id"], ", ".join(missing[:6])))
            continue
        if missing and repair:
            print("backup: " + backup(client, "index-pattern", o["id"], osd_decode(o)))
            fresh = osd_pattern_fields(client, a["title"])
            client.app("PUT", "/api/saved_objects/index-pattern/" + o["id"],
                       {"attributes": {"fields": json.dumps(fresh, ensure_ascii=False)}})
            created.append("refreshed field list of index pattern {} ({} fields)".format(a["title"], len(fresh)))
            names = {f.get("name") for f in fresh}
            missing = sorted(used[o["id"]] - names)
        if missing:
            warnings.append("index pattern {} has no field(s) {} -- those panels will error".format(
                a.get("title"), ", ".join(missing)))
        tf = a.get("timeFieldName")
        if tf and tf not in names:
            warnings.append("index pattern {} time field {} is not in its field list".format(a.get("title"), tf))
    return warnings


def _resolve_osd_placeholders(client, bundle, ctx, created):
    text = json.dumps(bundle)
    pats = set(re.findall(r"\{\{index_pattern:([^}]+)\}\}", text))
    for pat in pats:
        ip = (getattr(ctx, "patterns", {}) or {}).get(pat)
        if ip:
            iid = ip["id"]
        else:
            require_writes("create index pattern {!r}".format(pat))
            fields = ctx.f(pat) if ctx else Fields()
            tf = guess_time_field(fields)
            attrs = {"title": pat, "timeFieldName": tf}
            if client.product != "kibana":  # Kibana loads fields at runtime; OSD needs the cached list
                attrs["fields"] = json.dumps(osd_pattern_fields(client, pat), ensure_ascii=False)
            j = client.app("POST", "/api/saved_objects/index-pattern", {"attributes": attrs})
            iid = j["id"]
            created.append("index pattern {} [{}] time={}".format(pat, iid, tf))
        text = text.replace("{{index_pattern:%s}}" % pat, iid)
    return json.loads(text)


def _diff_summary(old, new, limit=40):
    a = json.dumps(old, indent=1, sort_keys=True, ensure_ascii=False).splitlines()
    b = json.dumps(new, indent=1, sort_keys=True, ensure_ascii=False).splitlines()
    d = [l for l in difflib.unified_diff(a, b, "remote", "local", n=1, lineterm="")][2:]
    if not d:
        return "no changes"
    out = "\n".join(d[:limit])
    if len(d) > limit:
        out += "\n… {} more diff lines".format(len(d) - limit)
    return out


def _normalize_kibana_for_diff(body):
    b = copy.deepcopy(body)
    b.pop("id", None)
    return b


def cmd_apply(client, args):
    compiled, problems, ctx, spec = compile_input(client, args.file, args)
    if spec is not None:
        if compiled["kind"] == "kibana_dashboard":
            lint_kibana_body(compiled["body"], problems, ctx.version)
        elif compiled["kind"] == "osd_bundle":
            lint_osd_bundle(compiled, problems)
    print("== lint")
    n = print_problems(problems, show_info=not args.quiet)
    if n["error"] and not args.force:
        print("aborted: {} lint error(s) (fix them, or --force)".format(n["error"]))
        return 1
    if spec is not None and not args.no_validate:
        print("== validate (data check)")
        w, e = validate_spec(client, spec, ctx, since_to_range(args.since) if args.since else None)
        if e and not args.force:
            print("aborted: {} panel(s) failed against the data (fix, or --force)".format(e))
            return 1
    if args.output:
        payload = compiled if compiled["kind"] == "osd_bundle" else dict({"id": compiled.get("id")}, **compiled["body"])
        dump_json(payload, args.output)
        note("compiled output saved to " + args.output)
    if compiled["kind"] == "osd_bundle":
        return _apply_osd(client, compiled, ctx, args)
    return _apply_kibana(client, compiled, args)


def _apply_kibana(client, compiled, args):
    kind = "dashboard" if compiled["kind"] == "kibana_dashboard" else "visualization"
    oid = compiled.get("id")
    body = compiled["body"]
    if client.version < (9, 4, 0):
        raise UsageError("Kibana {} has no {} API (needs 9.4+).".format(client.detect().get("version"), kind))
    current = fetch_kibana(client, kind, oid) if oid else None
    print("== plan")
    print("{} {} {!r} -> {}{}".format("update" if current else "create", kind, body.get("title") or oid,
                                      oid or "(auto id)", " in space " + client.space if client.space else ""))
    if current:
        print(_diff_summary(_normalize_kibana_for_diff(current), body))
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    require_writes("write {} {}".format(kind, oid or "(new)"))
    created = []
    body = _resolve_kibana_placeholders(client, body, created)
    if current:
        print("backup: " + backup(client, kind, oid, current))
    path = "/api/dashboards" if kind == "dashboard" else "/api/visualizations"
    if oid:
        res = client.app("PUT", path + "/" + urllib.parse.quote(oid, safe=""), body)
    else:
        res = client.app("POST", path, body)
    oid = res.get("id") or oid
    for c in created:
        print("created " + c)
    back = fetch_kibana(client, kind, oid)
    if back is None:
        raise ApiError("write returned OK but read-back of {} {!r} found nothing".format(kind, oid))
    print("== verify")
    issues = []
    if kind == "dashboard":
        sent = {p.get("id") for p in iter_kibana_panels(body)}
        got = {p.get("id") for p in iter_kibana_panels(back)}
        if sent - got:
            issues.append("panels missing after save: " + ", ".join(sorted(sent - got)))
        for p in iter_kibana_panels(back):
            cfg = p.get("config") or {}
            if cfg.get("type") == "xy":
                leg = (cfg.get("legend") or {}).get("visibility")
                if any(_is_multi_series(l) for l in cfg.get("layers") or []) and leg != "visible":
                    issues.append("{}: multi-series chart saved with legend {}".format(p.get("id"), leg))
    for i in issues:
        print("  WARN  " + i)
    print("saved {} {!r} ({} panels){}".format(kind, back.get("title") or oid,
                                              len(list(iter_kibana_panels(back))) if kind == "dashboard" else 1,
                                              "" if issues else ", read-back OK"))
    print("id:  " + oid)
    print("url: " + client.view_url(kind, oid))
    return 0


def _apply_osd(client, bundle, ctx, args):
    dash = bundle.get("dashboard")
    objs = list(bundle.get("visualizations") or [])
    oid = dash["id"] if dash else objs[0]["id"]
    kind = "dashboard" if dash else "visualization"
    current = fetch_osd_bundle(client, oid, kind)
    prev_vis = {v["id"] for v in (current or {}).get("visualizations") or [] if not v.get("missing")}
    new_vis = {v["id"] for v in objs}
    orphans = sorted(v for v in prev_vis - new_vis if v.startswith(oid + "--"))
    print("== plan")
    print("{} {} {!r} [{}] with {} visualization(s) in {}".format(
        "update" if current else "create", kind, (dash or objs[0])["attributes"].get("title"), oid,
        len(objs) if dash else 0, client.scope()))
    if orphans:
        print("remove {} orphaned panel visualization(s) from the previous version: {}".format(
            len(orphans), ", ".join(orphans)))
    if current and dash:
        print(_diff_summary(current["dashboard"]["attributes"], dash["attributes"], 30))
        old = {v["id"]: v for v in current.get("visualizations") or [] if not v.get("missing")}
        changed = [v["id"] for v in objs if v["id"] in old and
                   json.dumps(osd_decode(osd_encode(v))["attributes"], sort_keys=True) !=
                   json.dumps(old[v["id"]]["attributes"], sort_keys=True)]
        added = [v["id"] for v in objs if v["id"] not in old]
        if changed or added:
            print("visualizations: {} changed{}, {} new{}".format(
                len(changed), " (" + ", ".join(c.split("--", 1)[-1] for c in changed[:8]) + ")" if changed else "",
                len(added), " (" + ", ".join(a.split("--", 1)[-1] for a in added[:8]) + ")" if added else ""))
    plan = []
    try:
        osd_check_patterns(client, bundle, repair=False, created=[], plan=plan)
    except ApiError:
        pass
    for line in plan:
        print(line)
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    require_writes("write {} {}".format(kind, oid))
    created = []
    if ctx is None:
        ctx = Ctx("osd")
        ctx.patterns = {ip["title"]: ip for ip in list_index_patterns(client)}
    bundle = _resolve_osd_placeholders(client, bundle, ctx, created)
    dash = bundle.get("dashboard")
    objs = list(bundle.get("visualizations") or [])
    pattern_warnings = osd_check_patterns(client, bundle, repair=True, created=created)
    if current:
        print("backup: " + backup(client, kind, oid, current))
    payload = [osd_encode({k: o[k] for k in ("type", "id", "attributes", "references")}) for o in objs
               if not o.get("missing")]
    if dash:
        payload.append(osd_encode({k: dash[k] for k in ("type", "id", "attributes", "references")}))
    res = client.app("POST", "/api/saved_objects/_bulk_create", payload, params={"overwrite": "true"})
    errs = [o for o in res.get("saved_objects") or [] if o.get("error")]
    for c in created:
        print("created " + c)
    if errs:
        for o in errs:
            print("  ERROR {} {}: {}".format(o.get("type"), o.get("id"), o["error"].get("message")))
        return 1
    for vid in orphans:
        client.app("DELETE", "/api/saved_objects/visualization/" + urllib.parse.quote(vid, safe=""), ok=(200, 404))
    print("== verify")
    back = fetch_osd_bundle(client, oid, kind)
    issues = []
    if back is None:
        raise ApiError("bulk create returned OK but {} {!r} cannot be read back".format(kind, oid))
    missing = [v["id"] for v in back["visualizations"] if v.get("missing")]
    if missing:
        issues.append("missing referenced visualizations: " + ", ".join(missing))
    issues += pattern_warnings or osd_check_patterns(client, back, repair=False, created=[])
    for i in issues:
        print("  WARN  " + i)
    print("saved {} {!r} ({} panels){}".format(kind, (dash or objs[0])["attributes"].get("title"),
                                              len(back["visualizations"]), "" if issues else ", read-back OK"))
    print("id:  " + oid)
    print("url: " + client.view_url(kind, oid))
    return 1 if issues else 0


def cmd_delete(client, args):
    kind = "visualization" if args.vis else "dashboard"
    if not args.yes:
        raise UsageError("delete needs --yes")
    require_writes("delete {} {}".format(kind, args.id))
    if client.backend == "kibana":
        cur = fetch_kibana(client, kind, args.id)
        if cur is None:
            raise ApiError("{} {!r} not found".format(kind, args.id), 404)
        print("backup: " + backup(client, kind, args.id, cur))
        client.app("DELETE", ("/api/dashboards/" if kind == "dashboard" else "/api/visualizations/") +
                   urllib.parse.quote(args.id, safe=""), ok=(200, 204))
    else:
        cur = fetch_osd_bundle(client, args.id, kind)
        if cur is None:
            raise ApiError("{} {!r} not found".format(kind, args.id), 404)
        print("backup: " + backup(client, kind, args.id, cur))
        client.app("DELETE", "/api/saved_objects/{}/{}".format(kind, urllib.parse.quote(args.id, safe="")))
        if kind == "dashboard" and args.with_panels:
            for v in cur["visualizations"]:
                if v.get("id", "").startswith(args.id + "--") and not v.get("missing"):
                    client.app("DELETE", "/api/saved_objects/visualization/" + urllib.parse.quote(v["id"], safe=""),
                               ok=(200, 404))
                    print("deleted visualization " + v["id"])
    print("deleted {} {} (restore with: apply <backup file>)".format(kind, args.id))
    return 0


def _multipart(filename, content):
    boundary = "----kbd" + uuid.uuid4().hex
    body = ("--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{f}\"\r\n"
            "Content-Type: application/ndjson\r\n\r\n").format(b=boundary, f=filename).encode() + \
        content.encode("utf-8") + "\r\n--{}--\r\n".format(boundary).encode()
    return body, "multipart/form-data; boundary=" + boundary


def cmd_copy(client, args):
    """Export a dashboard with references and import it into another tenant/space."""
    exp = client.app("POST", "/api/saved_objects/_export",
                     {"objects": [{"type": "dashboard", "id": args.id}], "includeReferencesDeep": True}, raw=True)
    lines = [l for l in exp.splitlines() if l.strip() and '"exportedCount"' not in l]
    note("exported {} object(s)".format(len(lines)))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        note("wrote " + args.output)
    if not (args.to_tenant or args.to_space):
        return 0
    dest = Client(space=args.to_space if args.to_space else client.space,
                  tenant=args.to_tenant if args.to_tenant else client.tenant)
    require_writes("import {} objects into {}".format(len(lines), args.to_tenant or args.to_space))
    body, ctype = _multipart("export.ndjson", "\n".join(lines) + "\n")
    res = dest.app("POST", "/api/saved_objects/_import", body, params={"overwrite": "true"},
                   headers={"Content-Type": ctype})
    if not res.get("success"):
        for e in res.get("errors") or []:
            print("  ERROR {} {}: {}".format(e.get("type"), e.get("id"), json.dumps(e.get("error"))))
        return 1
    print("imported {} object(s) into {}".format(res.get("successCount"), args.to_tenant or args.to_space))
    print("url: " + dest.view_url("dashboard", args.id))
    return 0


def cmd_example(client, args):
    print(json.dumps(EXAMPLE_SPEC, indent=2))
    return 0


EXAMPLE_SPEC = {
    "id": "web-traffic",
    "title": "Web traffic overview",
    "description": "Request volume, response sizes and client reach, by response code, host and country.",
    "index": "logs-*",
    "time_field": "@timestamp",
    "time_range": "7d",
    "controls": ["response.keyword", "machine.os.keyword", "geo.dest"],
    "sections": [
        {"title": "Key metrics", "panels": [
            {"chart": "metric", "metric": "count", "label": "Requests"},
            {"chart": "metric", "metric": "avg:bytes", "label": "Avg response size"},
            {"chart": "metric", "metric": "unique:clientip", "label": "Unique clients"},
            {"chart": "metric", "metric": "p95:latency_ms", "label": "P95 latency"}]},
        {"title": "Trends", "panels": [
            {"chart": "area", "title": "Requests over time", "metric": "count", "wide": True,
             "legend_stats": ["avg", "min", "max"]},
            {"chart": "line", "title": "Requests by response code over time", "metric": "count",
             "split": "response.keyword", "size": 5}]},
        {"title": "Breakdowns", "panels": [
            {"chart": "bar_horizontal", "title": "Requests by host", "metric": "count", "x": "host.keyword"},
            {"chart": "donut", "title": "Share by OS", "metric": "count", "by": "machine.os.keyword", "size": 6},
            {"chart": "table", "title": "Top URLs", "metrics": ["count", "sum:bytes"], "by": "url.keyword",
             "size": 15}]},
    ],
}


# --- CLI ----------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="kibana_dashboards.py",
        description="Build, check and manage Kibana (9.4+) and OpenSearch Dashboards dashboards from compact specs.",
        epilog="Writes are refused unless KIBANA_READ_ONLY=0. Every overwrite/delete is backed up first.")
    ap.add_argument("--space", help="Kibana space id (default: KIBANA_SPACE or default)")
    ap.add_argument("--tenant", help="OSD tenant: private | global | <name> (default: KIBANA_TENANT)")
    # Accept the global flags after the subcommand too (`get X --tenant private`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--space", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--tenant", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)

    sub.add_parser("status", help="Detect backend/version, data endpoint, tenant, feature gates, write mode")

    p = sub.add_parser("discover", help="Profile an index: fields, cardinality, ranges, time span, suggestions")
    p.add_argument("index")
    p.add_argument("--since", default="7d", help="window for cardinality/stats (default 7d)")
    p.add_argument("--time-field")
    p.add_argument("--fields", help="regex to restrict fields")
    p.add_argument("--max-fields", type=int, default=40, help="max keyword and numeric fields to probe each")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("query", help="Run ES|QL (Kibana) or PPL/SQL (OpenSearch) and print a table")
    p.add_argument("query", help="query text, or - for stdin")
    p.add_argument("--lang", default="auto", choices=["auto", "esql", "ppl", "sql", "dsl"])
    p.add_argument("--index", help="index for --lang dsl")
    p.add_argument("--since", default="7d", help="value for ?_tstart (?_tend = now) in ES|QL")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--format", default="tsv", choices=["tsv", "json"])
    p.add_argument("--full", action="store_true")

    p = sub.add_parser("list", help="List dashboards | visualizations | index-patterns")
    p.add_argument("what", nargs="?", default="dashboards",
                   choices=["dashboards", "visualizations", "index-patterns", "data-views"])
    p.add_argument("--search")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("get", help="Outline a dashboard; -o saves an editable copy")
    p.add_argument("id")
    p.add_argument("--vis", action="store_true", help="the id is a visualization")
    p.add_argument("-o", "--output")
    p.add_argument("--json", action="store_true")

    for name, hlp in (("build", "Compile a spec to the native body (+lint); no writes"),
                      ("lint", "Check a spec / Kibana body / OSD bundle against the design and schema rules")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("file")
        p.add_argument("--offline", action="store_true", help="no cluster calls (fields not resolved)")
        p.add_argument("--target", choices=["kibana", "osd"], help="backend for --offline")
        p.add_argument("--version", help="backend version for --offline (default 9.6.0 / 3.0.0)")
        p.add_argument("--id")
        p.add_argument("-q", "--quiet", action="store_true", help="hide notes")
        if name == "build":
            p.add_argument("-o", "--output")
            p.add_argument("--json", action="store_true")

    p = sub.add_parser("layout", help="Reflow grid coordinates of a native body/bundle (keeps sizes)")
    p.add_argument("file")
    p.add_argument("-o", "--output")

    p = sub.add_parser("validate", help="Run every panel's query and report what it will show")
    p.add_argument("file", nargs="?", help="spec, Kibana body, or OSD bundle")
    p.add_argument("--id", help="OSD: validate a saved dashboard by id")
    p.add_argument("--since", help="override the time range, e.g. 30d")

    p = sub.add_parser("apply", help="[write] compile -> lint -> validate -> backup -> write -> verify")
    p.add_argument("file")
    p.add_argument("--id", help="target id (default: spec id, or a slug of the title)")
    p.add_argument("--dry-run", action="store_true", help="everything except the write; shows the diff")
    p.add_argument("--force", action="store_true", help="write despite lint/validate errors")
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--since", help="validation window override")
    p.add_argument("-o", "--output", help="also save the compiled native output")
    p.add_argument("-q", "--quiet", action="store_true")

    p = sub.add_parser("delete", help="[write] delete a dashboard (backed up first)")
    p.add_argument("id")
    p.add_argument("--vis", action="store_true")
    p.add_argument("--with-panels", action="store_true", help="OSD: also delete its generated visualizations")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("copy", help="[write] export a dashboard + references; import into another tenant/space")
    p.add_argument("id")
    p.add_argument("--to-tenant")
    p.add_argument("--to-space")
    p.add_argument("-o", "--output", help="save the NDJSON export")

    sub.add_parser("example", help="Print an example spec")
    return ap


OFFLINE_OK = {"example"}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "example":
            return cmd_example(None, args)
        offline = getattr(args, "offline", False)
        if not offline and args.cmd in ("build", "lint") and not os.environ.get("KIBANA_URL"):
            note("KIBANA_URL not set: running {} offline (fields are not checked against a cluster)".format(args.cmd))
            offline = args.offline = True
        client = None if offline else Client(space=args.space, tenant=args.tenant)
        handler = {"status": cmd_status, "discover": cmd_discover, "query": cmd_query, "list": cmd_list,
                   "get": cmd_get, "build": cmd_build, "lint": cmd_lint, "layout": cmd_layout,
                   "validate": cmd_validate, "apply": cmd_apply, "delete": cmd_delete, "copy": cmd_copy}[args.cmd]
        return handler(client, args) or 0
    except ReadOnlyError as e:
        print("error: {}".format(e), file=sys.stderr)
        return 3
    except (ApiError, UsageError) as e:
        print("error: {}".format(e), file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print("error: {}".format(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
