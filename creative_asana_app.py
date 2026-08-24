#!/usr/bin/env python3
"""
Asana Widget Dashboard.

A self-contained local app. Starts a small web server that serves a single-page
dashboard of project "widgets". Click a widget to drill into a detail view with a
per-assignee estimated-hours bar chart, a Refresh button, and a "last updated" time.

Run:  python creative_asana_app.py     (opens http://localhost:8765)

Add more widgets by appending to PROJECTS below.
Token lookup: ASANA_PAT env var, falling back to the Windows User env var (registry).
"""

import gzip
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Each entry becomes a clickable widget on the dashboard.
# "cap" (optional) is the project's monthly hour capacity, shown on the widgets.
PROJECTS = [
    {"gid": "1214228966572515", "name": "Georgia Grown Market MSA", "cap": 70},
    {"gid": "1214228966572497", "name": "Mid Eastern MSA", "cap": 38},
    {"gid": "1214228966572546", "name": "Cohen's Retreat MSA", "cap": 40},
    {"gid": "1216989200658405", "name": "My Pest Solutions SEO", "cap": 4},
    {"gid": "1214228966572508", "name": "Firebird MSA"},
    {"gid": "1214228966572503", "name": "Myrick Marine"},
    {"gid": "1214229029715234", "name": "Savannah Bee"},
    {"gid": "1214228966572536", "name": "CMD: Concierge Clinics"},
    {"gid": "1214228966572531", "name": "CMD: Pathologic"},
    {"gid": "1214228966572526", "name": "CMD: Products"},
    {"gid": "1214228966572521", "name": "Georgia Skin & Cancer Clinic"},
    {"gid": "1214228966572541", "name": "DocSmith.md MSA", "cap": 24},
    {"gid": "1214228966572551", "name": "Savannah Camellia Fest 2027"},
    {"gid": "1214755322546416", "name": "Project Twilight"},
    {"gid": "1214228966572578", "name": "Claude: Discovery and Engineering"},
    {"gid": "1214228966572573", "name": "Autotask Reporting"},
    {"gid": "1214228966572568", "name": "Brain Dump"},
    {"gid": "1214228966572563", "name": "Internal IIT Backlog"},
    {"gid": "1216154609521581", "name": "NuNu"},
    {"gid": "1216640931651593", "name": "Ross Wood Website Redesign", "cap": 30},   # one-month cap, not a recurring MSA
    {"gid": "1216208009045309", "name": "Marsh and Main"},
    {"gid": "1217239377209835", "name": "Sales"},
]

# Budget groups: several projects that share ONE combined monthly capacity.
# Each member project still appears individually in every other tab; the group only
# adds a single combined bucket to the Monthly Capacity tab (summing its members'
# logged hours against `cap`). Members are referenced by project gid.
GROUPS = [
    {"name": "CMD", "cap": 244, "gids": [
        "1214228966572536",   # CMD: Concierge Clinics
        "1214228966572531",   # CMD: Pathologic
        "1214228966572526",   # CMD: Products
        "1216154609521581",   # NuNu
        "1214228966572521",   # Georgia Skin & Cancer Clinic
    ]},
]

# Archived projects: dormant or internal work that shouldn't shape the headline numbers.
# They are still reported everywhere, just separately — the card/list tabs (Project Cards,
# MSA Project Capacity) put them under their own "Archived projects" section, below the
# active work, and they are left out of the summary stats above it. They ARE excluded from
# the aggregate estimated views (Team Capacity · Estimated and Bar Chart · Estimated), where
# a dormant project would distort per-person load and there is no section to put it in.
# Referenced by gid, so a rename can't silently un-archive one.
ARCHIVED_GIDS = {
    "1214228966572508",   # Firebird MSA
    "1214228966572503",   # Myrick Marine
    "1214228966572568",   # Brain Dump
    "1214755322546416",   # Project Twilight
    "1214228966572573",   # Autotask Reporting
    "1214228966572578",   # Claude: Discovery and Engineering
}
# The project list the aggregate estimated views (get_assignee_load) work over. Every other
# endpoint keeps all of PROJECTS and lets the UI split off the archived section.
EST_PROJECTS = [p for p in PROJECTS if p["gid"] not in ARCHIVED_GIDS]

EST_FIELD = "Estimated time"             # stored in minutes
EXCLUDE_SECTIONS = {"completed"}         # status columns excluded from hour totals (case-insensitive)
DEFAULT_START = "2026-06-01"             # default date range (inclusive) for the "Hours logged" view
DEFAULT_END = "2026-06-30"               #   entries with start <= entered_on <= end are counted
ASSIGNEE_HOURS_CAP = 128                 # estimated hours each assignee is expected to fill (all projects)
# Team Capacity chart shows only these people (sorted by remaining), with Unassigned pinned far right.
TEAM_MEMBERS = ["Miranda Osborn", "Linh Trinh", "Julia Reeves", "Grant Roach", "Alice Chao"]
PORT = 8765
API = "https://app.asana.com/api/1.0"
PROJECT_NAMES = {p["gid"]: p["name"] for p in PROJECTS}
PROJECT_CAPS = {p["gid"]: p.get("cap") for p in PROJECTS}   # monthly hour capacity, or None

# In-memory cache so navigating between pages never re-hits the Asana API.
# Data is fetched once and reused until the user explicitly clicks Refresh.
# The "hours logged" caches are keyed by month: {month: ...}.
CACHE = {"summaries": None, "detail": {}, "logged_summaries": {}, "logged_detail": {}, "me": None,
         # Raw Asana reads, shared by both halves of the app so nothing is fetched twice:
         #   tree    = {project gid: {tasks, subs, at}}   entries = {task gid: {rows, at}}
         "tree": {}, "entries": {}}
CACHE_LOCK = threading.Lock()


def get_token():
    tok = os.environ.get("ASANA_PAT")
    if tok:
        return tok.strip()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val, _ = winreg.QueryValueEx(k, "ASANA_PAT")
            if val:
                return val.strip()
    except (ImportError, FileNotFoundError, OSError):
        pass
    sys.exit('ERROR: ASANA_PAT not found. Set it with:  setx ASANA_PAT "your_token"')


TOKEN = get_token()

# Two long-lived, bounded thread pools shared across all requests.
#  - LEAF_POOL runs the many small API calls (subtasks, time entries). The binding limit is
#    Asana's per-token rate limit, not connections, and a load is hundreds of small reads, so
#    it is sized wide; api_get halves it on the first 429 rather than crawling by default.
#  - PROJECT_POOL drives whole projects concurrently; its workers only ever block
#    waiting on LEAF_POOL futures, so the two pools can't deadlock each other.
LEAF_POOL = ThreadPoolExecutor(max_workers=24)
PROJECT_POOL = ThreadPoolExecutor(max_workers=8)

# ---- HTTP layer: every Asana call goes through here ----
#
# Startup makes hundreds of small GETs to one host, so the per-call TLS handshake — not the
# response body — used to dominate the wall clock. One connection per worker thread, kept
# alive and reused, removes that round trip; the pools above are long-lived, so this is in
# effect a connection pool sized to them. gzip cuts the task-list payloads several-fold.
_LOCAL = threading.local()
_ASANA_HOST = urllib.parse.urlsplit(API).netloc


# The real ceiling is Asana's per-token rate limit (1500 requests/minute on paid plans, 150 on
# free), not connection count — so pace every call through a token bucket just under it rather
# than letting 24 threads sprint into a 429 storm. A 429 halves the rate, so a free-tier token
# settles down instead of thrashing.
_RATE = [20.0]                    # requests/second = 1200/min
_BUCKET = [20.0, time.time()]     # [tokens, last refill]
_BUCKET_LOCK = threading.Lock()


def _take_token():
    while True:
        with _BUCKET_LOCK:
            now = time.time()
            rate = _RATE[0]
            _BUCKET[0] = min(rate, _BUCKET[0] + (now - _BUCKET[1]) * rate)
            _BUCKET[1] = now
            if _BUCKET[0] >= 1:
                _BUCKET[0] -= 1
                return
            wait = (1 - _BUCKET[0]) / rate
        time.sleep(wait)


def _rate_back_off():
    with _BUCKET_LOCK:
        if _RATE[0] > 2:
            _RATE[0] = max(2.0, _RATE[0] / 2)
            print(f"WARNING: Asana rate-limited this token (429) — pacing reduced to "
                  f"{_RATE[0]:.0f} requests/sec.")


def _drop_conn():
    c = getattr(_LOCAL, "conn", None)
    if c is not None:
        try:
            c.close()
        except OSError:
            pass
    _LOCAL.conn = None


# Every Asana round trip is counted by kind, so the console can report what a load actually
# cost ("/api/projects  71 Asana calls") instead of leaving it a mystery.
REQ_COUNTS = {}
REQ_LOCK = threading.Lock()


def _count(target):
    kind = ("batch" if "batch_requests" in target else
            "entries" if "time_tracking_entries" in target else
            "subtasks" if "/subtasks" in target else
            "tasks" if "/tasks?" in target else "other")
    with REQ_LOCK:
        REQ_COUNTS[kind] = REQ_COUNTS.get(kind, 0) + 1


def req_snapshot():
    with REQ_LOCK:
        return dict(REQ_COUNTS)


def api_get(path_or_url, tries=4):
    """GET one Asana URL (path or absolute) and return its decoded JSON body."""
    return _api_call("GET", path_or_url, None, tries)


def api_post(path, body, tries=4):
    """POST a JSON body to one Asana path and return its decoded JSON response."""
    return _api_call("POST", path, json.dumps(body).encode(), tries)


def _api_call(method, path_or_url, payload, tries=4):
    """One Asana request over the thread's keep-alive connection.

    Retries a rate limit (429), a 5xx, and a connection the server closed between calls —
    an idle keep-alive connection can go away at any time, which is not an error worth
    surfacing. Anything else raises urllib.error.HTTPError, so the request handler's existing
    502 path still reports it unchanged.
    """
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    parts = urllib.parse.urlsplit(url)
    target = parts.path + (("?" + parts.query) if parts.query else "")
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json",
               "Accept-Encoding": "gzip"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    _count(target)
    for attempt in range(tries):
        last = attempt == tries - 1
        _take_token()
        try:
            conn = getattr(_LOCAL, "conn", None)
            if conn is None:
                conn = _LOCAL.conn = http.client.HTTPSConnection(_ASANA_HOST, timeout=60)
            conn.request(method, target, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()          # drain in full, or the connection can't be reused
        except (http.client.HTTPException, OSError):
            _drop_conn()               # stale or broken: reconnect and try again
            if last:
                raise
            time.sleep(0.3 * (attempt + 1))
            continue
        if resp.status == 429 or resp.status >= 500:
            if resp.status == 429:
                _rate_back_off()
            if (resp.getheader("Connection") or "").lower() == "close":
                _drop_conn()
            if last:
                raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
            time.sleep(float(resp.getheader("Retry-After") or (attempt + 1)))
            continue
        if resp.status >= 400:
            raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
        if (resp.getheader("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)


def api_pages(path):
    """GET a paginated Asana collection, following next_page. Returns the concatenated data."""
    out, url = [], path
    while url:
        page = api_get(url)
        out.extend(page.get("data", []))
        nxt = page.get("next_page")
        url = nxt["uri"] if nxt and nxt.get("uri") else None
    return out


# Asana's /batch_requests runs up to 10 reads in one HTTP request. Startup is dominated by
# hundreds of tiny per-task reads (time entries, subtasks), so batching them cuts the round
# trips — the thing that actually costs seconds — by ~10x.
BATCH_MAX = 10
# Asana accepts two shapes for a batch action: the query string inline in relative_path, or the
# path bare with `options` alongside. Workspaces differ in what they accept, so try the inline
# form, fall back to the structured form, and only then give up on batching for this process —
# escalating once, not once per chunk, so a rejection can't cost hundreds of wasted POSTs.
BATCH_MODES = ["path", "options", "off"]
_BATCH_MODE = [0]


def _batch_action(path, mode):
    if mode == "path":
        return {"method": "get", "relative_path": path}
    base, _, qs = path.partition("?")
    q = urllib.parse.parse_qs(qs)
    options = {}
    if q.get("opt_fields"):
        options["fields"] = q["opt_fields"][0].split(",")
    if q.get("limit"):
        options["limit"] = int(q["limit"][0])
    return {"method": "get", "relative_path": base, "options": options}


def _batch_off(why):
    """Say once, loudly, that batching isn't working. The load still completes — every read
    falls back to its own request — but at ~10x the requests, i.e. a slow start."""
    if _BATCH_MODE[0] == len(BATCH_MODES) - 1:
        return
    _BATCH_MODE[0] = len(BATCH_MODES) - 1
    print(f"WARNING: Asana /batch_requests unavailable ({why}) — falling back to one request "
          f"per read, so this load will be slow. Data is unaffected.")


def _batch_try(paths, mode):
    """(results, problem) for one batch attempt. results is None when this shape didn't work."""
    actions = [_batch_action(p, mode) for p in paths]
    try:
        results = api_post("/batch_requests", {"data": {"actions": actions}}).get("data")
    except urllib.error.HTTPError as e:
        # Some accounts have no /batch_requests endpoint at all (404) or can't reach it (403).
        # No action shape can fix that, so stop trying immediately.
        if e.code in (403, 404):
            _batch_off(f"Asana {e.code} on /batch_requests")
        return None, f"Asana {e.code}"
    except (http.client.HTTPException, OSError, json.JSONDecodeError) as e:
        return None, e
    if not isinstance(results, list) or len(results) != len(paths):
        return None, "unexpected /batch_requests response"
    # Every action failing points at the request shape — but only if there were several. A
    # small batch can fail outright for ordinary reasons (a deleted task), and that must not
    # be read as "batching is broken"; those paths just fall back individually below.
    if len(paths) >= 3 and all(not r or r.get("status_code") != 200 for r in results):
        first = results[0] or {}
        errs = (first.get("body") or {}).get("errors") or [{}]
        return None, errs[0].get("message") or f"status {first.get('status_code')}"
    return results, None


def _batch_chunk(paths):
    """One /batch_requests call covering up to BATCH_MAX paths -> {path: data list}.

    Any batch Asana rejects, any action that failed, and any first page that says there is more
    falls back to a plain GET. Batching is an optimization only: it can cost requests, never data.
    """
    results = None
    while True:
        mode_i = _BATCH_MODE[0]
        mode = BATCH_MODES[mode_i]
        if mode == "off":
            break
        results, problem = _batch_try(paths, mode)
        if results is not None:
            break
        if _BATCH_MODE[0] != mode_i:
            break               # _batch_try already gave up for good (e.g. a 404 endpoint)
        # Every action failed: this shape isn't supported here. Try the next one, then stop.
        if BATCH_MODES[mode_i + 1] == "off":
            _batch_off(problem)
            break
        print(f'Asana batch shape "{mode}" rejected ({problem}) — trying the next shape.')
        _BATCH_MODE[0] = mode_i + 1
    if results is None:
        return {p: api_pages(p) for p in paths}
    out = {}
    for path, res in zip(paths, results):
        body = (res or {}).get("body") or {}
        # A per-action failure, or a first page that says there are more: fetch that one
        # directly rather than dropping rows. Batch actions return only their first page.
        if (res or {}).get("status_code") != 200 or "data" not in body or body.get("next_page"):
            out[path] = api_pages(path)
        else:
            out[path] = body["data"]
    return out


_BATCH_PROBED = [False]
_BATCH_PROBE_LOCK = threading.Lock()


def api_batch(paths):
    """GET many small collections, BATCH_MAX per HTTP request. Returns {path: data list}."""
    paths = list(paths)
    if not paths:
        return {}
    chunks = [paths[i:i + BATCH_MAX] for i in range(0, len(paths), BATCH_MAX)]
    out = {}
    # Send the very first chunk on its own and wait for it. If this account has no
    # /batch_requests endpoint, that costs exactly one wasted request to find out, instead of a
    # whole parallel wave of them discovering it at the same time.
    if not _BATCH_PROBED[0]:
        with _BATCH_PROBE_LOCK:
            if not _BATCH_PROBED[0]:
                out.update(_batch_chunk(chunks[0]))
                _BATCH_PROBED[0] = True
                chunks = chunks[1:]
    for part in LEAF_POOL.map(_batch_chunk, chunks):
        out.update(part)
    return out


# One field set for both halves of the app. `num_subtasks` lets fetch_tree skip the subtask
# call for a leaf task (most tasks are leaves), and `completed_at` is what the logged-hours
# path needs — asking for it here means that path no longer re-fetches the same lists.
TASK_FIELDS = ("name,assignee.name,completed,completed_at,actual_time_minutes,num_subtasks,"
               "custom_fields.name,custom_fields.number_value,"
               "memberships.section.name,memberships.project.gid")
SUBTASK_FIELDS = ("name,assignee.name,completed,completed_at,actual_time_minutes,"
                  "custom_fields.name,custom_fields.number_value")


def fetch_tasks(gid):
    """Return all tasks for a project (name + assignee + completed + estimated/actual time + section)."""
    return api_pages(f"/projects/{gid}/tasks?opt_fields={TASK_FIELDS}&limit=100")


def subtasks_path(task_gid):
    return f"/tasks/{task_gid}/subtasks?opt_fields={SUBTASK_FIELDS}&limit=100"


def entries_path(task_gid):
    fields = "duration_minutes,entered_on,created_by.name"
    return f"/tasks/{task_gid}/time_tracking_entries?opt_fields={fields}&limit=100"


# A refresh landing within this many seconds of the last one reuses the fetched tree. The
# dashboard refreshes estimated and logged hours as two concurrent requests, and they must not
# pull the same project twice; a real refresh minutes later still re-reads Asana.
REFRESH_SHARE_WINDOW = 30
_TREE_LOCKS = {}


def _tree_lock(gid):
    with CACHE_LOCK:
        lk = _TREE_LOCKS.get(gid)
        if lk is None:
            lk = _TREE_LOCKS[gid] = threading.Lock()
        return lk


def fetch_tree(gid, refresh=False):
    """Every task and subtask of one project, fetched once and shared by both halves of the app.

    The estimated and logged-hours paths need the same two lists; they used to fetch them
    separately, so every project's tasks were pulled twice and every parent's subtasks twice.
    Returns {"tasks": [...], "subs": {parent_gid: [...]}, "at": <fetch time>}.

    Single-flighted per project: the two concurrent startup requests share one fetch instead
    of racing to make the same calls. The per-gid lock is held across the network work, which
    is the point — the second caller waits for the first's result — and it never holds
    CACHE_LOCK or a PROJECT_POOL slot while doing so.
    """
    def usable():
        with CACHE_LOCK:
            e = CACHE["tree"].get(gid)
        if e and (not refresh or time.time() - e["at"] < REFRESH_SHARE_WINDOW):
            return e
        return None

    hit = usable()
    if hit:
        return hit
    with _tree_lock(gid):
        hit = usable()          # another thread may have fetched it while we waited
        if hit:
            return hit
        tasks = fetch_tasks(gid)
        parents = [t["gid"] for t in tasks if (t.get("num_subtasks") or 0) > 0]
        by_path = api_batch(subtasks_path(g) for g in parents)
        subs = {g: by_path[subtasks_path(g)] for g in parents}
        entry = {"tasks": tasks, "subs": subs, "at": time.time()}
        with CACHE_LOCK:
            CACHE["tree"][gid] = entry
        return entry


def get_me(refresh=False):
    """The Asana display name that owns the PAT, so the UI can default a filter to "you".

    One tiny call, cached for the life of the process — the token can't change under us.
    """
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["me"]
        if cached is not None:
            return cached
    data = api_get("/users/me?opt_fields=name").get("data") or {}
    me = {"name": data.get("name") or ""}
    with CACHE_LOCK:
        CACHE["me"] = me
    return me


def task_minutes(t):
    for cf in t.get("custom_fields", []):
        if cf.get("name") == EST_FIELD and cf.get("number_value") is not None:
            return cf["number_value"]
    return 0


def actual_minutes(t):
    """Total tracked time on a task/subtask, in minutes."""
    return t.get("actual_time_minutes") or 0


def section_name(t, gid):
    """The task's section (status column) within project `gid`, or ''."""
    fallback = ""
    for m in t.get("memberships", []):
        sec = (m.get("section") or {}).get("name")
        if not sec:
            continue
        if (m.get("project") or {}).get("gid") == gid:
            return sec
        fallback = fallback or sec
    return fallback


def is_excluded(t, gid):
    """True if the task sits in an excluded status column (e.g. Completed) of this project."""
    return section_name(t, gid).strip().lower() in EXCLUDE_SECTIONS


def build_task(t, gid, children):
    """Shape one task plus its (incomplete) subtasks for the drill-down list.

    `children` comes from the shared project tree, so this makes no API calls of its own.
    """
    parent_min = task_minutes(t)
    subs, sub_min = [], 0
    for s in children:
        if s.get("completed"):
            continue  # completed subtasks are not shown or counted
        m = task_minutes(s)
        sub_min += m
        subs.append({
            "name": s.get("name", "(untitled)"),
            "assignee": (s.get("assignee") or {}).get("name") or "Unassigned",
            "hours": round(m / 60, 2),
            "actual": round(actual_minutes(s) / 60, 2),
        })
    return {
        "gid": t["gid"],
        "name": t.get("name", "(untitled)"),
        "assignee": (t.get("assignee") or {}).get("name") or "Unassigned",
        "hours": round(parent_min / 60, 2),   # parent's own estimate (attributed to parent assignee)
        "actual": round(actual_minutes(t) / 60, 2),   # parent's own tracked time
        "section": section_name(t, gid),
        "subtasks": subs,                     # each subtask attributed to its own assignee
    }


def project_detail(gid, refresh=False):
    tree = fetch_tree(gid, refresh=refresh)
    # Drop checked-off tasks and anything in an excluded (e.g. Completed) section.
    tasks = [t for t in tree["tasks"] if not t.get("completed") and not is_excluded(t, gid)]
    # Pure shaping now — the tree already holds every subtask, so this makes no API calls.
    detailed = [build_task(t, gid, tree["subs"].get(t["gid"], [])) for t in tasks]

    # Attribute each task's own hours to its assignee, and each subtask's hours to
    # the subtask's assignee (not the parent owner). Estimated and actual time are
    # attributed the same way so remaining = estimated - actual lines up per person.
    # counts = work items per assignee.
    totals, actuals, counts = {}, {}, {}
    for d in detailed:
        totals[d["assignee"]] = totals.get(d["assignee"], 0) + d["hours"]
        actuals[d["assignee"]] = actuals.get(d["assignee"], 0) + d["actual"]
        counts[d["assignee"]] = counts.get(d["assignee"], 0) + 1
        for s in d["subtasks"]:
            totals[s["assignee"]] = totals.get(s["assignee"], 0) + s["hours"]
            actuals[s["assignee"]] = actuals.get(s["assignee"], 0) + s["actual"]
            counts[s["assignee"]] = counts.get(s["assignee"], 0) + 1
    ordered = sorted(totals, key=lambda n: totals[n], reverse=True)
    return {
        "gid": gid,
        "name": PROJECT_NAMES.get(gid, gid),
        "labels": ordered,
        "hours": [round(totals[n], 2) for n in ordered],
        "actual_hours": [round(actuals[n], 2) for n in ordered],
        "counts": [counts[n] for n in ordered],
        "ntasks": len(detailed),
        "tasks": detailed,
        "updated": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
    }


# ---- Cache layer: only hits Asana when refresh=True; otherwise serves cached data ----

def summary_from_detail(d):
    return {
        "gid": d["gid"],
        "name": d["name"],
        "ntasks": d["ntasks"],
        "hours": round(sum(d["hours"]), 2),
        "cap": PROJECT_CAPS.get(d["gid"]),
        "updated": d["updated"],
    }


def get_detail(gid, refresh=False):
    """Cached project detail. Hits Asana only when refresh or not yet cached."""
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["detail"].get(gid)
        if cached is not None:
            return cached
    data = project_detail(gid, refresh=refresh)
    with CACHE_LOCK:
        CACHE["detail"][gid] = data
        # Keep any cached dashboard summary in sync with this fresh detail.
        if CACHE["summaries"] is not None:
            CACHE["summaries"] = [
                summary_from_detail(data) if s["gid"] == gid else s
                for s in CACHE["summaries"]
            ]
    return data


def get_summaries(refresh=False):
    """Cached dashboard widgets. Builds from (cached) per-project detail."""
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["summaries"]
        if cached is not None:
            return cached
    # Build every project's detail concurrently instead of one-at-a-time.
    details = PROJECT_POOL.map(lambda p: get_detail(p["gid"], refresh=refresh), PROJECTS)
    out = [summary_from_detail(d) for d in details]
    with CACHE_LOCK:
        CACHE["summaries"] = out
    return out


def assignee_project_tasks(d, name):
    """The task/subtask rows assigned to `name` within one project detail `d`,
    each with estimated / actual / remaining hours and status (section)."""
    rows = []
    for t in d["tasks"]:
        if t["assignee"] == name:
            rows.append({
                "name": t["name"], "type": "task", "status": t["section"],
                "estimated": t["hours"], "actual": t["actual"],
                "remaining": round(t["hours"] - t["actual"], 2), "context": "",
            })
        for s in t["subtasks"]:
            if s["assignee"] == name:
                rows.append({
                    "name": s["name"], "type": "subtask", "status": t["section"],
                    "estimated": s["hours"], "actual": s["actual"],
                    "remaining": round(s["hours"] - s["actual"], 2),
                    # note the parent when this subtask lives under someone else's task
                    "context": "" if t["assignee"] == name else f'under "{t["name"]}" · {t["assignee"]}',
                })
    return rows


def get_assignee_load(refresh=False):
    """Remaining hours (estimated - actual) per assignee across ALL projects, vs. the cap.

    Estimated and actual are both attributed to each item's assignee, so the chart's
    `hours` is the work each person still has left to do. Reuses the cached per-project
    detail, so it adds no Asana calls when the projects are already loaded.
    """
    details = list(PROJECT_POOL.map(lambda p: get_detail(p["gid"], refresh=refresh), EST_PROJECTS))
    est, act, counts, breakdown = {}, {}, {}, {}
    for d in details:
        for name, e, a, cnt in zip(d["labels"], d["hours"], d["actual_hours"], d["counts"]):
            est[name] = est.get(name, 0) + e
            act[name] = act.get(name, 0) + a
            counts[name] = counts.get(name, 0) + cnt
            if e or a:
                breakdown.setdefault(name, []).append({
                    "project": d["name"], "estimated": round(e, 2),
                    "actual": round(a, 2), "remaining": round(e - a, 2),
                    "tasks": assignee_project_tasks(d, name),
                })
    # Only the named team members (sorted by remaining), with Unassigned pinned far right.
    ordered = sorted((n for n in TEAM_MEMBERS if n in est),
                     key=lambda n: est[n] - act[n], reverse=True)
    if "Unassigned" in est:
        ordered.append("Unassigned")
    return {
        "cap": ASSIGNEE_HOURS_CAP,
        "labels": ordered,
        "hours": [round(est[n] - act[n], 2) for n in ordered],   # remaining = estimated - actual
        "estimated": [round(est[n], 2) for n in ordered],
        "actual": [round(act[n], 2) for n in ordered],
        "counts": [counts[n] for n in ordered],
        "breakdown": {n: breakdown.get(n, []) for n in ordered},
        "updated": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
    }


# ---- "Hours logged": actual time-tracking entries dated in a given month (YYYY-MM) ----

# The entry cache is the one thing worth keeping across restarts: it is the bulk of a load, and
# it self-invalidates on the tracked-minutes total (see entries_for_tasks). Kept next to the
# script, gitignored, and holding nothing secret — task gids, durations, dates, author names.
ENTRY_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".entries_cache.json")
ENTRY_CACHE_VERSION = 1
ENTRY_CACHE_MAX = 8000           # tasks; oldest fetches are dropped past this
_ENTRY_CACHE_DIRTY = [False]
_ENTRY_CACHE_IO = threading.Lock()


def load_entry_cache():
    """Warm CACHE["entries"] from disk. Any problem is ignored — it's only a cache."""
    try:
        with open(ENTRY_CACHE_PATH, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return 0
    if not isinstance(blob, dict) or blob.get("v") != ENTRY_CACHE_VERSION:
        return 0
    entries = blob.get("entries")
    if not isinstance(entries, dict):
        return 0
    with CACHE_LOCK:
        # `at` is deliberately not restored: a fresh process must not think it just fetched
        # these, or the REFRESH_SHARE_WINDOW would suppress a genuine Refresh.
        for gid, e in entries.items():
            if isinstance(e, dict) and isinstance(e.get("rows"), list):
                CACHE["entries"][gid] = {"rows": e["rows"], "minutes": e.get("minutes"), "at": 0}
        return len(CACHE["entries"])


def save_entry_cache():
    """Write the entry cache out if it changed. Best-effort: a failure just costs speed."""
    if not _ENTRY_CACHE_DIRTY[0]:
        return
    with _ENTRY_CACHE_IO:
        _ENTRY_CACHE_DIRTY[0] = False
        with CACHE_LOCK:
            items = sorted(CACHE["entries"].items(), key=lambda kv: -(kv[1].get("at") or 0))
            keep = {g: {"rows": e["rows"], "minutes": e.get("minutes")}
                    for g, e in items[:ENTRY_CACHE_MAX]}
        tmp = ENTRY_CACHE_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": ENTRY_CACHE_VERSION, "entries": keep}, f)
            os.replace(tmp, ENTRY_CACHE_PATH)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def entries_for_tasks(minutes_by_gid, refresh=False):
    """Time entries for many tasks -> {task gid: rows}. Input is {task gid: tracked minutes}.

    This is the call that dominates a load — one request per task that has tracked time, and
    Asana's rate limit puts a hard floor under a few hundred of those. So entries are cached and
    only re-read when they can have changed:

    a task's `actual_time_minutes` IS the sum of its time entries, so an unchanged total means
    unchanged entries. That invariant is what makes the cache safe to keep across a restart —
    any task whose total moved is always re-fetched, and Refresh re-reads everything regardless.
    Entries also don't depend on the selected date range, so changing the range only re-filters.
    """
    out, missing, now = {}, [], time.time()
    with CACHE_LOCK:
        for g, minutes in minutes_by_gid.items():
            e = CACHE["entries"].get(g)
            if not e:
                missing.append(g)
            elif now - e.get("at", 0) < REFRESH_SHARE_WINDOW:
                out[g] = e["rows"]      # just fetched; don't re-read for a concurrent request
            elif not refresh and e.get("minutes") == minutes:
                out[g] = e["rows"]      # total unchanged -> entries unchanged
            else:
                missing.append(g)
    if missing:
        by_path = api_batch(entries_path(g) for g in missing)
        fetched = time.time()
        with CACHE_LOCK:
            for g in missing:
                rows = by_path[entries_path(g)]
                CACHE["entries"][g] = {"rows": rows, "minutes": minutes_by_gid[g], "at": fetched}
                out[g] = rows
        _ENTRY_CACHE_DIRTY[0] = True
    return out


def logged_detail(gid, start=DEFAULT_START, end=DEFAULT_END, refresh=False):
    """Per-person hours logged in [start, end] for one project (tasks + subtasks).

    Reads the shared project tree, so on a normal load the only calls this makes are the
    per-item time-entry lookups — the task and subtask lists come from the estimated side's
    fetch (or vice versa, whichever got there first).
    """
    tree = fetch_tree(gid, refresh=refresh)
    tasks = tree["tasks"]

    # Tasks/subtasks completed within the date range (by completed_at), deduped by gid.
    completed_dates = {}
    def note_completed(item):
        if item.get("completed") and item.get("completed_at"):
            day = item["completed_at"][:10]
            if start <= day <= end:
                completed_dates[item["gid"]] = day

    # Only query time entries for items that actually have logged time.
    # Dedupe by task gid: a subtask added directly to the project would otherwise be
    # reached twice (project task list + parent's subtasks) and double-counted.
    # {gid: (name, tracked minutes)} — the minutes are what the entry cache validates against.
    cand_by_gid = {}
    for item in list(tasks) + [s for subs in tree["subs"].values() for s in subs]:
        note_completed(item)
        minutes = item.get("actual_time_minutes") or 0
        if minutes > 0:
            cand_by_gid[item["gid"]] = (item.get("name", "(untitled)"), minutes)
    # Only tasks whose tracked total moved are read from Asana; the rest come from cache.
    by_gid = entries_for_tasks({g: m for g, (_, m) in cand_by_gid.items()}, refresh=refresh)

    # Collect entries, deduping by entry gid as a final guard against any double-pull.
    seen, entries = set(), []
    for tgid, (name, _minutes) in cand_by_gid.items():
        for e in by_gid.get(tgid, []):
            entered = e.get("entered_on") or ""
            if not entered or not (start <= entered <= end):
                continue
            egid = e.get("gid")
            if egid and egid in seen:
                continue
            if egid:
                seen.add(egid)
            entries.append({
                "task": name,
                "by": (e.get("created_by") or {}).get("name") or "Unknown",
                "date": entered,
                # Every logged entry counts the same, regardless of Asana's billable flag.
                "minutes": e.get("duration_minutes") or 0,
            })

    # Per person: total minutes logged. All logged time counts toward project budgets.
    totals, counts = {}, {}
    for e in entries:
        totals[e["by"]] = totals.get(e["by"], 0) + e["minutes"]
        counts[e["by"]] = counts.get(e["by"], 0) + 1
    ordered = sorted(totals, key=lambda n: totals[n], reverse=True)
    entries.sort(key=lambda e: e["date"])
    return {
        "gid": gid,
        "name": PROJECT_NAMES.get(gid, gid),
        "start": start,
        "end": end,
        "labels": ordered,
        "hours": [round(totals[n] / 60, 2) for n in ordered],
        "counts": [counts[n] for n in ordered],
        "total_hours": round(sum(totals.values()) / 60, 2),
        "completed": len(completed_dates),
        "nentries": len(entries),
        "entries": [
            {"task": e["task"], "by": e["by"], "date": e["date"],
             "hours": round(e["minutes"] / 60, 2)}
            for e in entries
        ],
        "updated": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
    }


def logged_summary_from_detail(d):
    return {
        "gid": d["gid"],
        "name": d["name"],
        "start": d["start"],
        "end": d["end"],
        "hours": d["total_hours"],
        "completed": d["completed"],
        "nentries": d["nentries"],
        "cap": PROJECT_CAPS.get(d["gid"]),
        "updated": d["updated"],
    }


def get_logged_detail(gid, refresh=False, start=DEFAULT_START, end=DEFAULT_END):
    """Cached per-project logged-hours detail for a date range. Cache key = 'start:end'."""
    key = f"{start}:{end}"
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["logged_detail"].get(key, {}).get(gid)
        if cached is not None:
            return cached
    data = logged_detail(gid, start, end, refresh=refresh)
    with CACHE_LOCK:
        CACHE["logged_detail"].setdefault(key, {})[gid] = data
        # Keep this range's cached summary in sync with the fresh detail.
        rsum = CACHE["logged_summaries"].get(key)
        if rsum is not None:
            CACHE["logged_summaries"][key] = [
                logged_summary_from_detail(data) if s["gid"] == gid else s
                for s in rsum
            ]
    return data


def get_logged_summaries(refresh=False, start=DEFAULT_START, end=DEFAULT_END):
    key = f"{start}:{end}"
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["logged_summaries"].get(key)
        if cached is not None:
            return cached
    # Build every project's logged-hours detail concurrently instead of one-at-a-time.
    details = PROJECT_POOL.map(lambda p: get_logged_detail(p["gid"], refresh=refresh, start=start, end=end), PROJECTS)
    out = [logged_summary_from_detail(d) for d in details]
    with CACHE_LOCK:
        CACHE["logged_summaries"][key] = out
    save_entry_cache()      # end of a full load: persist whatever it just read
    return out


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark">
<title>Creative Hours Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { --blue:#6aa9e0; --blue-d:#9cc7f0; --green:#4cc085; --green-d:#6cd49d; --red:#e26b66;
          --bg:#12151a; --panel:#2a2f38; --panel2:#353b45; --border:#3c4350;
          --text:#edeff2; --muted:#a3aab4; --faint:#737b86;
          /* capacity gold — the same reserved hue as C.amber; only ever used for
             "this budget is spent" signals, never as a data color. */
          --amber:#f0c674; --amber-line:#7d6a3c; --amber-tint:#2c281d;
          /* Unspent blocks on a budget bar. Deliberately lighter than --panel2, which is the
             background of the capacity cards the bar sits on — at --panel2 it disappeared. */
          --blk-off:#5b6474; }
  body { font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0; background:var(--bg); color:var(--text); }
  .wrap { max-width:1040px; margin:32px auto; padding:0 20px; }
  h1 { font-size:22px; margin:0 0 2px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 24px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:18px; }
  .card { background:var(--panel); border-radius:12px; padding:20px; box-shadow:0 1px 3px rgba(0,0,0,.4);
          cursor:pointer; transition:transform .08s, box-shadow .08s; border:1px solid var(--border); }
  .card:hover { transform:translateY(-2px); box-shadow:0 8px 22px rgba(0,0,0,.55); border-color:var(--blue); }
  .card h3 { margin:0 0 14px; font-size:16px; }
  .stats { display:flex; gap:24px; }
  .stat .n { font-size:24px; font-weight:600; color:var(--blue-d); }
  .stat .l { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .card .go { margin-top:14px; font-size:12px; color:var(--blue-d); }
  .head-right { display:flex; align-items:center; gap:12px; }
  /* Loading / empty / "nothing matches the filter" states. One component everywhere, so
     every tab announces itself the same way. .box centers it inside a chart frame. */
  .note { color:var(--muted); font-size:13px; margin:14px 0; }
  .note.box { display:flex; align-items:center; justify-content:center; height:100%;
              margin:0; padding:24px; text-align:center; }
  /* A number that has gone the wrong way (over budget). Listed against the card/summary
     selectors too, so it wins over the blue/green accent those numbers normally take. */
  .neg, .card .stat .n.neg, .card.logged .stat .n.neg { color:var(--red); }
  .dash-updated { font-size:11px; color:var(--faint); white-space:nowrap; }
  /* Budget bar: 10 discrete blocks of 10% rather than one continuous fill, so a glance
     reads as a count ("7 of 10 blocks") instead of an eyeballed length. Green = room
     left, gold = exactly at capacity, red = over. */
  .cap-bar { margin-top:14px; }
  .cap-wide { margin:0 0 18px; }   /* the bucket page's budget bar, under the stat strip */
  .cap-bar .track { display:flex; gap:2px; }
  .cap-bar .blk { flex:1 1 0; height:9px; border-radius:2px; background:var(--blk-off); }
  .cap-bar .blk.on { background:var(--green); }
  .cap-bar.at .blk.on { background:var(--amber); }
  .cap-bar.over .blk.on { background:var(--red); }
  .cap-bar .lab { font-size:11px; color:var(--muted); margin-top:5px; }
  /* Filter toolbar. Every tab that filters anything uses exactly one of these, directly
     under the stat strip, and it always opens with a bold .tb-label. It sits in its own
     panel so the controls read as one strip instead of floating on the page. */
  .toolbar { display:flex; align-items:center; flex-wrap:wrap; gap:10px 14px; margin:0 0 20px;
             background:var(--panel); border:1px solid var(--border); border-radius:10px;
             padding:11px 16px; font-size:13px; color:var(--muted); }
  .toolbar label { margin-left:6px; }
  .toolbar label:first-child { margin-left:0; }
  .toolbar select, .toolbar input[type=date] { background:var(--panel2); color:var(--text);
                    border:1px solid var(--border); border-radius:7px; padding:6px 9px; font-size:13px; cursor:pointer; }
  .toolbar select:hover, .toolbar input[type=date]:hover { border-color:#4d5666; }
  .toolbar input[type=date]::-webkit-calendar-picker-indicator { filter:invert(.8); cursor:pointer; }
  /* Search re-runs the range; Refresh (top right) is the page's primary action, so this
     one stays secondary and doesn't compete with it. */
  .toolbar .btn { background:var(--panel2); color:var(--text); border:1px solid var(--border);
                  padding:6px 14px; font-size:13px; }
  .toolbar .btn:hover { background:#414854; border-color:#4d5666; }
  .toolbar .tb-label { font-weight:600; color:var(--text); margin-left:0; }
  /* A label and its control wrap as one unit, so a narrow window never strands a label
     on the end of a line with its <select> on the next. */
  .toolbar .tb-group { display:inline-flex; align-items:center; gap:8px; }
  .toolbar .chk { display:inline-flex; align-items:center; gap:5px; margin-left:0; cursor:pointer; }
  .toolbar .chk input { cursor:pointer; margin:0; }
  .toolbar .tb-sep { width:1px; align-self:stretch; background:var(--border); margin:2px 4px; }
  /* dashboard layout: left nav + content */
  .layout { display:flex; gap:24px; align-items:flex-start; }
  .sidebar { flex:0 0 210px; position:sticky; top:32px; background:var(--panel); border-radius:12px;
             padding:14px 12px; box-shadow:0 1px 3px rgba(0,0,0,.4); border:1px solid var(--border); }
  .sidebar .brand { font-size:15px; font-weight:600; padding:6px 12px 14px; color:var(--text); }
  /* Sidebar sections carry the same blue = estimated / green = logged semantics as the rest
     of the app, so the two halves of the nav are told apart at a glance: a colored dot and
     heading, a colored rail down the group, and an active item in that section's accent.
     A hairline between sections stops the eight items reading as one flat list. */
  .nav-sec + .nav-sec { margin-top:12px; padding-top:2px; border-top:1px solid var(--border); }
  .nav-section { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.06em;
                 color:var(--faint); padding:14px 12px 6px; }
  .nav-sec:first-child .nav-section { padding-top:4px; }
  .nav-section::before { content:''; display:inline-block; width:6px; height:6px; border-radius:50%;
                         margin-right:6px; vertical-align:middle; background:var(--faint); }
  .nav-sec.est .nav-section { color:var(--blue); }
  .nav-sec.est .nav-section::before { background:var(--blue); }
  .nav-sec.act .nav-section { color:var(--green); }
  .nav-sec.act .nav-section::before { background:var(--green); }
  .nav-group { border-left:2px solid var(--border); padding-left:7px; margin-left:3px; }
  .nav-sec.est .nav-group { border-left-color:var(--blue); }
  .nav-sec.act .nav-group { border-left-color:var(--green); }
  .nav-item { display:block; padding:10px 12px; margin-bottom:4px; border-radius:8px;
              font-size:14px; color:var(--muted); text-decoration:none; cursor:pointer; }
  .nav-item:hover { background:var(--panel2); color:var(--text); }
  .nav-item.active { background:var(--blue); color:#10141a; font-weight:600; }
  .nav-sec.act .nav-item.active { background:var(--green); }
  .content { flex:1; min-width:0; }
  .content .head { margin-bottom:4px; }
  .content h1 { font-size:20px; margin:0; }
  .content .sub { margin:3px 0 16px; }   /* tighter than the standalone .sub default */
  .section-h { font-size:16px; margin:30px 0 12px; padding-top:6px; border-top:1px solid var(--border); }
  .section-h.flush { margin-top:8px; padding-top:0; border-top:0; }   /* first heading under the filter */
  /* Headline stat strip. Every tab opens with one so the top-line numbers are always in
     the same place. Deliberately quieter than the <h1> above it — it summarises the page,
     it isn't the page. Green numbers on Actual-Hours tabs, blue on Estimated (.est). */
  /* No box of its own — a boxed strip stacked above the boxed toolbar reads as clutter.
     A hairline underneath is enough to set it apart from the controls below. */
  .summary-bar { display:flex; flex-wrap:wrap; gap:12px 36px; padding:2px 2px 15px; margin:0 0 16px;
                 border-bottom:1px solid var(--border); }
  .summary-stat { display:flex; flex-direction:column; gap:2px; }
  .summary-stat .n { font-size:19px; font-weight:600; line-height:1.2; color:var(--green-d);
                     font-variant-numeric:tabular-nums; }
  .summary-stat .l { font-size:11px; color:var(--faint); text-transform:uppercase; letter-spacing:.05em; }
  .summary-bar.est .summary-stat .n { color:var(--blue-d); }
  .summary-stat .n.neg { color:var(--red); }
  .card.logged:hover { border-color:var(--green); }
  .card.logged .stat .n, .card.logged .go { color:var(--green-d); }
  .card.logged .cap-bar { margin-top:22px; }   /* extra space between the big numbers and the bar */
  /* MSA/capacity cards: lighter panel + brighter border so they stand out at the top of the list */
  .card.cap { background:var(--panel2); border-color:#5c6b86; }
  /* A project (or bucket) that has spent its whole monthly budget: gold tint, so "no hours
     left" is visible from the grid without reading a single number. */
  .card.at-cap, .card.cap.at-cap { background:var(--amber-tint); border-color:var(--amber-line); }
  .card.at-cap:hover { border-color:var(--amber); }
  .card.at-cap .grp-row:hover { background:#3a3427; }
  .grp-card { grid-column: 1 / -1; }   /* combined buckets (e.g. CMD) span the whole row */
  /* combined budget-group card: per-project breakdown rows */
  .grp-tag { font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.05em;
             color:var(--muted); background:var(--panel2); border-radius:8px; padding:2px 7px; vertical-align:middle; }
  .grp-members { margin-top:16px; border-top:1px solid var(--border); padding-top:6px; }
  .grp-row { display:flex; justify-content:space-between; align-items:center; gap:10px;
             padding:7px 6px; border-radius:7px; font-size:13px; cursor:pointer; }
  .grp-row:hover { background:var(--panel2); }
  /* On highlighted (.cap) bucket cards the card bg is already --panel2, so member rows need a
     lighter hover to stay visible. */
  .card.cap .grp-row:hover { background:#414854; }
  .grp-row .grp-name { color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .grp-row .grp-h { color:var(--green-d); font-variant-numeric:tabular-nums; white-space:nowrap; }
  /* detail */
  .head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
  .left { display:flex; align-items:center; gap:14px; }
  /* Breadcrumbs sit above the page title on detail pages, so the <h1> stays the biggest
     thing on every page — dashboard and drill-in alike. */
  .crumbs { font-size:12px; display:flex; align-items:center; flex-wrap:wrap; gap:8px;
            margin:0 0 8px; color:var(--faint); }
  .crumb { color:var(--blue-d); text-decoration:none; cursor:pointer; }
  .crumb:hover { text-decoration:underline; }
  .crumb-sep { color:#5a616b; }
  .crumb-cur { font-weight:600; }
  button.btn { background:var(--blue); color:#10141a; border:0; border-radius:8px; padding:9px 16px; font-size:14px; font-weight:600; cursor:pointer; }
  button.btn:hover { background:var(--blue-d); }
  button.btn:disabled { background:#434a56; color:#8a929c; cursor:default; }
  .back { background:var(--panel2); color:var(--text); }
  .back:hover { background:#414854; }
  .panel { background:var(--panel); border-radius:12px; padding:26px 30px 36px; box-shadow:0 1px 3px rgba(0,0,0,.4); border:1px solid var(--border); }
  #updated { font-size:12px; color:var(--muted); }
  .chart-box { position:relative; height:480px; margin-top:10px; }
  .muted { color:var(--muted); }
  .hint { font-size:12px; color:var(--muted); margin-top:8px; }
  /* Team Capacity: per-person project donuts under the bar chart */
  .donut-sub { margin:-4px 0 12px; }
  .donut-legend { display:flex; flex-wrap:wrap; gap:8px 18px; margin:0 0 18px; }
  .dl-item { display:inline-flex; align-items:center; gap:7px; font-size:12px; color:var(--muted); }
  .dl-item i { width:11px; height:11px; border-radius:3px; flex:0 0 auto; }
  /* The legend swatch for a real project is a color input: click it for the browser's color
     wheel and that project is recolored everywhere (saved per-browser). The two synthetic
     grey slices stay plain <i> markers. */
  .dl-item input.dl-swatch { -webkit-appearance:none; appearance:none; width:13px; height:13px;
             padding:0; border:0; border-radius:3px; flex:0 0 auto; cursor:pointer;
             background:none; outline:1px solid transparent; }
  .dl-item input.dl-swatch:hover { outline:1px solid var(--text); outline-offset:2px; }
  .dl-item input.dl-swatch::-webkit-color-swatch-wrapper { padding:0; }
  .dl-item input.dl-swatch::-webkit-color-swatch { border:0; border-radius:3px; }
  .dl-item input.dl-swatch::-moz-color-swatch { border:0; border-radius:3px; }
  /* Chart.js paints its tooltip inside the canvas, so on the 170 px donut cards it was clipped
     at the card edge. Those charts use this DOM tooltip instead: fixed-position, above every
     card and the sticky sidebar, and clamped into the viewport. */
  .chart-tip { position:fixed; z-index:200; pointer-events:none; opacity:0; transition:opacity .08s;
               background:var(--panel2); border:1px solid var(--border); border-radius:8px;
               padding:8px 10px; font-size:12px; color:var(--text); max-width:270px;
               box-shadow:0 8px 22px rgba(0,0,0,.6); }
  .chart-tip .tip-h { display:flex; align-items:center; gap:7px; font-weight:600; margin-bottom:3px; }
  .chart-tip .tip-h i { width:10px; height:10px; border-radius:3px; flex:0 0 auto; }
  .chart-tip .tip-l { color:var(--muted); font-variant-numeric:tabular-nums; }
  .donut-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:16px; }
  .donut-card { background:var(--panel2); border:1px solid var(--border); border-radius:12px;
                padding:14px 12px 12px; cursor:pointer; transition:border-color .08s, transform .08s; }
  .donut-card:hover { border-color:var(--blue); transform:translateY(-2px); position:relative; z-index:2; }
  .donut-card h4 { font-size:13px; margin:0 0 8px; text-align:center; overflow:hidden;
                   text-overflow:ellipsis; white-space:nowrap; }
  .donut-box { position:relative; height:170px; }
  .donut-total { margin-top:10px; text-align:center; font-size:12px; color:var(--muted);
                 font-variant-numeric:tabular-nums; }
  /* drill-down task list */
  .drill-head { display:flex; align-items:center; gap:14px; margin-bottom:14px; }
  .drill-head h2 { font-size:16px; margin:0; }   /* same step as .section-h */
  .drill-total { font-size:13px; color:var(--muted); margin:0 0 16px; }
  table.tasks { width:100%; border-collapse:collapse; font-size:14px; }
  table.tasks th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.04em;
                   color:var(--muted); border-bottom:2px solid var(--border); padding:8px 10px; }
  table.tasks th.hours { text-align:right; }
  table.tasks td { padding:9px 10px; border-bottom:1px solid var(--border); vertical-align:top; }
  table.tasks tr.parent td { font-weight:600; }
  table.tasks tr.sub td { font-weight:400; color:var(--muted); }
  /* Daily Log: the date cell spans its day's rows and carries that day's total. A rule
     above each new day's first row keeps the days visually separate without headings. */
  table.daylog td.day { white-space:nowrap; vertical-align:top; color:var(--muted); }
  table.daylog td.day .day-tot { display:block; margin-top:3px; font-size:12px; color:var(--faint);
                                 font-variant-numeric:tabular-nums; }
  /* Rule between days only, not between every row — each day reads as one block. */
  table.daylog tbody td { border-bottom:0; }
  table.daylog tbody tr.day-start td { border-top:1px solid var(--border); }
  table.daylog tbody tr:first-child td { border-top:0; }
  .proj-toggle { display:inline-flex; align-items:center; gap:8px; cursor:pointer; }
  .proj-toggle input { cursor:pointer; margin:0; flex:0 0 auto; }
  .badge { display:inline-block; font-size:11px; padding:2px 8px; border-radius:10px; background:var(--panel2); color:var(--text); }
  .badge.none { color:var(--faint); }   /* the "No status" placeholder badge */
  .sub-name { padding-left:22px; position:relative; }
  .sub-name::before { content:'↳'; position:absolute; left:6px; color:#5a616b; }
  /* Nesting steps for rows in the breakdown tables: .lvl1 is a task under its project
     heading row, .lvl2 a subtask under that task (keeps the ↳ aligned with the indent). */
  .lvl1 { padding-left:22px; }
  .sub-name.lvl2 { padding-left:34px; }
  .sub-name.lvl2::before { left:18px; }
  .hours { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
  /* settings: per-person graph colors */
  .color-list { display:flex; flex-direction:column; gap:2px; }
  .color-row { display:flex; align-items:center; gap:12px; padding:9px 6px; border-bottom:1px solid var(--border); }
  .color-row:last-child { border-bottom:0; }
  .color-row .color-name { flex:1; font-size:14px; }
  .color-pick { width:40px; height:28px; padding:0; border:1px solid var(--border); border-radius:6px;
                background:var(--panel2); cursor:pointer; }
  .reset-one { background:none; border:0; color:var(--blue-d); font-size:12px; cursor:pointer; padding:4px 6px; }
  .reset-one:hover:not(:disabled) { text-decoration:underline; }
  .reset-one:disabled { color:var(--faint); cursor:default; }
  .color-actions { margin-top:18px; }
</style>
</head>
<body>
<div class="wrap" id="app"></div>
<script>
const app = document.getElementById('app');
let chart = null;
// Small per-person donuts drawn under the Team Capacity bar charts. Tracked as a set so
// every re-render tears all of them down alongside the main chart.
let donutCharts = [];
function destroyCharts(){
  if (chart) { chart.destroy(); chart = null; }
  donutCharts.forEach(c => c.destroy());
  donutCharts = [];
  hideChartTip();   // a chart can be torn down mid-hover, leaving its tooltip stranded
}

// One shared DOM tooltip for charts whose canvas is too small to hold Chart.js's own
// (the per-person donuts). Enable per-chart with:
//   tooltip: { enabled:false, external:externalTip, callbacks:{...} }
let _chartTip = null;
function chartTipEl(){
  if (!_chartTip) {
    _chartTip = document.createElement('div');
    _chartTip.className = 'chart-tip';
    document.body.appendChild(_chartTip);
  }
  return _chartTip;
}
function hideChartTip(){ if (_chartTip) _chartTip.style.opacity = 0; }
function externalTip(ctx){
  const tt = ctx.tooltip;
  if (!tt || !tt.opacity) { hideChartTip(); return; }
  const el = chartTipEl();
  const swatch = (tt.labelColors && tt.labelColors[0] && tt.labelColors[0].backgroundColor) || C.muted;
  const lines = [];
  (tt.body || []).forEach(b => lines.push(...(b.before || []), ...(b.lines || []), ...(b.after || [])));
  el.innerHTML =
    `<div class="tip-h"><i style="background:${swatch}"></i>${esc((tt.title || [])[0] || '')}</div>` +
    lines.filter(l => l !== '').map(l => `<div class="tip-l">${esc(l)}</div>`).join('');
  el.style.opacity = 1;
  // Sit to the right of the caret, flipping to the left and clamping vertically so a ring at
  // the edge of the grid can't push the tooltip off-screen.
  const r = ctx.chart.canvas.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
  let x = r.left + tt.caretX + 14;
  if (x + w > window.innerWidth - 8) x = r.left + tt.caretX - w - 14;
  el.style.left = Math.max(8, x) + 'px';
  el.style.top = Math.min(Math.max(8, r.top + tt.caretY - h / 2), window.innerHeight - h - 8) + 'px';
}

// The :root palette, mirrored for canvas drawing (Chart.js can't read CSS variables).
// Keep these in lockstep with the --blue/--green/... custom properties in <style>.
const C = {
  blue:'#6aa9e0', blueD:'#9cc7f0', green:'#4cc085', greenD:'#6cd49d', red:'#e26b66',
  panel:'#2a2f38', panel2:'#353b45', border:'#3c4350', muted:'#a3aab4', faint:'#737b86',
  amber:'#f0c674',   // reserved for capacity/budget markers only — never a data series
};

// Dark-mode chart defaults: light tick/legend text and faint gridlines.
Chart.defaults.color = '#9aa0a8';
Chart.defaults.borderColor = 'rgba(255,255,255,.08)';

// Draws a dashed horizontal target line (e.g. the 128 h per-assignee cap) on a bar chart.
// Enable per-chart via options.plugins.capLine = { value: <hours> }.
// `pace` adds a second, amber line below the target: where a person should already be today,
// given how far through the work month the range is. Labels sit above their own line.
const capLinePlugin = {
  id: 'capLine',
  afterDatasetsDraw(c) {
    const cfg = c.options.plugins.capLine;
    if (!cfg) return;
    const { left, right } = c.chartArea, ctx = c.ctx;
    const rule = (value, color, label) => {
      const y = c.scales.y.getPixelForValue(value);
      ctx.save();
      ctx.beginPath(); ctx.setLineDash([6, 4]); ctx.lineWidth = 2; ctx.strokeStyle = color;
      ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
      ctx.setLineDash([]); ctx.fillStyle = color; ctx.font = '600 12px sans-serif'; ctx.textAlign = 'right';
      ctx.fillText(label, right - 6, y - 6);
      ctx.restore();
    };
    if (cfg.value != null) rule(cfg.value, C.blueD, h2(cfg.value) + ' h target');
    if (cfg.pace != null) rule(cfg.pace, C.amber, cfg.paceLabel || (h2(cfg.pace) + ' h by today'));
  }
};
Chart.register(capLinePlugin);

// Team Capacity · Logged bar color: measured against the PACE target (where a person should
// be by today), not the whole month's cap — that is the number a PM acts on mid-month. Green
// means keeping pace: at or above the target, or no more than PACE_TOLERANCE hours short of
// it. Red means far enough behind to need attention.
const PACE_TOLERANCE = 5;
const capColorPace = (h, paceTarget) => h >= paceTarget - PACE_TOLERANCE ? C.green : C.red;
// Team Capacity · Estimated uses a flat threshold instead of the tolerance band: a person
// carrying more than CAP_GREEN_MIN remaining hours is booked up (green); anyone below it has
// room and needs work assigned (red).
const CAP_GREEN_MIN = 80;
const capColorEst = h => h > CAP_GREEN_MIN ? C.green : C.red;

// Label for task rows that sit in no status column; used by the status filters and shown
// as a muted badge wherever a status column would be.
const NO_STATUS = 'No status';
const r2 = v => Math.round(v * 100) / 100;

// Draws a per-bar monthly-budget marker: a short amber line across each bar that
// has a cap. Enable via options.plugins.capMarks = { caps: [<cap or null per bar>] }.
const capMarksPlugin = {
  id: 'capMarks',
  afterDatasetsDraw(c) {
    const cfg = c.options.plugins.capMarks;
    if (!cfg || !cfg.caps) return;
    const meta = c.getDatasetMeta(0), y = c.scales.y, ctx = c.ctx;
    ctx.save();
    cfg.caps.forEach((cap, i) => {
      const bar = meta.data[i];
      if (cap == null || !bar) return;
      const half = (bar.width || 18) / 2 + 2, py = y.getPixelForValue(cap);
      ctx.beginPath(); ctx.lineWidth = 2.5; ctx.strokeStyle = C.amber;
      ctx.moveTo(bar.x - half, py); ctx.lineTo(bar.x + half, py); ctx.stroke();
    });
    ctx.restore();
  }
};
Chart.register(capMarksPlugin);

function fmtErr(e){ return '<p class="note">Error: '+e+'</p>'; }
function esc(s){ return (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function h2(x){ return Number(x || 0).toFixed(2); }   // always two decimals: 40.00, 40.50, 40.75
// One phrasing for every loading / empty / nothing-matches state on every tab.
function note(text){ return `<p class="note">${esc(text)}</p>`; }
function noteBox(text){ return `<p class="note box">${esc(text)}</p>`; }
const LOADING = 'Loading…';
// "1 project" / "3 projects" — used anywhere a count is read out in prose.
function plural(n, word, wordPlural){ return `${n} ${n === 1 ? word : (wordPlural || word + 's')}`; }
// Headline stat banner shown at the top of every tab. stats: [{n, l, neg?}].
// kind 'est' paints the numbers blue (estimated hours), anything else green (logged hours).
function summaryBar(stats, kind){
  return `<div class="summary-bar${kind === 'est' ? ' est' : ''}">` +
    stats.map(s => `<div class="summary-stat"><span class="n${s.neg ? ' neg' : ''}">${esc(String(s.n))}</span>` +
      `<span class="l">${esc(s.l)}</span></div>`).join('') + '</div>';
}

// Stable per-person colors, shared across the estimated & actual per-person stacked charts,
// so the same employee keeps one color everywhere within a session.
// Well-separated categorical hues, one per color family, so stacked segments never blend.
// Deliberately NO orange/amber/gold — those get lost against the amber capacity marker (#f0c674).
const PERSON_PALETTE = [
  '#1f77b4', // blue
  '#d62728', // red
  '#2ca02c', // green
  '#9467bd', // purple
  '#17becf', // cyan
  '#e377c2', // pink
  '#8c564b', // brown
  '#7f7f7f', // gray
  '#393b79', // deep indigo
  '#a55194', // plum
];
// Unassigned is always grey — it isn't a person, so it shouldn't take a person's hue. Seeded
// into _personColors so the grey is reserved up front and auto-assignment can't hand it out.
// Per-person preferences live in the Graph Colors tab, not here.
const _personColors = { 'Unassigned': '#7f7f7f' };
let _personColorN = 0;
// User-picked colors (Settings tab), persisted per-browser. These win over the built-in
// defaults/auto-assignment so a chosen color applies everywhere the same person is drawn.
const PERSON_COLOR_STORE = 'personColors.v1';
function loadPersonColorConfig(){
  try { return JSON.parse(localStorage.getItem(PERSON_COLOR_STORE)) || {}; } catch (e) { return {}; }
}
function savePersonColorConfig(){
  try { localStorage.setItem(PERSON_COLOR_STORE, JSON.stringify(personColorConfig)); } catch (e) {}
}
let personColorConfig = loadPersonColorConfig();
// Every name we've been asked to color this session, so the Settings tab can list everyone
// (seeded with the known team + Unassigned so they always show even before any chart loads).
const _seenPeople = new Set([...TEAM_MEMBERS, 'Unassigned']);
function personColor(name){
  _seenPeople.add(name);
  if (name in personColorConfig) return personColorConfig[name];   // user choice wins
  if (name in _personColors) return _personColors[name];
  // Skip palette entries already taken (by an override, a user color, or an earlier person).
  const used = new Set([...Object.values(_personColors), ...Object.values(personColorConfig)]);
  while (used.size < PERSON_PALETTE.length && used.has(PERSON_PALETTE[_personColorN % PERSON_PALETTE.length])) _personColorN++;
  return (_personColors[name] = PERSON_PALETTE[_personColorN++ % PERSON_PALETTE.length]);
}
// Default color for a person, ignoring any user override (used by the Settings "Reset" action
// to show what they'd revert to).
function personDefaultColor(name){
  if (name in _personColors) return _personColors[name];
  const used = new Set([...Object.values(_personColors), ...Object.values(personColorConfig)]);
  while (used.size < PERSON_PALETTE.length && used.has(PERSON_PALETTE[_personColorN % PERSON_PALETTE.length])) _personColorN++;
  return (_personColors[name] = PERSON_PALETTE[_personColorN++ % PERSON_PALETTE.length]);
}
// Build stacked Chart.js datasets (one per person) from project rows. hoursOf(row) returns
// { person: hours } for that project; persons are ordered by grand total (biggest at the bottom).
function personStacks(rows, hoursOf){
  const maps = rows.map(hoursOf), totals = {};
  maps.forEach(m => Object.entries(m).forEach(([p, h]) => { totals[p] = (totals[p] || 0) + h; }));
  const persons = Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
  // An over-run task leaves a person with negative remaining hours. Stacking that below zero
  // drags the axis into the negatives and shrinks every other bar, so segments floor at 0;
  // tooltips already skip zero-height segments.
  return persons.map(p => ({ label: p, data: maps.map(m => Math.max(0, Math.round((m[p] || 0) * 100) / 100)),
    backgroundColor: personColor(p), borderColor: personColor(p), borderWidth: 0 }));
}
// Donut slice colors get their own palette, not the 10-color person palette: there are more
// projects than people, and reusing PERSON_PALETTE meant the eleventh project wrapped around
// and repeated a color. This list is longer than the project roster so every project in a
// ring is a different color, ordered most-distinct-first because slices are colored in
// descending-hours order. No greys (the two synthetic slices below own those) and no near-
// duplicates of the fixed colors in PROJECT_COLORS.
const PROJECT_PALETTE = [
  '#1f77b4', // blue
  '#d62728', // red
  '#9467bd', // purple
  '#17becf', // cyan
  '#ff7f0e', // orange
  '#e377c2', // pink
  '#8c564b', // brown
  '#bcbd22', // olive
  '#393b79', // deep indigo
  '#66c2a5', // seafoam
  '#a55194', // plum
  '#e7969c', // salmon
  '#6b6ecf', // periwinkle
  '#8ca252', // moss
  '#ce6dbd', // orchid
  '#ad494a', // brick
  '#c49c94', // tan
  '#5254a3', // blue-violet
  '#b5cf6b', // pale olive
  '#e6550d', // burnt orange
  '#9c9ede', // pale periwinkle
  '#f7b6d2', // light pink
  '#843c39', // oxblood
  '#9edae5', // pale cyan
  '#756bb1', // muted violet
  '#fdae6b', // apricot
];
// Projects with a color chosen by hand rather than taken from the palette above. These are
// reserved: the auto-assignment skips them so nothing else can land on the same hue.
const PROJECT_COLORS = {
  'Georgia Grown Market MSA': '#2ca02c',   // green
};
// User-picked project colors, set by clicking a swatch in a donut legend and persisted
// per-browser, exactly like the per-person colors. A choice here wins over the pinned
// PROJECT_COLORS and over auto-assignment, so the project reads that color everywhere.
const PROJECT_COLOR_STORE = 'projectColors.v1';
function loadProjectColorConfig(){
  try { return JSON.parse(localStorage.getItem(PROJECT_COLOR_STORE)) || {}; } catch (e) { return {}; }
}
function saveProjectColorConfig(){
  try { localStorage.setItem(PROJECT_COLOR_STORE, JSON.stringify(projectColorConfig)); } catch (e) {}
}
let projectColorConfig = loadProjectColorConfig();
// Stable per-project colors for the Team Capacity donuts, so one project reads as the same
// color in every person's ring.
const _projColors = {};
let _projColorN = 0;
function projectColor(name){
  if (name in projectColorConfig) return projectColorConfig[name];   // user choice wins
  if (name in PROJECT_COLORS) return PROJECT_COLORS[name];
  if (name in _projColors) return _projColors[name];
  // Skip palette entries already spoken for (a fixed color, a user choice, or an earlier
  // project), so two projects never share a hue while any unused one is left.
  const used = new Set([...Object.values(_projColors), ...Object.values(PROJECT_COLORS),
                        ...Object.values(projectColorConfig)]);
  while (used.size < PROJECT_PALETTE.length && used.has(PROJECT_PALETTE[_projColorN % PROJECT_PALETTE.length])) _projColorN++;
  return (_projColors[name] = PROJECT_PALETTE[_projColorN++ % PROJECT_PALETTE.length]);
}
// Claim a color for every configured project up front, in config order, so the assignment
// never depends on which tab drew first: a project is the same color on the Estimated donuts
// and the Logged donuts, in every person's ring, for the whole session.
PROJECT_ROSTER.forEach(projectColor);

// Fold a { project: hours } map into donut slices: biggest first, with everything past the
// top DONUT_MAX_SLICES rolled into a single "Other projects" slice so small rings stay legible.
const DONUT_MAX_SLICES = 7;
const OTHER_SLICE = 'Other projects';
// Filler slice: the part of a person's monthly capacity no project has claimed yet, so the
// ring reads against the full 128 h target instead of only against their own workload.
const FREE_SLICE = 'Unallocated capacity';
// The two synthetic slices get fixed greys and always sort last in the legend.
function sliceColor(label){
  if (label === OTHER_SLICE) return '#5a616b';
  if (label === FREE_SLICE) return '#434a56';
  return projectColor(label);
}
// Donut rings are drawn on .donut-card, whose background is --panel2 — the slice borders
// must match that surface, not the darker --panel used by the outer cards.
const DONUT_BORDER = C.panel2;
const sliceRank = label => label === FREE_SLICE ? 2 : label === OTHER_SLICE ? 1 : 0;
function donutSlices(map){
  const all = Object.entries(map).filter(([, h]) => h > 0).sort((a, b) => b[1] - a[1]);
  if (all.length <= DONUT_MAX_SLICES + 1) return all.map(([label, hours]) => ({ label, hours }));
  const top = all.slice(0, DONUT_MAX_SLICES).map(([label, hours]) => ({ label, hours }));
  top.push({ label: OTHER_SLICE, hours: all.slice(DONUT_MAX_SLICES).reduce((a, x) => a + x[1], 0) });
  return top;
}

// Repaint every live donut from the current project colors, so a color picked in the legend
// takes effect immediately without rebuilding the tab.
function recolorDonuts(){
  donutCharts.forEach(c => {
    c.data.datasets[0].backgroundColor = c.data.labels.map(sliceColor);
    c.update('none');
  });
}

// A grid of one donut per person, showing how that person's hours split across projects.
// rows: [{ name, total, slices:[{label, hours}], caption? }] — total is what the ring adds up
// to (the capacity target when a FREE_SLICE is present), caption overrides the line beneath.
// Colors are shared across every ring, so a single legend above the grid covers them all.
// Clicking a ring calls onPick(row).
function donutGrid(container, rows, opts){
  const o = opts || {};
  rows = rows.filter(r => r.slices.length);
  if (!rows.length) return;
  // Legend lists every project drawn below, heaviest overall first, with the synthetic
  // "Other"/"Unallocated" slices pinned to the end.
  const totals = {};
  rows.forEach(r => r.slices.forEach(s => { totals[s.label] = (totals[s.label] || 0) + s.hours; }));
  // A real project's swatch is a color input (click = color wheel, saved to localStorage);
  // the synthetic "Other"/"Unallocated" slices keep their fixed grey marker.
  const legendKeys = Object.keys(totals)
    .sort((a, b) => sliceRank(a) - sliceRank(b) || totals[b] - totals[a]);
  // The swatch carries an index, not the project name, so names with markup-significant
  // characters can't break the attribute or the lookup.
  const legend = legendKeys
    .map((p, i) => `<span class="dl-item">${sliceRank(p)
        ? `<i style="background:${sliceColor(p)}"></i>`
        : `<input type="color" class="dl-swatch" value="${sliceColor(p)}" data-pi="${i}"
                  title="Change the color of ${esc(p)}">`}${esc(p)}</span>`)
    .join('');
  container.insertAdjacentHTML('beforeend',
    `<h2 class="section-h">${esc(o.title || 'By project, per person')}</h2>
     ${o.sub ? `<p class="hint donut-sub">${esc(o.sub)}</p>` : ''}
     <div class="donut-legend">${legend}</div>
     <div class="donut-grid">${rows.map((r, i) =>
       `<div class="donut-card" data-di="${i}">
          <h4>${esc(r.name)}</h4>
          <div class="donut-box"><canvas id="donut-${i}"></canvas></div>
          <div class="donut-total">${esc(r.caption || `${h2(r.total)} h · ${plural(r.slices.length, 'project')}`)}</div>
        </div>`).join('')}</div>`);
  // Picking a color repaints every ring in place (and the swatch itself) — no reload, and the
  // choice sticks for the next session.
  container.querySelectorAll('input.dl-swatch').forEach(inp => {
    inp.oninput = () => {
      projectColorConfig[legendKeys[+inp.dataset.pi]] = inp.value;
      saveProjectColorConfig();
      recolorDonuts();
    };
  });
  rows.forEach((r, i) => {
    const el = document.getElementById('donut-' + i);
    if (!el) return;
    const colors = r.slices.map(s => sliceColor(s.label));
    donutCharts.push(new Chart(el, {
      type: 'doughnut',
      data: { labels: r.slices.map(s => s.label),
              datasets: [{ data: r.slices.map(s => r2(s.hours)), backgroundColor: colors,
                           borderColor: DONUT_BORDER, borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: '58%',
        plugins: { legend: { display: false },
          // DOM tooltip: the canvas is far too small to hold Chart.js's own without clipping it.
          tooltip: { enabled: false, external: externalTip, callbacks: {
            title: items => items.length ? items[0].label : '',
            label: ctx => `${h2(ctx.parsed)} h · ${(ctx.parsed / (r.total || 1) * 100).toFixed(0)}% of ${h2(r.total)} h` } } } }
    }));
  });
  // Clicking anywhere on a card opens that person's existing drill-in.
  if (o.onPick) container.querySelectorAll('.donut-card').forEach(c =>
    c.onclick = () => o.onPick(rows[+c.dataset.di]));
}

// The budget bar is drawn as CAP_BLOCKS discrete blocks (10% each): a block lights up for
// every whole 10% of the monthly capacity spent, so the bar can be read as a count.
const CAP_BLOCKS = 10, CAP_BLOCK_PCT = 100 / CAP_BLOCKS;
// True once a project/bucket has spent its whole monthly budget — the gold-tint trigger.
const atCapacity = (hours, cap) => cap > 0 && Number(hours || 0) >= cap;
function capBar(hours, cap){
  if (cap == null) return '';   // only projects with a monthly capacity get a bar
  const pct = cap > 0 ? (hours / cap) * 100 : 0;
  const over = pct > 100;
  const on = Math.max(0, Math.min(CAP_BLOCKS, Math.floor(pct / CAP_BLOCK_PCT)));
  let blocks = '';
  for (let i = 0; i < CAP_BLOCKS; i++) blocks += `<i class="blk${i < on ? ' on' : ''}"></i>`;
  const tone = over ? ' over' : (pct >= 100 ? ' at' : '');
  return `<div class="cap-bar${tone}">
      <div class="track">${blocks}</div>
      <div class="lab">${h2(hours)} / ${h2(cap)} h used · ${pct.toFixed(0)}%${
        over ? ' — over capacity' : (pct >= 100 ? ' — at capacity' : '')}</div>
    </div>`;
}
function setCrumbs(items) {
  // items: [{label, fn?}] — entries with fn render as links; the last/plain one is the current page.
  const el = document.getElementById('crumbs');
  if (!el) return;
  el.innerHTML = items.map((it, i) => {
    const last = i === items.length - 1;
    return (last || !it.fn)
      ? `<span class="crumb-cur">${esc(it.label)}</span>`
      : `<a href="#" class="crumb" data-ci="${i}">${esc(it.label)}</a>`;
  }).join('<span class="crumb-sep">›</span>');
  el.querySelectorAll('a.crumb').forEach(a => {
    a.onclick = (e) => { e.preventDefault(); items[+a.dataset.ci].fn(); };
  });
}

// Archived projects (injected by both deploy paths): still listed on the card/list tabs, but
// under their own section and out of the stats above it.
const ARCHIVED = new Set(ARCHIVED_GIDS);
const isArchived = w => ARCHIVED.has(w.gid);

// Which dashboard tab is active; remembered across renders/drill-ins.
let dashTab = 'team';   // Team Capacity is the default tab
// Estimated-hours Bar Chart filters, remembered across renders/tab switches.
let estStatusFilter = null;    // Set of enabled status columns; null = show all statuses
let estHideUnassigned = false; // when true, drop the Unassigned assignee from the chart
// Bar-chart drill-in: name of the budget group (e.g. "CMD") whose combined bucket has been
// clicked open into its member projects; null = show the combined bucket. One per chart.
let estDrillGroup = null;
let actualDrillGroup = null;
// Projects unchecked in the "By project" summary tables — hidden from the bar chart above only
// (the table still lists them so they can be re-checked). Keyed by the row's display name.
let estHiddenProjects = new Set();
let actualHiddenProjects = new Set();
// Team Capacity: hide the Unassigned bar by default; toggle remembered across renders.
let teamShowUnassigned = false;
// Team Capacity status-column filter; null = all statuses. Applies to the bars and the drill-in.
let teamStatusFilter = null;
// Task List: filter to a single assignee (person who logged the time); null = show everyone.
let itemFilterPerson = null;
// Daily Log: filter to a single assignee; null = show everyone. Kept separate from the Task
// List filter so switching between the two tabs doesn't clobber either selection.
let dailyFilterPerson = null;
// Display name behind the PAT (/api/me), so a filter can default to "you". Stays null if the
// lookup fails, which just means the filters open on everyone.
let currentUser = null;
// Whether Daily Log has already applied its "you" default. Set once the tab first has data,
// and by the dropdown, so a deliberate choice is never overwritten later in the session.
let dailyFilterInit = false;
// Daily Log: newest day first by default (a PM reads today's work first); toggled by the
// Order control.
let dailyNewestFirst = true;
// Selected date range (YYYY-MM-DD) for the "Actual Hours" view; shared by the tab and drill-in.
// Defaults to the current calendar month so the dashboard opens on "this month" every time.
function monthRange(now){ const d = now || new Date(), p = n => String(n).padStart(2, '0'),
  y = d.getFullYear(), m = d.getMonth(),
  first = `${y}-${p(m+1)}-01`, last = `${y}-${p(m+1)}-${p(new Date(y, m+1, 0).getDate())}`;
  return [first, last]; }
let [dateStart, dateEnd] = monthRange();
function fmtDate(d){ const [y,m,day]=d.split('-').map(Number); return new Date(y, m-1, day).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}); }
function rangeLabel(s, e){ return fmtDate(s) + ' – ' + fmtDate(e); }
// How far through the *working* part of the selected range today sits. Mon–Fri only, so a
// month's 30 calendar days become ~22 billable ones; no holiday calendar, so a week with a
// holiday in it still counts five days. Today counts as elapsed once it starts, which is the
// reading a PM wants at a glance ("we're 12 of 22 days in").
//   { total, elapsed, left, pct } — pct is 0 before the range opens, 100 once it has closed,
//   and doubles as the share of the monthly target that should already be logged.
function workdayProgress(start, end, today){
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const isWork = d => d.getDay() !== 0 && d.getDay() !== 6;
  const s = parse(start), e = parse(end);
  const now = today || new Date();
  const t = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  let total = 0, elapsed = 0;
  for (const d = new Date(s); d <= e; d.setDate(d.getDate() + 1)) {
    if (!isWork(d)) continue;
    total++;
    if (d <= t) elapsed++;
  }
  return { total, elapsed, left: total - elapsed,
           pct: total ? elapsed / total * 100 : 0 };
}
// The single filter row every Actual-Hours tab opens with: the date range, plus whatever
// extra controls that tab needs appended after a separator — one toolbar, never two stacked.
function rangePicker(extra){
  return `<div class="toolbar">
      <span class="tb-group"><span class="tb-label">Date range</span>
        <input type="date" id="d-start" value="${dateStart}" aria-label="From">
        <span>to</span>
        <input type="date" id="d-end" value="${dateEnd}" aria-label="To">
        <button class="btn" id="range-go">Search</button></span>
      ${extra ? '<span class="tb-sep"></span>' + extra : ''}
    </div>`;
}
// Sidebar labels stay short (they read under their section heading); the page <h1> spells
// out which half of the dashboard you're on, because several tabs share a short label.
// Every tab carries a one-line `sub` so no page opens without saying what it shows.
const TABS = {
  team: { label:'Team Capacity', title:'Team Capacity · Estimated',
    sub:'Remaining estimated hours per person against the monthly target. Click a bar or ring for their full breakdown.' },
  estproj: { label:'Bar Chart', title:'Projects · Estimated',
    sub:'Remaining estimated hours per project, stacked by the person assigned.' },
  estimated: { label:'Project Cards', title:'Project Cards · Estimated',
    sub:'One card per project: estimated hours still on the board and how many tasks they sit in.' },
  teamactual: { label:'Team Capacity', title:'Team Capacity · Logged',
    sub:'Hours actually logged per person in the selected range, against the monthly target. The amber line is where each person should be today, based on how far through the work month (Mon–Fri) we are — a bar is green when it is at that line or within 5 h of it, red when it falls further behind.' },
  capacity: { label:'MSA Project Capacity', title:'MSA Project Capacity · Logged',
    sub:'Hours logged against each retainer budget for the selected range, with what is left.' },
  actualproj: { label:'Bar Chart', title:'Projects · Logged',
    sub:'Hours logged per project in the selected range, stacked by the person who logged them.' },
  actualitems: { label:'Task List', title:'Task List · Logged',
    sub:'Every task worked on in the selected range, grouped by project.' },
  actualdaily: { label:'Daily Log', title:'Daily Log · Logged',
    sub:'Day by day, what each person logged their time to in the selected range.' },
  settings: { label:'Graph Colors', title:'Graph Colors',
    sub:'Pick a color for each person — saved in this browser and applied to every per-person chart.' },
};
// Sidebar groups: estimated/planned views vs. logged-hours & progress views. `tone` picks the
// section's accent — 'est' = blue, 'act' = green, omitted for neutral (Settings).
const NAV_SECTIONS = [
  { title: 'Estimated Hours', tone: 'est', tabs: ['team', 'estproj', 'estimated'] },
  { title: 'Actual Hours', tone: 'act', tabs: ['teamactual', 'capacity', 'actualproj', 'actualitems', 'actualdaily'] },
  { title: 'Settings', tabs: ['settings'] },
];

// Shared left nav, reused by the dashboard and the drill-in detail pages so the
// sidebar always stays put. The active item tracks the last-opened tab (dashTab).
function sidebarHtml() {
  return `<nav class="sidebar">
      <div class="brand">Creative Hours</div>
      ${NAV_SECTIONS.map(sec =>
        `<div class="nav-sec${sec.tone ? ' ' + sec.tone : ''}">` +
          `<div class="nav-section">${sec.title}</div><div class="nav-group">` +
          sec.tabs.map(k => `<a href="#" class="nav-item${k === dashTab ? ' active' : ''}" data-tab="${k}">${TABS[k].label}</a>`).join('') +
        '</div></div>'
      ).join('')}
    </nav>`;
}
// Wire nav clicks. onSwitch (dashboard) switches tab in place; on a detail page we
// set the tab and navigate home, where the dashboard renders it.
function wireSidebar(onSwitch) {
  document.querySelectorAll('.nav-item').forEach(a => {
    a.onclick = (e) => {
      e.preventDefault();
      dashTab = a.dataset.tab;
      if (onSwitch) onSwitch(); else location.hash = '';
    };
  });
}

function estCard(w) {
  const c = document.createElement('div');
  c.className = 'card';
  c.innerHTML = `<h3>${esc(w.name)}</h3>
    <div class="stats">
      <div class="stat"><div class="n">${h2(w.hours)}</div><div class="l">Est. Hours</div></div>
      <div class="stat"><div class="n">${w.ntasks}</div><div class="l">Tasks</div></div>
      ${w.cap != null ? `<div class="stat"><div class="n">${h2(w.cap)}</div><div class="l">Capacity h/mo</div></div>` : ''}
    </div>`;
  c.onclick = () => { location.hash = '#/p/' + w.gid; };
  return c;
}

function loggedCard(w) {
  const c = document.createElement('div');
  c.className = 'card logged';
  c.innerHTML = `<h3>${esc(w.name)}</h3>
    <div class="stats">
      <div class="stat"><div class="n">${h2(w.hours)}</div><div class="l">Hours Logged</div></div>
      <div class="stat"><div class="n">${w.nentries}</div><div class="l">Time Entries</div></div>
      <div class="stat"><div class="n">${w.completed}</div><div class="l">Completed Tasks</div></div>
    </div>`;
  c.onclick = () => { location.hash = '#/logged/' + w.gid; };
  return c;
}

function capCard(w) {
  // All logged hours count against a project's monthly capacity.
  const used = Number(w.hours || 0), cap = Number(w.cap || 0);
  const remaining = cap - used;
  const c = document.createElement('div');
  c.className = 'card logged' + (atCapacity(used, cap) ? ' at-cap' : '');
  c.innerHTML = `<h3>${esc(w.name)}</h3>
    <div class="stats">
      <div class="stat"><div class="n">${h2(w.cap)}</div><div class="l">Capacity h/mo</div></div>
      <div class="stat"><div class="n">${h2(used)}</div><div class="l">Hours used</div></div>
      <div class="stat"><div class="n${remaining < 0 ? ' neg' : ''}">${h2(remaining)}</div><div class="l">${remaining < 0 ? 'Over' : 'Remaining'}</div></div>
    </div>
    ${capBar(used, cap)}`;
  c.onclick = () => { location.hash = '#/logged/' + w.gid; };
  return c;
}

// Roll up a budget group's member projects (from the current Hours-Logged data)
// into one combined bucket: summed hours, plus the per-member rows.
function buildGroupSummary(g, jd) {
  const members = g.gids.map(gid => jd.find(w => w.gid === gid)).filter(Boolean);
  return {
    name: g.name, cap: g.cap, members,
    hours: members.reduce((a, w) => a + (w.hours || 0), 0),
    nentries: members.reduce((a, w) => a + (w.nentries || 0), 0),
    updated: members.length ? members[0].updated : '',
  };
}

function groupCard(g) {
  // Combined monthly bucket shared by several projects; all logged hours count.
  const used = Number(g.hours || 0), cap = Number(g.cap || 0);
  const remaining = cap - used;
  const c = document.createElement('div');
  c.className = 'card logged grp-card' + (atCapacity(used, cap) ? ' at-cap' : '');
  const rows = g.members.map(m =>
    `<div class="grp-row" data-gid="${m.gid}" title="Open ${esc(m.name)}">
       <span class="grp-name">${esc(m.name)}</span>
       <span class="grp-h">${h2(m.hours)} h</span>
     </div>`).join('') || '<div class="grp-row"><span class="grp-name muted">No member data in range.</span></div>';
  c.innerHTML = `<h3>${esc(g.name)} <span class="grp-tag">combined bucket</span></h3>
    <div class="stats">
      <div class="stat"><div class="n">${h2(g.cap)}</div><div class="l">Capacity h/mo</div></div>
      <div class="stat"><div class="n">${h2(used)}</div><div class="l">Hours used</div></div>
      <div class="stat"><div class="n${remaining < 0 ? ' neg' : ''}">${h2(remaining)}</div><div class="l">${remaining < 0 ? 'Over' : 'Remaining'}</div></div>
    </div>
    ${capBar(used, cap)}
    <div class="grp-members">${rows}</div>
    <div class="go">Open bucket · hours by project →</div>`;
  // Each member row opens that project's own Hours-Logged detail; the card itself opens the
  // bucket's own page, where every member project sits in one chart.
  c.querySelectorAll('.grp-row[data-gid]').forEach(r =>
    r.onclick = (e) => { e.stopPropagation(); location.hash = '#/logged/' + r.dataset.gid; });
  c.onclick = () => { location.hash = '#/grp/' + encodeURIComponent(g.name); };
  return c;
}

async function renderDashboard() {
  destroyCharts();
  app.innerHTML = `
    <div class="layout">
      ${sidebarHtml()}
      <main class="content">
        <div class="head">
          <h1 id="tab-title"></h1>
          <div class="head-right">
            <span id="dash-updated" class="dash-updated"></span>
            <button class="btn" id="dash-refresh">Refresh</button>
          </div>
        </div>
        <p class="sub" id="tab-sub"></p>
        <div id="tabview"><p class="note">Loading…</p></div>
      </main>
    </div>`;
  const view = document.getElementById('tabview');
  const btn = document.getElementById('dash-refresh');
  let estData = null, loggedData = null, teamData = null;   // cached so switching tabs is instant
  let groupsConfig = null;   // budget-group definitions (loaded once); combined per current range
  let personStatsCache = {};   // Actual Hours per-person totals, keyed by 'start:end'
  let projPersonCache = {};    // per-project person split { gid: { person: hours } }, keyed by 'start:end'
  let itemStatsCache = {};     // Actual Hours per-item (task) totals, keyed by 'start:end'
  let dailyStatsCache = {};    // Actual Hours per-person/per-day task rows, keyed by 'start:end'
  let personLoading = {};      // in-flight guard so the chart + summary don't double-fetch

  // Wire the date-range Search button (if the current tab rendered one). Editing the date
  // fields does nothing until Search is clicked (or Enter is pressed in a field).
  function wireRangeSel() {
    const s = document.getElementById('d-start'), e = document.getElementById('d-end'),
          go = document.getElementById('range-go');
    if (!go) return;
    const apply = () => {
      if (s.value) dateStart = s.value;
      if (e.value) dateEnd = e.value;
      loggedData = null; renderTab(); loadLogged(false);
    };
    go.onclick = apply;
    [s, e].forEach(inp => inp.onkeydown = ev => { if (ev.key === 'Enter') apply(); });
  }

  function cardGrid(items, cardFn, empty, toolbar) {
    view.innerHTML = toolbar || '';
    if (!items.length) { view.insertAdjacentHTML('beforeend', note(empty)); }
    else {
      const grid = document.createElement('div');
      grid.className = 'grid';
      items.forEach(w => grid.appendChild(cardFn(w)));
      view.appendChild(grid);
    }
    wireRangeSel();
  }

  function renderEstProj() {
    // Stacked bar chart of REMAINING hours per project (estimated − actual, per task), split by
    // assignee, with filters for the status column and for hiding Unassigned. Caps/gids come from
    // estData; the per-person split and per-status breakdown are inverted out of teamData.breakdown's
    // task rows — the same estimated − actual math the Team Capacity chart uses.
    if (!estData || !teamData) { view.innerHTML = note(LOADING); return; }
    // Every status column seen in the data → drives the filter checkboxes.
    const statusList = statusColumns(), allStatuses = new Set(statusList);
    // Forget any remembered status that no longer exists; an empty set collapses back to "all".
    if (estStatusFilter) {
      estStatusFilter = new Set([...estStatusFilter].filter(s => allStatuses.has(s)));
      if (!estStatusFilter.size) estStatusFilter = null;
    }
    const statusOn = s => !estStatusFilter || estStatusFilter.has(s);

    // Toolbar: one checkbox per status column, then an "Exclude Unassigned" toggle.
    const toolbar =
      '<div class="toolbar est-toolbar">' +
        '<span class="tb-label">Status</span>' +
        statusList.map(s =>
          `<label class="chk"><input type="checkbox" class="est-status" value="${esc(s)}" ${statusOn(s) ? 'checked' : ''}>${esc(s)}</label>`
        ).join('') +
        '<span class="tb-sep"></span>' +
        `<label class="chk"><input type="checkbox" id="est-hide-unassigned" ${estHideUnassigned ? 'checked' : ''}>Exclude Unassigned</label>` +
      '</div>';
    const wireFilters = () => {
      view.querySelectorAll('.est-status').forEach(cb => cb.onchange = () => {
        const on = [...view.querySelectorAll('.est-status')].filter(c => c.checked).map(c => c.value);
        estStatusFilter = (on.length === statusList.length) ? null : new Set(on);
        renderEstProj();
      });
      const hu = view.querySelector('#est-hide-unassigned');
      if (hu) hu.onchange = () => { estHideUnassigned = hu.checked; renderEstProj(); };
    };

    // Aggregate remaining hours per project from the filtered task rows: total, item count,
    // and the per-person split for the stacked bars.
    const capOf = Object.fromEntries(estData.map(w => [w.name, w.cap == null ? null : w.cap]));
    const gidOf = Object.fromEntries(estData.map(w => [w.name, w.gid]));
    const agg = {};
    Object.entries(teamData.breakdown).forEach(([person, projs]) => {
      if (estHideUnassigned && person === 'Unassigned') return;
      (projs || []).forEach(p => (p.tasks || []).forEach(t => {
        if (!statusOn(t.status || NO_STATUS)) return;
        const a = agg[p.project] || (agg[p.project] = { hours: 0, ntasks: 0, persons: {} });
        // Remaining work on this task: what was estimated, less the time already tracked
        // against it. An over-run task contributes a negative amount, as on Team Capacity.
        const rem = (t.estimated || 0) - (t.actual || 0);
        a.hours += rem;
        a.ntasks += 1;
        a.persons[person] = (a.persons[person] || 0) + rem;
      }));
    });
    const rows = Object.entries(agg)
      .map(([name, a]) => ({ name, gid: gidOf[name], cap: name in capOf ? capOf[name] : null,
        hours: Math.round(a.hours * 100) / 100, ntasks: a.ntasks, persons: a.persons }))
      .filter(w => w.hours > 0)
      .sort((a, b) => b.hours - a.hours);

    // Roll grouped projects (e.g. CMD) into one combined bucket; clicking that bucket drills in
    // and splits it back out into its member projects (each as its own bar). Non-grouped
    // projects always stay standalone.
    const groups = groupsConfig || [];
    const memberGids = new Set(groups.flatMap(g => g.gids));
    const drill = estDrillGroup ? groups.find(g => g.name === estDrillGroup) : null;
    let displayRows, backBar = '';
    if (drill) {
      const gset = new Set(drill.gids);
      displayRows = rows.filter(w => gset.has(w.gid)).sort((a, b) => b.hours - a.hours);
      if (!displayRows.length) { estDrillGroup = null; return renderEstProj(); }
      backBar = `<div class="drill-head"><button class="btn back" id="est-drill-back">← Back to all projects</button>` +
                `<h2>${esc(drill.name)} · split by project</h2></div>`;
    } else {
      const groupRows = groups.map(g => {
        const gset = new Set(g.gids);
        const members = rows.filter(w => gset.has(w.gid));
        const persons = {};
        members.forEach(m => Object.entries(m.persons).forEach(([p, v]) => { persons[p] = (persons[p] || 0) + v; }));
        return { name: g.name, gid: null, isGroup: true, group: g.name, cap: g.cap,
          hours: r2(members.reduce((a, m) => a + m.hours, 0)),
          ntasks: members.reduce((a, m) => a + m.ntasks, 0), persons, members };
      }).filter(g => g.hours > 0);
      const others = rows.filter(w => !memberGids.has(w.gid));
      displayRows = [...groupRows, ...others].sort((a, b) => b.hours - a.hours);
    }
    if (!displayRows.length) {
      view.innerHTML = toolbar + note('No remaining hours match the current filters.');
      wireFilters(); return;
    }
    // Headline stats for the rows currently in view, before the By-project checkboxes
    // narrow the chart down — so the banner always reads as "everything under the filters".
    const people = new Set();
    displayRows.forEach(w => Object.entries(w.persons).forEach(([p, v]) => { if (v > 0) people.add(p); }));
    const summary = summaryBar([
      { n: h2(displayRows.reduce((a, w) => a + w.hours, 0)) + ' h', l: 'Remaining estimated' },
      { n: displayRows.length, l: 'Projects' },
      { n: displayRows.reduce((a, w) => a + w.ntasks, 0), l: 'Tasks' },
      { n: people.size, l: 'People assigned' },
    ], 'est');
    view.innerHTML = summary + toolbar + backBar + '<div class="chart-box"><canvas id="chart"></canvas></div>' +
      '<div id="est-summary"></div>';
    wireFilters();
    const backBtn = document.getElementById('est-drill-back');
    if (backBtn) backBtn.onclick = () => { estDrillGroup = null; renderEstProj(); };
    destroyCharts();
    renderEstSummary(displayRows);
    // Projects unchecked in the By-project table drop out of the chart only (same behaviour
    // as the Actual Hours bar chart); the table below still lists them so they can come back.
    const chartRows = displayRows.filter(w => !estHiddenProjects.has(w.name));
    if (!chartRows.length) {
      const cb = view.querySelector('.chart-box');
      if (cb) cb.innerHTML = noteBox('All projects hidden — re-check a project below to show it in the chart.');
      return;
    }
    const labels = chartRows.map(w => w.name);
    const caps = chartRows.map(w => (w.cap == null ? null : w.cap));
    const datasets = personStacks(chartRows, w => w.persons);
    const top = Math.max(...chartRows.map(w => w.hours), ...caps.filter(c => c != null), 1);
    chart = new Chart(document.getElementById('chart'), {
      type: 'bar',
      data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        // Combined buckets split into their projects; single projects open their detail.
        onClick: (evt, els) => { if (!els.length) return; const r = chartRows[els[0].index];
          if (r.isGroup) { estDrillGroup = r.group; renderEstProj(); }
          else if (r.gid) location.hash = '#/p/' + r.gid; },
        onHover: (evt, els) => { const r = els.length ? chartRows[els[0].index] : null;
          evt.native.target.style.cursor = (r && (r.isGroup || r.gid)) ? 'pointer' : 'default'; },
        scales: { x: { stacked: true, title: { display: true, text: 'Project' } },
                  y: { stacked: true, beginAtZero: true, suggestedMax: top * 1.05,
                       title: { display: true, text: 'Remaining hours' }, ticks: { callback: v => h2(v) } } },
        plugins: { legend: { display: true, position: 'bottom' }, capMarks: { caps },
          tooltip: { itemSort: (a, b) => b.parsed.y - a.parsed.y, filter: item => item.parsed.y > 0,
            callbacks: {
              label: ctx => `${ctx.dataset.label}: ${h2(ctx.parsed.y)} h`,
              afterBody: items => {
                const r = chartRows[items[0].dataIndex], lines = [`Remaining ${h2(r.hours)} h · ${plural(r.ntasks, 'task')}`];
                if (r.cap != null) lines.push(`Budget ${h2(r.cap)} h/mo (${(r.hours / r.cap * 100).toFixed(0)}% remaining)`);
                if (r.isGroup) {
                  lines.push('', 'By project:');
                  r.members.slice().sort((a, b) => b.hours - a.hours).forEach(m => {
                    lines.push(`  ${m.name} — ${h2(m.hours)} h`);
                    Object.entries(m.persons).filter(x => x[1] > 0).sort((a, b) => b[1] - a[1])
                      .forEach(([p, v]) => lines.push(`     ${p}: ${h2(v)} h`));
                  });
                  lines.push('', 'Click to split into projects');
                }
                return lines;
              } } } } }
    });
  }

  // Bottom-of-page summary table for the Estimated Hours bar chart: remaining hours per project.
  function renderEstSummary(rows) {
    const box = document.getElementById('est-summary');
    if (!box) return;
    const statRow = (name, hours, ntasks, cls) =>
      `<tr${cls ? ' class="' + cls + '"' : ''}><td>${esc(name)}</td>` +
      `<td class="hours">${h2(hours)} h</td><td class="hours">${ntasks}</td></tr>`;
    // Same as statRow but the Project cell leads with a checkbox that toggles the project's
    // visibility in the bar chart above (unchecking adds it to estHiddenProjects).
    const projStatRow = (name, hours, ntasks) => {
      const checked = estHiddenProjects.has(name) ? '' : ' checked';
      const attr = esc(name).replace(/"/g, '&quot;');
      return `<tr><td><label class="proj-toggle"><input type="checkbox" class="proj-check" data-proj="${attr}"${checked}> ${esc(name)}</label></td>` +
        `<td class="hours">${h2(hours)} h</td><td class="hours">${ntasks}</td></tr>`;
    };
    const totH = rows.reduce((a, w) => a + w.hours, 0);
    const totT = rows.reduce((a, w) => a + w.ntasks, 0);
    box.innerHTML =
      `<h2 class="section-h">By project</h2>
       <table class="tasks">
         <thead><tr><th>Project</th><th class="hours">Remaining</th><th class="hours">Tasks</th></tr></thead>
         <tbody>${rows.map(w => projStatRow(w.name, w.hours, w.ntasks)).join('')}
           ${statRow('All projects', totH, totT, 'parent')}</tbody>
       </table>`;
    // Wire the By-project checkboxes: toggling one hides/shows that project in the chart
    // (kept in estHiddenProjects) and re-renders the tab.
    box.querySelectorAll('.proj-check').forEach(cb => {
      cb.onchange = () => {
        const p = cb.getAttribute('data-proj');
        if (cb.checked) estHiddenProjects.delete(p); else estHiddenProjects.add(p);
        renderEstProj();
      };
    });
  }

  function renderActualProj() {
    // Stacked bar chart of logged hours per project for the selected date range, split by person.
    const picker = rangePicker();
    if (!loggedData) { view.innerHTML = picker + note(LOADING); wireRangeSel(); return; }
    // Roll grouped projects (e.g. CMD) into one combined bucket with the group's cap; clicking
    // that bucket drills in and splits it into its member projects. Every other project stays
    // on its own bar. Then drop anything with no hours.
    const memberGids = new Set((groupsConfig || []).flatMap(g => g.gids));
    const drill = actualDrillGroup ? (groupsConfig || []).find(g => g.name === actualDrillGroup) : null;
    let rows, backBar = '';
    if (drill) {
      const gset = new Set(drill.gids);
      rows = loggedData.filter(w => gset.has(w.gid) && w.hours > 0)
        .map(w => Object.assign({ isGroup: false }, w))
        .sort((a, b) => b.hours - a.hours);
      if (!rows.length) { actualDrillGroup = null; return renderActualProj(); }
      backBar = `<div class="drill-head"><button class="btn back" id="actual-drill-back">← Back to all projects</button>` +
                `<h2>${esc(drill.name)} · split by project</h2></div>`;
    } else {
      const groupItems = (groupsConfig || []).map(g => {
        const s = buildGroupSummary(g, loggedData);
        return { name: g.name, gid: null, isGroup: true, gids: g.gids, cap: g.cap, members: s.members,
          hours: s.hours, nentries: s.nentries };
      });
      const projItems = loggedData.filter(w => !memberGids.has(w.gid)).map(w => Object.assign({ isGroup: false }, w));
      rows = [...groupItems, ...projItems].filter(w => w.hours > 0).sort((a, b) => b.hours - a.hours);
    }
    if (!rows.length) {
      view.innerHTML = picker + note(`No hours logged in ${rangeLabel(dateStart, dateEnd)}.`);
      wireRangeSel(); return;
    }
    // Headline stats for everything in range, before the By-project checkboxes narrow the chart.
    const summary = summaryBar([
      { n: h2(rows.reduce((a, w) => a + w.hours, 0)) + ' h', l: 'Hours logged' },
      { n: rows.length, l: 'Projects' },
      { n: rows.reduce((a, w) => a + (w.nentries || 0), 0), l: 'Time entries' },
    ]);
    view.innerHTML = summary + picker + backBar + '<div class="chart-box"><canvas id="chart"></canvas></div>' +
      '<div id="actual-summary"></div>';
    wireRangeSel();
    const backBtn = document.getElementById('actual-drill-back');
    if (backBtn) backBtn.onclick = () => { actualDrillGroup = null; renderActualProj(); };
    destroyCharts();
    renderActualSummary(rows);
    // The per-person split needs each project's detail (loaded on demand). Show a placeholder
    // in the chart box until it lands, then re-render this whole tab.
    const key = dateStart + ':' + dateEnd, pcache = projPersonCache[key];
    if (!pcache) {
      const cb = view.querySelector('.chart-box');
      if (cb) cb.innerHTML = noteBox('Loading per-person breakdown…');
      loadPersonStats(key);
      return;
    }
    // Checkboxes in the By-project table hide projects from the chart only.
    const chartRows = rows.filter(w => !actualHiddenProjects.has(w.name));
    if (!chartRows.length) {
      const cbox = view.querySelector('.chart-box');
      if (cbox) cbox.innerHTML = noteBox('All projects hidden — re-check a project below to show it in the chart.');
      return;
    }
    const labels = chartRows.map(w => w.name);
    const caps = chartRows.map(w => (w.cap == null ? null : w.cap));
    // Total logged hours per person for a row; groups sum their members.
    const rowPersons = (w) => {
      const src = w.isGroup ? (w.gids || []) : [w.gid], out = {};
      src.forEach(g => { const m = pcache[g]; if (m) Object.entries(m).forEach(([p, v]) => { out[p] = (out[p] || 0) + v; }); });
      return out;
    };
    const datasets = personStacks(chartRows, rowPersons);
    // Keep every budget marker visible even if it sits above the tallest bar.
    const top = Math.max(...chartRows.map(w => w.hours), ...caps.filter(c => c != null), 1);
    chart = new Chart(document.getElementById('chart'), {
      type: 'bar',
      data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        // Combined buckets split into their projects; single projects open their detail.
        onClick: (evt, els) => { if (!els.length) return; const r = chartRows[els[0].index];
          if (r.isGroup) { actualDrillGroup = r.name; renderActualProj(); }
          else if (r.gid) location.hash = '#/logged/' + r.gid; },
        onHover: (evt, els) => { const r = els.length ? chartRows[els[0].index] : null;
          evt.native.target.style.cursor = (r && (r.isGroup || r.gid)) ? 'pointer' : 'default'; },
        scales: { x: { stacked: true, title: { display: true, text: 'Project' } },
                  y: { stacked: true, beginAtZero: true, suggestedMax: top * 1.05,
                       title: { display: true, text: 'Hours logged' }, ticks: { callback: v => h2(v) } } },
        plugins: { legend: { display: true, position: 'bottom' }, capMarks: { caps },
          tooltip: { itemSort: (a, b) => b.parsed.y - a.parsed.y, filter: item => item.parsed.y > 0,
            callbacks: {
              label: ctx => `${ctx.dataset.label}: ${h2(ctx.parsed.y)} h`,
              afterBody: items => {
                const r = chartRows[items[0].dataIndex], lines = [`Total ${h2(r.hours)} h · ${plural(r.nentries, 'entry', 'entries')}`];
                if (r.cap != null) lines.push(`Budget ${h2(r.cap)} h`);
                if (r.isGroup) {
                  lines.push('', 'By project:');
                  (r.members || []).slice().sort((a, b) => b.hours - a.hours).forEach(m => {
                    lines.push(`  ${m.name} — ${h2(m.hours)} h`);
                    Object.entries(pcache[m.gid] || {})
                      .filter(x => x[1] > 0).sort((a, b) => b[1] - a[1])
                      .forEach(([p, v]) => lines.push(`     ${p}: ${h2(v)} h`));
                  });
                  lines.push('', 'Click to split into projects');
                }
                return lines;
              } } } } }
    });
  }

  // Bottom-of-page summary tables for Actual Hours: totals per project and per person.
  // rows_ mirrors the bar chart's rows (combined buckets when not drilled in, member projects
  // when drilled in) so the "By project" table matches whatever the chart is showing.
  function renderActualSummary(rows_) {
    const box = document.getElementById('actual-summary');
    if (!box) return;
    const statRow = (name, h, cls) =>
      `<tr${cls ? ' class="' + cls + '"' : ''}><td>${esc(name)}</td>` +
      `<td class="hours">${h2(h)} h</td></tr>`;
    // Same as statRow but the Project cell leads with a checkbox that toggles the project's
    // visibility in the bar chart above (unchecking adds it to actualHiddenProjects).
    const projStatRow = (name, h) => {
      const checked = actualHiddenProjects.has(name) ? '' : ' checked';
      const attr = esc(name).replace(/"/g, '&quot;');
      return `<tr><td><label class="proj-toggle"><input type="checkbox" class="proj-check" data-proj="${attr}"${checked}> ${esc(name)}</label></td>` +
        `<td class="hours">${h2(h)} h</td></tr>`;
    };

    // By project — mirrors the chart's rows (combined buckets / drilled-in members).
    const projRows = (rows_ || loggedData.filter(w => w.hours > 0)).slice().sort((a, b) => b.hours - a.hours);
    const pjTot = projRows.reduce((a, w) => a + w.hours, 0);
    const projTable =
      `<h2 class="section-h">By project</h2>
       <table class="tasks">
         <thead><tr><th>Project</th><th class="hours">Hours</th></tr></thead>
         <tbody>${projRows.map(w => projStatRow(w.name, w.hours)).join('')}
           ${statRow('All projects', pjTot, 'parent')}</tbody>
       </table>`;

    // By person — aggregated across every project's detail for this range (loaded on demand).
    // When drilled into a group, scope the per-person totals to that group's member projects.
    const key = dateStart + ':' + dateEnd;
    let people = personStatsCache[key];
    if (actualDrillGroup && projPersonCache[key]) {
      const pc = projPersonCache[key], g = (groupsConfig || []).find(x => x.name === actualDrillGroup), pa = {};
      (g ? g.gids : []).forEach(gid => Object.entries(pc[gid] || {}).forEach(([p, v]) => {
        pa[p] = (pa[p] || 0) + v; }));
      people = Object.keys(pa).map(name => ({ name, hours: pa[name] }))
        .sort((a, b) => b.hours - a.hours);
    }
    let personTable;
    if (!people) {
      personTable = '<h2 class="section-h">By person</h2><p class="note" id="person-loading">Loading per-person totals…</p>';
    } else {
      const ppTot = people.reduce((a, p) => a + p.hours, 0);
      personTable =
        `<h2 class="section-h">By person</h2>
         <table class="tasks">
           <thead><tr><th>Person</th><th class="hours">Hours</th></tr></thead>
           <tbody>${people.map(p => statRow(p.name, p.hours)).join('')}
             ${statRow('Everyone', ppTot, 'parent')}</tbody>
         </table>`;
    }
    box.innerHTML = projTable + personTable;
    // Wire the By-project checkboxes: toggling one hides/shows that project in the chart
    // (kept in actualHiddenProjects) and re-renders the tab.
    box.querySelectorAll('.proj-check').forEach(cb => {
      cb.onchange = () => {
        const p = cb.getAttribute('data-proj');
        if (cb.checked) actualHiddenProjects.delete(p); else actualHiddenProjects.add(p);
        renderActualProj();
      };
    });
    if (!people) loadPersonStats(key);
  }

  // Pull each logged project's detail and total the hours per person — both globally
  // (summary table) and per project (the stacked chart) — then cache + re-render.
  async function loadPersonStats(key) {
    if (personLoading[key] || personStatsCache[key]) return;   // already loading / loaded
    personLoading[key] = true;
    const gids = loggedData.filter(w => w.hours > 0).map(w => w.gid);
    let details;
    try {
      details = await Promise.all(gids.map(g =>
        fetch(`/api/logged/${g}?start=${dateStart}&end=${dateEnd}`).then(r => r.json())));
    } catch (e) {
      const el = document.getElementById('person-loading');
      if (el) el.innerHTML = fmtErr(e);
      delete personLoading[key];
      return;
    }
    delete personLoading[key];
    if (key !== dateStart + ':' + dateEnd) return;   // range changed while loading
    const agg = {}, byGid = {}, items = {}, daily = {};
    gids.forEach((g, idx) => {
      const d = details[idx], pm = byGid[g] = {};
      (d.labels || []).forEach((name, i) => {
        agg[name] = (agg[name] || 0) + d.hours[i];
        pm[name] = (pm[name] || 0) + d.hours[i];
      });
      // Roll each project's time entries up by task AND the person who logged them, so the
      // Items tab can list every worked-on item with who logged the time and how much. Key by
      // project + task + person so identically named tasks / multiple loggers stay separate.
      (d.entries || []).forEach(e => {
        const person = e.by || 'Unknown';
        const ikey = (d.name || '') + ' | ' + e.task + ' | ' + person;
        const it = items[ikey] || (items[ikey] = { task: e.task, project: d.name || '', person: person, hours: 0, entries: 0 });
        it.hours += e.hours;
        it.entries += 1;
        // Same rollup again, but keyed by person + day + task, so the Daily Log can show
        // one row per task worked on that day and total each day's logged hours.
        const dkey = person + ' | ' + (e.date || '') + ' | ' + (d.name || '') + ' | ' + e.task;
        const dr = daily[dkey] || (daily[dkey] =
          { person: person, date: e.date || '', task: e.task, project: d.name || '', hours: 0, entries: 0 });
        dr.hours += e.hours;
        dr.entries += 1;
      });
    });
    projPersonCache[key] = byGid;
    personStatsCache[key] = Object.keys(agg)
      .map(name => ({ name, hours: agg[name] }))
      .sort((a, b) => b.hours - a.hours);
    itemStatsCache[key] = Object.values(items)
      // Group by project (A→Z), then heaviest items first within each project.
      .sort((a, b) => a.project.localeCompare(b.project)
        || b.hours - a.hours
        || a.task.localeCompare(b.task));
    dailyStatsCache[key] = Object.values(daily)
      // Person (A→Z), then day (oldest first — the tab flips this when newest-first is on),
      // then heaviest task first within the day.
      .sort((a, b) => a.person.localeCompare(b.person)
        || (a.date < b.date ? -1 : a.date > b.date ? 1 : 0)
        || b.hours - a.hours
        || a.task.localeCompare(b.task));
    // Re-render the whole tab so the chart (which needs the per-project split) draws too.
    if (dashTab === 'actualproj') renderActualProj();
    else if (dashTab === 'actualitems') renderActualItems();
    else if (dashTab === 'actualdaily') renderActualDaily();
    else if (dashTab === 'teamactual') renderTeamActual();
  }

  // Actual Hours › Items: every task worked on in the selected range, with its logged
  // hours and a grand total. Reuses the per-item rollup
  // that loadPersonStats builds from each project's time entries.
  function renderActualItems() {
    if (!loggedData) { view.innerHTML = rangePicker() + note(LOADING); wireRangeSel(); return; }
    if (!loggedData.some(w => w.hours > 0)) {
      view.innerHTML = rangePicker() + note(`No hours logged in ${rangeLabel(dateStart, dateEnd)}.`);
      wireRangeSel(); return;
    }
    const key = dateStart + ':' + dateEnd, allItems = itemStatsCache[key];
    if (!allItems) {
      view.innerHTML = rangePicker() + note(LOADING);
      wireRangeSel();
      loadPersonStats(key);
      return;
    }
    // Assignee filter: dropdown of everyone who logged time in this range. If the remembered
    // selection isn't present in this range, fall back to showing everyone.
    const people = [...new Set(allItems.map(it => it.person))].sort((a, b) => a.localeCompare(b));
    if (itemFilterPerson && !people.includes(itemFilterPerson)) itemFilterPerson = null;
    const items = itemFilterPerson ? allItems.filter(it => it.person === itemFilterPerson) : allItems;
    // Range + assignee live in the one toolbar this tab shows.
    const picker = rangePicker(`<span class="tb-group"><span class="tb-label">Assignee</span>
      <select id="item-assignee">
        <option value="">All assignees</option>
        ${people.map(p => `<option value="${esc(p)}"${p === itemFilterPerson ? ' selected' : ''}>${esc(p)}</option>`).join('')}
      </select></span>`);
    const tot = items.reduce((a, it) => a + it.hours, 0);
    const totEntries = items.reduce((a, it) => a + it.entries, 0);
    const nproj = new Set(items.map(it => it.project)).size;
    const hoursCells = (h) => `<td class="hours">${h2(h)} h</td>`;
    // Headline summary: total hours logged across every project for the selected range.
    const summary = summaryBar([
      { n: h2(tot) + ' h', l: 'Hours logged' },
      { n: nproj, l: 'Projects' },
      { n: items.length, l: 'Tasks' },
      { n: totEntries, l: 'Time entries' },
    ]);
    // One titled section per project: a heading, a table of that project's logged tasks
    // (with who logged them), and a project total row. `items` is already sorted by project,
    // so the keys keep project order.
    const byProject = {};
    items.forEach(it => (byProject[it.project] = byProject[it.project] || []).push(it));
    const sections = Object.keys(byProject).map((proj, i) => {
      const list = byProject[proj];
      const pTot = list.reduce((a, it) => a + it.hours, 0);
      const body = list.map(it =>
        `<tr><td>${esc(it.task)}</td><td>${esc(it.person)}</td>${hoursCells(it.hours)}</tr>`).join('');
      // Heading carries the project total, matching how the Daily Log heads its sections.
      // The heading carries the project total, so the table needs no total row.
      return `<h2 class="section-h${i ? '' : ' flush'}">${esc(proj)} · ${h2(pTot)} h</h2>
        <table class="tasks">
          <thead><tr><th>Task</th><th>Logged by</th><th class="hours">Hours</th></tr></thead>
          <tbody>${body}</tbody>
        </table>`;
    }).join('');
    const noneMsg = items.length ? '' : note(`No hours logged by ${itemFilterPerson} in ${rangeLabel(dateStart, dateEnd)}.`);
    view.innerHTML = summary + picker + (noneMsg || sections);
    wireRangeSel();
    const sel = document.getElementById('item-assignee');
    if (sel) sel.onchange = () => { itemFilterPerson = sel.value || null; renderActualItems(); };
  }

  // Actual Hours › Daily Log: for each person, a day-by-day list of everything they logged
  // time to in the selected range — one table per day (task, project, hours) with that day's
  // total. Reuses the per-person/per-day rollup loadPersonStats builds from the time entries.
  function renderActualDaily() {
    if (!loggedData) { view.innerHTML = rangePicker() + note(LOADING); wireRangeSel(); return; }
    if (!loggedData.some(w => w.hours > 0)) {
      view.innerHTML = rangePicker() + note(`No hours logged in ${rangeLabel(dateStart, dateEnd)}.`);
      wireRangeSel(); return;
    }
    const key = dateStart + ':' + dateEnd, allRows = dailyStatsCache[key];
    if (!allRows) {
      view.innerHTML = rangePicker() + note(LOADING);
      wireRangeSel();
      loadPersonStats(key);
      return;
    }
    // Assignee filter: same behaviour as the Task List tab — a remembered person who logged
    // nothing in this range falls back to showing everyone.
    const people = [...new Set(allRows.map(r => r.person))].sort((a, b) => a.localeCompare(b));
    // First time this tab has data: open on whoever is signed in, so you land on your own log.
    // If the PAT owner logged nothing in this range (or /api/me failed), stay on all assignees.
    if (!dailyFilterInit) {
      if (currentUser && people.includes(currentUser)) dailyFilterPerson = currentUser;
      dailyFilterInit = true;
    }
    if (dailyFilterPerson && !people.includes(dailyFilterPerson)) dailyFilterPerson = null;
    const rows = dailyFilterPerson ? allRows.filter(r => r.person === dailyFilterPerson) : allRows;
    // Range, assignee and day order all live in the one toolbar this tab shows.
    const picker = rangePicker(`<span class="tb-group"><span class="tb-label">Assignee</span>
      <select id="daily-assignee">
        <option value="">All assignees</option>
        ${people.map(p => `<option value="${esc(p)}"${p === dailyFilterPerson ? ' selected' : ''}>${esc(p)}</option>`).join('')}
      </select></span>
      <span class="tb-group"><span class="tb-label">Order</span>
      <select id="daily-order">
        <option value="old"${dailyNewestFirst ? '' : ' selected'}>Oldest day first</option>
        <option value="new"${dailyNewestFirst ? ' selected' : ''}>Newest day first</option>
      </select></span>`);
    const tot = rows.reduce((a, r) => a + r.hours, 0);
    const totEntries = rows.reduce((a, r) => a + r.entries, 0);
    const days = new Set(rows.map(r => r.person + '|' + r.date));
    const hoursCells = (h) => `<td class="hours">${h2(h)} h</td>`;
    const summary = summaryBar([
      { n: h2(tot) + ' h', l: 'Hours logged' },
      { n: days.size, l: 'Person-days' },
      { n: totEntries, l: 'Time entries' },
    ]);
    // Group person → day. `rows` is already sorted person A→Z then date ascending, so the
    // key order holds; only the day order flips when "newest first" is selected.
    const byPerson = {};
    rows.forEach(r => {
      const p = byPerson[r.person] = byPerson[r.person] || {};
      (p[r.date] = p[r.date] || []).push(r);
    });
    // One table per person — not one per day. The date cell spans that day's rows and
    // carries the day's total underneath it, so nothing is stated twice and the column
    // headers appear once instead of once per day.
    const sections = Object.keys(byPerson).map((person, pi) => {
      const dayMap = byPerson[person];
      const dayKeys = Object.keys(dayMap);
      if (dailyNewestFirst) dayKeys.reverse();
      const pTot = dayKeys.reduce((a, d) => a + dayMap[d].reduce((x, r) => x + r.hours, 0), 0);
      const body = dayKeys.map(d => {
        const list = dayMap[d];
        const dTot = list.reduce((a, r) => a + r.hours, 0);
        const dayCell = `<td class="day" rowspan="${list.length}">${d ? esc(fmtDate(d)) : 'No date'}` +
          `<span class="day-tot">${h2(dTot)} h</span></td>`;
        return list.map((r, i) =>
          `<tr class="${i ? '' : 'day-start'}">${i ? '' : dayCell}` +
          `<td>${esc(r.task)}</td><td>${esc(r.project)}</td>${hoursCells(r.hours)}</tr>`).join('');
      }).join('');
      return `<h2 class="section-h${pi ? '' : ' flush'}">${esc(person)} · ${h2(pTot)} h</h2>
        <table class="tasks daylog">
          <thead><tr><th>Day</th><th>Task</th><th>Project</th><th class="hours">Hours</th></tr></thead>
          <tbody>${body}</tbody>
        </table>`;
    }).join('');
    const noneMsg = rows.length ? '' : note(`No hours logged by ${dailyFilterPerson} in ${rangeLabel(dateStart, dateEnd)}.`);
    view.innerHTML = summary + picker + (noneMsg || sections);
    wireRangeSel();
    const sel = document.getElementById('daily-assignee');
    if (sel) sel.onchange = () => { dailyFilterPerson = sel.value || null; dailyFilterInit = true; renderActualDaily(); };
    const ord = document.getElementById('daily-order');
    if (ord) ord.onchange = () => { dailyNewestFirst = ord.value === 'new'; renderActualDaily(); };
  }

  // Every status column present in the Team Capacity task rows, in filter order
  // (alphabetical, with "no status" last).
  function statusColumns() {
    const seen = new Set();
    Object.values((teamData && teamData.breakdown) || {}).forEach(projs =>
      (projs || []).forEach(p => (p.tasks || []).forEach(t => seen.add(t.status || NO_STATUS))));
    return [...seen].sort((a, b) => a === NO_STATUS ? 1 : b === NO_STATUS ? -1 : a.localeCompare(b));
  }
  const teamStatusOn = s => !teamStatusFilter || teamStatusFilter.has(s || NO_STATUS);

  // One person's Team Capacity rollup under the current status filter: their projects with
  // only the matching task rows, and the totals those rows add up to. Recomputing from the
  // task rows (rather than teamData's per-person totals) is what makes the filter apply to
  // the bars, the tooltips and the drill-in table alike.
  function teamRollup(name) {
    const projects = [];
    let estimated = 0, actual = 0, count = 0;
    ((teamData.breakdown || {})[name] || []).forEach(p => {
      const tasks = (p.tasks || []).filter(t => teamStatusOn(t.status));
      if (teamStatusFilter && !tasks.length) return;
      const e = tasks.reduce((a, t) => a + (t.estimated || 0), 0);
      const a_ = tasks.reduce((a, t) => a + (t.actual || 0), 0);
      projects.push({ project: p.project, estimated: r2(e), actual: r2(a_), remaining: r2(e - a_), tasks });
      estimated += e; actual += a_; count += tasks.length;
    });
    return { projects, estimated: r2(estimated), actual: r2(actual),
             remaining: r2(estimated - actual), count };
  }

  function renderTeam() {
    // Bar chart of estimated hours per assignee, with a dashed line at the 128 h target.
    // Bars can be filtered to a subset of status columns; Unassigned is hidden by default.
    const src = teamData, hasU = src.labels.includes('Unassigned');
    const statusList = statusColumns();
    // Forget any remembered status that no longer exists; an empty set collapses back to "all".
    if (teamStatusFilter) {
      teamStatusFilter = new Set([...teamStatusFilter].filter(s => statusList.includes(s)));
      if (!teamStatusFilter.size) teamStatusFilter = null;
    }
    // Re-sort by the filtered remaining hours (Unassigned stays pinned far right), so the
    // bars keep the same most-loaded-first reading under any status selection.
    const rolled = src.labels
      .filter(n => teamShowUnassigned || n !== 'Unassigned')
      .map(n => ({ name: n, roll: teamRollup(n) }))
      .sort((a, b) => a.name === 'Unassigned' ? 1 : b.name === 'Unassigned' ? -1
                      : b.roll.remaining - a.roll.remaining);
    const labels = rolled.map(x => x.name), stats = rolled.map(x => x.roll);
    const d = { cap: src.cap, labels,
      hours: stats.map(s => s.remaining), estimated: stats.map(s => s.estimated),
      actual: stats.map(s => s.actual), counts: stats.map(s => s.count) };
    const toolbar =
      '<div class="toolbar team-toolbar">' +
        '<span class="tb-label">Status</span>' +
        statusList.map(s =>
          `<label class="chk"><input type="checkbox" class="team-status" value="${esc(s)}" ${teamStatusOn(s) ? 'checked' : ''}>${esc(s)}</label>`
        ).join('') +
        (hasU
          ? '<span class="tb-sep"></span>' +
            `<label class="chk"><input type="checkbox" id="team-show-unassigned" ${teamShowUnassigned ? 'checked' : ''}>Include Unassigned</label>`
          : '') +
      '</div>';
    // Headline stats: what the team still has on its plate versus what it can absorb.
    const totRem = d.hours.reduce((a, h) => a + h, 0);
    const headroom = r2(d.cap * d.labels.length - totRem);
    const summary = summaryBar([
      { n: h2(totRem) + ' h', l: 'Remaining estimated' },
      { n: d.labels.length, l: 'People' },
      { n: h2(d.labels.length ? totRem / d.labels.length : 0) + ' h', l: 'Avg per person' },
      { n: h2(headroom) + ' h', l: headroom < 0 ? 'Over team capacity' : 'Team headroom', neg: headroom < 0 },
    ], 'est');
    view.innerHTML = summary + toolbar + '<div class="chart-box"><canvas id="chart"></canvas></div>';
    view.querySelectorAll('.team-status').forEach(box => box.onchange = () => {
      const on = [...view.querySelectorAll('.team-status')].filter(c => c.checked).map(c => c.value);
      teamStatusFilter = (on.length === statusList.length) ? null : new Set(on);
      renderTeam();
    });
    const cb = document.getElementById('team-show-unassigned');
    if (cb) cb.onchange = () => { teamShowUnassigned = cb.checked; renderTeam(); };
    destroyCharts();
    const colors = d.hours.map((h, i) => d.labels[i] === 'Unassigned' ? personColor('Unassigned') : capColorEst(h));
    // Someone who has tracked more time than was estimated has negative remaining hours. That
    // is real, but drawing it pulls the axis below zero and squashes everyone else, so the bars
    // bottom out at 0 — the true est/actual split stays in the tooltip and the drill-in.
    const barHours = d.hours.map(h => Math.max(0, h));
    chart = new Chart(document.getElementById('chart'), {
      type: 'bar',
      data: { labels: d.labels, datasets: [{ label: 'Remaining hours', data: barHours,
        backgroundColor: colors, borderColor: colors, borderWidth: 1,
        _counts: d.counts, _est: d.estimated, _act: d.actual }] },
      options: { responsive: true, maintainAspectRatio: false,
        onClick: (evt, els) => { if (els.length) showBreakdown(d.labels[els[0].index]); },
        onHover: (evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        scales: { x: { title: { display: true, text: 'Assignee' },
                       ticks: { callback: (v, i) => [d.labels[i], h2(d.hours[i]) + ' h'] } },
                  y: { min: 0, suggestedMax: Math.max(d.cap * 1.1, ...barHours, 1),
                       title: { display: true, text: 'Remaining hours' }, ticks: { callback: v => h2(v) } } },
        plugins: { legend: { display: false }, capLine: { value: d.cap },
          tooltip: { callbacks: {
            label: ctx => `Remaining: ${h2(d.hours[ctx.dataIndex])} h of ${h2(d.cap)} (${(d.hours[ctx.dataIndex] / d.cap * 100).toFixed(0)}%)`,
            afterLabel: ctx => `Est ${h2(ctx.dataset._est[ctx.dataIndex])} − actual ${h2(ctx.dataset._act[ctx.dataIndex])} · ${plural(ctx.dataset._counts[ctx.dataIndex], 'item')}` } } } }
    });
    // One donut per assignee under the bars: which projects their remaining hours sit in.
    // Projects already fully burned down (remaining ≤ 0) drop out, so the rings only show
    // work still ahead — the same measure the bars use. Each ring is padded out to the
    // capacity target with an "unallocated" slice, so a light week reads as a mostly empty
    // ring rather than a full one; anyone already past the target gets no filler.
    donutGrid(view, rolled.map(x => {
      const m = {};
      x.roll.projects.forEach(p => { if (p.remaining > 0) m[p.project] = (m[p.project] || 0) + p.remaining; });
      const slices = donutSlices(m);
      const used = slices.reduce((a, s) => a + s.hours, 0);
      const nproj = slices.length;
      const free = r2(d.cap - used);
      if (free > 0) slices.push({ label: FREE_SLICE, hours: free });
      return { name: x.name, slices, total: Math.max(used, d.cap),
        caption: `${h2(used)} of ${h2(d.cap)} h · ${plural(nproj, 'project')}` +
                 (free > 0 ? '' : ' · at capacity') };
    }), { title: 'Remaining hours by project, per person',
          sub: `Each ring is one assignee, filled out to the ${h2(d.cap)} h monthly target. Click a card for their full project/task breakdown.`,
          onPick: r => showBreakdown(r.name) });
  }

  function showBreakdown(name, back) {
    destroyCharts();
    // Same status filter the chart is showing, so the drill-in adds up to the clicked bar.
    const roll = teamRollup(name);
    const rows_ = roll.projects.slice().sort((a, b) => b.remaining - a.remaining);
    const totEst = roll.estimated, totAct = roll.actual, totRem = roll.remaining;
    const pct = (totRem / teamData.cap * 100).toFixed(0);
    const filterNote = teamStatusFilter
      ? ` · status: ${[...teamStatusFilter].join(', ')}` : '';
    // Each project is a bold header row, with that person's tasks/subtasks listed beneath it.
    let rows = rows_.map(r => {
      let body = `<tr class="parent"><td>${esc(r.project)}</td><td></td>` +
        `<td class="hours">${h2(r.estimated)} h</td>` +
        `<td class="hours">${h2(r.actual)} h</td>` +
        `<td class="hours">${h2(r.remaining)} h</td></tr>`;
      const tasks = r.tasks || [];
      if (!tasks.length) {
        body += `<tr class="sub"><td class="sub-name" colspan="5">No tasks.</td></tr>`;
      } else {
        body += tasks.map(t => {
          const ctx = t.context ? ` <span class="muted">(${esc(t.context)})</span>` : '';
          const cls = t.type === 'subtask' ? 'sub-name lvl2' : 'lvl1';
          return `<tr class="sub"><td class="${cls}">${esc(t.name)}${ctx}</td>` +
            `<td>${t.status ? `<span class="badge">${esc(t.status)}</span>`
                             : `<span class="badge none">${NO_STATUS}</span>`}</td>` +
            `<td class="hours">${h2(t.estimated)} h</td>` +
            `<td class="hours">${h2(t.actual)} h</td>` +
            `<td class="hours">${h2(t.remaining)} h</td></tr>`;
        }).join('');
      }
      return body;
    }).join('');
    if (!rows_.length) rows = '<tr><td colspan="5" class="muted">No estimated or actual hours.</td></tr>';
    view.innerHTML =
      `<div class="drill-head">
         <button class="btn back" id="tochart">← Back to chart</button>
         <h2>${esc(name)}</h2>
       </div>
       <p class="drill-total">${h2(totRem)} h remaining of ${h2(teamData.cap)} (${pct}%) · ${h2(totEst)} est − ${h2(totAct)} actual · ${plural(rows_.length, 'project')}${esc(filterNote)}</p>
       <table class="tasks">
         <thead><tr><th>Project / Task</th><th>Status</th><th class="hours">Est.</th><th class="hours">Actual</th><th class="hours">Remaining</th></tr></thead>
         <tbody>${rows}</tbody>
       </table>`;
    document.getElementById('tochart').onclick = back || renderTeam;
  }

  // Actual Hours copy of Team Capacity: bars show hours actually LOGGED per person (time
  // entries) in the selected range, vs. the monthly capacity target. Uses the same per-person
  // rollup as the By-person summary (personStatsCache), so it matches the rest of Actual Hours
  // (e.g. Grant's logged time on a project), not Asana's task tracked-time totals.
  function renderTeamActual() {
    if (!loggedData) { view.innerHTML = rangePicker() + note(LOADING); wireRangeSel(); return; }
    const key = dateStart + ':' + dateEnd, people = personStatsCache[key];
    if (!people) {
      view.innerHTML = rangePicker() + note(LOADING);
      wireRangeSel(); loadPersonStats(key); return;
    }
    const cap = (teamData && teamData.cap) || 128;
    const rows = people.map(p => ({ name: p.name, total: p.hours }))
      .filter(p => p.total > 0)
      .sort((a, b) => b.total - a.total);
    // Headline stats: what the team actually booked against what it could have.
    const totLogged = rows.reduce((a, r) => a + r.total, 0);
    const teamCap = cap * rows.length;
    // How far through the work month (Mon–Fri) we are, and therefore how many hours each
    // person should already have logged if they are keeping pace with the monthly target.
    const wp = workdayProgress(dateStart, dateEnd);
    const paceTarget = r2(cap * wp.pct / 100);
    // "Team pace" is what the team has logged against what it should have by today — 100% is
    // on schedule, and anything short of it is flagged.
    const teamPaceTarget = paceTarget * rows.length;
    const teamPace = teamPaceTarget ? totLogged / teamPaceTarget * 100 : 0;
    const summary = summaryBar([
      { n: h2(totLogged) + ' h', l: 'Hours logged' },
      { n: rows.length, l: 'People' },
      { n: h2(rows.length ? totLogged / rows.length : 0) + ' h', l: 'Avg per person' },
      { n: (teamCap ? (totLogged / teamCap * 100).toFixed(0) : '0') + '%', l: 'Of team capacity' },
      { n: wp.pct.toFixed(0) + '%', l: 'Of work month elapsed' },
      { n: `${wp.elapsed} of ${wp.total}`, l: 'Work days used' },
      { n: wp.left, l: 'Work days left' },
      { n: h2(paceTarget) + ' h', l: 'Target by today' },
      { n: teamPaceTarget ? teamPace.toFixed(0) + '%' : '—', l: 'Team pace', neg: teamPaceTarget > 0 && teamPace < 100 },
    ]);
    view.innerHTML = summary + rangePicker() + '<div class="chart-box"><canvas id="chart"></canvas></div>';
    wireRangeSel();
    if (!rows.length) {
      const cb = view.querySelector('.chart-box');
      if (cb) cb.innerHTML = noteBox(`No hours logged in ${rangeLabel(dateStart, dateEnd)}.`);
      return;
    }
    destroyCharts();
    const labels = rows.map(r => r.name), totals = rows.map(r => r.total);
    const colors = totals.map(h => capColorPace(h, paceTarget));
    chart = new Chart(document.getElementById('chart'), {
      type: 'bar',
      data: { labels, datasets: [{ label: 'Hours logged', data: totals,
        backgroundColor: colors, borderColor: colors, borderWidth: 1 }] },
      options: { responsive: true, maintainAspectRatio: false,
        onClick: (evt, els) => { if (els.length) showLoggedBreakdown(rows[els[0].index].name); },
        onHover: (evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        scales: { x: { title: { display: true, text: 'Logged by' },
                       ticks: { callback: (v, i) => [labels[i], h2(totals[i]) + ' h'] } },
                  y: { beginAtZero: true, suggestedMax: Math.max(cap * 1.1, ...totals, 1),
                       title: { display: true, text: 'Hours logged' }, ticks: { callback: v => h2(v) } } },
        // The pace line only means something mid-range: before it opens there is nothing to
        // expect yet, and once it has closed the pace target is just the target.
        plugins: { legend: { display: false },
          capLine: { value: cap,
            pace: (wp.pct > 0 && wp.pct < 100) ? paceTarget : null,
            paceLabel: `${h2(paceTarget)} h by today · ${wp.pct.toFixed(0)}% of work month` },
          tooltip: { callbacks: {
            label: ctx => `Logged: ${h2(ctx.parsed.y)} h of ${h2(cap)} (${(ctx.parsed.y / cap * 100).toFixed(0)}%)`,
            afterLabel: ctx => paceTarget
              ? `Target by today ${h2(paceTarget)} h · ${(ctx.parsed.y / paceTarget * 100).toFixed(0)}% of pace` +
                ` · ${ctx.parsed.y >= paceTarget - PACE_TOLERANCE ? 'on pace'
                       : h2(paceTarget - ctx.parsed.y) + ' h behind'}`
              : '' } } } }
    });
    // One donut per person under the bars: which projects they logged their hours against.
    const pc = projPersonCache[key] || {};
    const nameOf = Object.fromEntries((loggedData || []).map(w => [w.gid, w.name]));
    donutGrid(view, rows.map(r => {
      const m = {};
      Object.entries(pc).forEach(([gid, pm]) => {
        const v = pm[r.name];
        if (v > 0) { const n = nameOf[gid] || gid; m[n] = (m[n] || 0) + v; }
      });
      const slices = donutSlices(m);
      return { name: r.name, slices, total: slices.reduce((a, s) => a + s.hours, 0) };
    }), { title: 'Logged hours by project, per person',
          sub: 'Each ring is one person. Click a card for their per-project totals and the tasks '
             + 'behind them; click a legend swatch to recolor that project.',
          onPick: r => showLoggedBreakdown(r.name) });
  }

  // Per-project logged-hours breakdown for one person, opened from the Actual Hours Team
  // Capacity chart. Built from the per-project/person split (projPersonCache) for the range.
  function showLoggedBreakdown(person) {
    destroyCharts();
    const key = dateStart + ':' + dateEnd, pc = projPersonCache[key] || {};
    const nameOf = Object.fromEntries((loggedData || []).map(w => [w.gid, w.name]));
    // Every task this person logged time to in the range, keyed by project — the same
    // per-item rollup the Items tab uses, so the numbers agree.
    const tasksByProject = {};
    (itemStatsCache[key] || []).forEach(it => {
      if (it.person !== person || !(it.hours > 0)) return;
      (tasksByProject[it.project] || (tasksByProject[it.project] = [])).push(it);
    });
    Object.values(tasksByProject).forEach(list =>
      list.sort((a, b) => b.hours - a.hours || a.task.localeCompare(b.task)));
    const rows_ = [];
    Object.entries(pc).forEach(([gid, pm]) => {
      const v = pm[person];
      if (v > 0) rows_.push({ project: nameOf[gid] || gid, hours: v });
    });
    rows_.sort((a, b) => b.hours - a.hours);
    const tot = rows_.reduce((a, r) => a + r.hours, 0);
    const nEntries = rows_.reduce((a, r) => a + (tasksByProject[r.project] || [])
      .reduce((x, t) => x + (t.entries || 0), 0), 0);
    const nTasks = rows_.reduce((a, r) => a + (tasksByProject[r.project] || []).length, 0);
    const cap = (teamData && teamData.cap) || 128;
    const pct = (tot / cap * 100).toFixed(0);
    const cell = (h) => `<td class="hours">${h2(h)} h</td>`;
    // Each project is a bold header row, with the tasks that person logged to it beneath it.
    let body = rows_.map(r => {
      const tasks = tasksByProject[r.project] || [];
      let out = `<tr class="parent"><td>${esc(r.project)}</td>${cell(r.hours)}` +
        `<td class="hours">${tasks.reduce((a, t) => a + (t.entries || 0), 0)}</td></tr>`;
      out += tasks.length
        ? tasks.map(t => `<tr class="sub"><td class="sub-name lvl2">${esc(t.task)}</td>` +
            `${cell(t.hours)}<td class="hours">${t.entries || 0}</td></tr>`).join('')
        : `<tr class="sub"><td class="sub-name lvl2" colspan="3">No task-level entries.</td></tr>`;
      return out;
    }).join('');
    if (!rows_.length) body = '<tr><td colspan="3" class="muted">No hours logged in this range.</td></tr>';
    view.innerHTML =
      `<div class="drill-head">
         <button class="btn back" id="tochart">← Back to chart</button>
         <h2>${esc(person)}</h2>
       </div>
       <p class="drill-total">${h2(tot)} h logged of ${h2(cap)} (${pct}%) · ${
         plural(rows_.length, 'project')} · ${plural(nTasks, 'task')} · ${
         plural(nEntries, 'time entry', 'time entries')} in ${rangeLabel(dateStart, dateEnd)}</p>
       <table class="tasks">
         <thead><tr><th>Project / Task</th><th class="hours">Hours</th>
           <th class="hours">Entries</th></tr></thead>
         <tbody>${body}
           <tr class="parent"><td>All projects</td>${cell(tot)}<td class="hours">${nEntries}</td></tr></tbody>
       </table>`;
    document.getElementById('tochart').onclick = renderTeamActual;
  }

  // One central "Updated" label by the Refresh button, reflecting the data behind the
  // active tab (estimated pull for team/estimated; logged-hours pull for the rest).
  function setUpdatedLabel() {
    const el = document.getElementById('dash-updated');
    if (!el) return;
    let u = null;
    if (dashTab === 'settings') u = null;
    else if (dashTab === 'team') u = teamData && teamData.updated;
    else if (dashTab === 'estimated' || dashTab === 'estproj') u = estData && estData[0] && estData[0].updated;
    else u = loggedData && loggedData[0] && loggedData[0].updated;
    el.textContent = u ? ('Updated ' + u) : '';
  }

  function renderTab() {
    destroyCharts();   // leaving any chart tab
    estDrillGroup = null; actualDrillGroup = null;   // switching tabs collapses any drilled-in bucket
    document.querySelectorAll('.nav-item').forEach(a =>
      a.classList.toggle('active', a.dataset.tab === dashTab));
    setUpdatedLabel();
    // Titles stay clean: the From/To filter under range-based tabs is the single
    // place that shows the selected date range.
    document.getElementById('tab-title').textContent = TABS[dashTab].title;
    const sub = TABS[dashTab].sub || '', subEl = document.getElementById('tab-sub');
    subEl.textContent = sub; subEl.style.display = sub ? '' : 'none';
    const loading = note(LOADING);

    if (dashTab === 'team') {
      teamData ? renderTeam() : (view.innerHTML = loading);
    } else if (dashTab === 'teamactual') {
      renderTeamActual();
    } else if (dashTab === 'estproj') {
      renderEstProj();
    } else if (dashTab === 'actualproj') {
      renderActualProj();
    } else if (dashTab === 'actualitems') {
      renderActualItems();
    } else if (dashTab === 'actualdaily') {
      renderActualDaily();
    } else if (dashTab === 'estimated') {
      if (!estData) view.innerHTML = loading;
      else {
        // Archived projects sit in their own section below the active ones and stay out of
        // the headline stats, which are about the work in flight.
        const live = estData.filter(w => !isArchived(w)), arch = estData.filter(isArchived);
        const summary = summaryBar([
          { n: h2(live.reduce((a, w) => a + (w.hours || 0), 0)) + ' h', l: 'Estimated remaining' },
          { n: live.length, l: 'Projects' },
          { n: live.reduce((a, w) => a + (w.ntasks || 0), 0), l: 'Tasks' },
          { n: h2(arch.reduce((a, w) => a + (w.hours || 0), 0)) + ' h', l: 'Archived (est.)' },
        ], 'est');
        cardGrid(live, estCard, 'No projects.', summary);
        if (arch.length) {
          view.insertAdjacentHTML('beforeend', '<h2 class="section-h">Archived projects</h2>');
          const g = document.createElement('div');
          g.className = 'grid';
          arch.forEach(w => g.appendChild(estCard(w)));
          view.appendChild(g);
        }
      }
    } else if (dashTab === 'capacity') {
      const picker = rangePicker();
      if (!loggedData) { view.innerHTML = picker + loading; wireRangeSel(); }
      else {
        // MSA projects with a monthly capacity lead the list and are highlighted: combined
        // budget buckets first, then any standalone capped project (group members roll into
        // their bucket). Everything else that logged hours follows as plain statistics cards.
        const memberGids = new Set((groupsConfig || []).flatMap(g => g.gids));
        const groups = (groupsConfig || []).map(g => buildGroupSummary(g, loggedData))
                         .filter(g => g.members.length);
        const standalone = loggedData.filter(w => w.cap != null && !memberGids.has(w.gid))
                             .sort((a, b) => b.hours - a.hours);
        // Archived projects still show their logged hours, but in a section of their own at
        // the bottom rather than mixed in with the active work.
        const others = loggedData.filter(w => w.cap == null && !memberGids.has(w.gid) && !isArchived(w))
                         .sort((a, b) => b.hours - a.hours);
        const archived = loggedData.filter(w => isArchived(w) && w.hours > 0)
                           .sort((a, b) => b.hours - a.hours);
        const hasCap = groups.length || standalone.length;
        // Headline stats cover the budgeted work only — the retainer picture in one line.
        const budgeted = [...groups, ...standalone];
        const totCap = budgeted.reduce((a, w) => a + (w.cap || 0), 0);
        const totUsed = budgeted.reduce((a, w) => a + (w.hours || 0), 0);
        const left = r2(totCap - totUsed);
        const overCount = budgeted.filter(w => (w.hours || 0) > (w.cap || 0)).length;
        const summary = summaryBar([
          { n: h2(totCap) + ' h', l: 'Budgeted capacity' },
          { n: h2(totUsed) + ' h', l: 'Hours used' },
          { n: h2(left) + ' h', l: left < 0 ? 'Over budget' : 'Remaining', neg: left < 0 },
          { n: (totCap ? (totUsed / totCap * 100).toFixed(0) : '0') + '%', l: 'Budget used' },
          { n: overCount, l: overCount === 1 ? 'Budget over' : 'Budgets over', neg: overCount > 0 },
        ]);
        view.innerHTML = (hasCap ? summary : '') + picker;
        if (!hasCap && !others.length && !archived.length) {
          view.insertAdjacentHTML('beforeend', note(`No hours logged in ${rangeLabel(dateStart, dateEnd)}.`));
        } else {
          if (hasCap) {
            view.insertAdjacentHTML('beforeend', '<h2 class="section-h flush">MSA projects · monthly capacity</h2>');
            const capGrid = document.createElement('div');
            capGrid.className = 'grid';
            groups.forEach(g => { const c = groupCard(g); c.classList.add('cap'); capGrid.appendChild(c); });
            standalone.forEach(w => { const c = capCard(w); c.classList.add('cap'); capGrid.appendChild(c); });
            view.appendChild(capGrid);
          }
          const section = (title, rows, flush) => {
            if (!rows.length) return;
            view.insertAdjacentHTML('beforeend',
              `<h2 class="section-h${flush ? ' flush' : ''}">${esc(title)}</h2>`);
            const grid = document.createElement('div');
            grid.className = 'grid';
            rows.forEach(w => grid.appendChild(loggedCard(w)));
            view.appendChild(grid);
          };
          section('Other projects', others, !hasCap);
          section('Archived projects', archived, !hasCap && !others.length);
        }
        wireRangeSel();
      }
    } else if (dashTab === 'settings') {
      renderSettings();
    }
  }

  // Settings: per-person graph colors. Choices persist to localStorage (see personColor)
  // and take effect on any per-person chart the next time it's drawn.
  function renderSettings() {
    // Everyone we might color: known team + Unassigned, plus anyone seen in a chart this
    // session, plus anyone who already has a saved color.
    const people = [...new Set([...TEAM_MEMBERS, 'Unassigned', ..._seenPeople, ...Object.keys(personColorConfig)])];
    const rows = people.map(p => {
      const custom = p in personColorConfig;
      return `<div class="color-row" data-person="${esc(p)}">
          <input type="color" class="color-pick" value="${personColor(p)}">
          <span class="color-name">${esc(p)}</span>
          <button class="reset-one"${custom ? '' : ' disabled'}>Reset</button>
        </div>`;
    }).join('');
    view.innerHTML = `<div class="panel">
        <div class="color-list">${rows}</div>
        <div class="color-actions"><button class="btn back" id="reset-all">Reset all to defaults</button></div>
        <p class="hint">Applies to the per-person stacked bar charts and the Unassigned bar on Team Capacity. Open a chart tab to see the change.</p>
      </div>`;
    view.querySelectorAll('.color-row').forEach(row => {
      const name = row.dataset.person;
      const pick = row.querySelector('.color-pick'), reset = row.querySelector('.reset-one');
      pick.oninput = () => { personColorConfig[name] = pick.value; savePersonColorConfig(); reset.disabled = false; };
      reset.onclick = () => { delete personColorConfig[name]; savePersonColorConfig(); pick.value = personDefaultColor(name); reset.disabled = true; };
    });
    document.getElementById('reset-all').onclick = () => { personColorConfig = {}; savePersonColorConfig(); renderSettings(); };
  }

  wireSidebar(renderTab);

  // Loads the selected range's "Actual Hours" widgets (used on its own when the range changes).
  async function loadLogged(refresh) {
    const s = dateStart, e = dateEnd;   // capture so a fast re-pick doesn't paint stale data
    personStatsCache = {}; projPersonCache = {}; itemStatsCache = {}; dailyStatsCache = {}; personLoading = {};   // range/refresh changed — recompute per-person/per-item splits
    try {
      const url = `/api/logged?start=${s}&end=${e}` + (refresh ? '&refresh=1' : '');
      const j = await fetch(url).then(r => r.json());
      if (s !== dateStart || e !== dateEnd) return;   // a newer range was picked mid-flight
      loggedData = j;
    } catch (err) { view.innerHTML = fmtErr(err); return; }
    await mePromise;   // so Daily Log knows who "you" are before it picks its default assignee
    if (['capacity', 'actualproj', 'actualitems', 'actualdaily', 'teamactual'].includes(dashTab)) renderTab();
  }

  // Who owns the token, resolved once at startup and awaited before any render that could
  // apply a "you" default. A failure here is not worth an error banner — the filters simply
  // open on everyone — so it resolves to null instead of rejecting.
  const mePromise = fetch('/api/me').then(r => r.json())
    .then(j => { currentUser = (j && j.name) || null; })
    .catch(() => { currentUser = null; });

  async function loadAll(refresh) {
    btn.disabled = true; btn.textContent = refresh ? 'Refreshing…' : 'Refresh';
    const q = refresh ? '?refresh=1' : '';
    try {
      // Estimated and logged hours are pulled together and both awaited: one wait at startup,
      // after which every tab is already warm. Deferring the logged half would paint sooner
      // but move the wait onto the first Actual tab you open, which is worse.
      const [e, grp] = await Promise.all([
        fetch('/api/projects' + q).then(r => r.json()),
        groupsConfig ? Promise.resolve(groupsConfig) : fetch('/api/groups').then(r => r.json()),
        loadLogged(refresh),
      ]);
      estData = e; groupsConfig = grp;
      // Team load derives from the per-project detail that /api/projects just (re)built,
      // so it reads the warm cache — no refresh flag, no duplicate Asana pulls.
      teamData = await fetch('/api/assignees').then(r => r.json());
    } catch (err) { view.innerHTML = fmtErr(err); }
    renderTab();
    btn.disabled = false; btn.textContent = 'Refresh';
  }
  btn.onclick = () => loadAll(true);
  renderTab();        // paint sidebar + active tab immediately (shows "Loading…")
  loadAll(false);     // navigation = cached
}

async function renderDetail(gid) {
  app.innerHTML = `
    <div class="layout">
      ${sidebarHtml()}
      <main class="content">
        <div id="crumbs" class="crumbs">Loading…</div>
        <div class="head">
          <h1 id="page-title"></h1>
          <div class="head-right">
            <span id="dash-updated" class="dash-updated"></span>
            <button class="btn" id="refresh">Refresh</button>
          </div>
        </div>
        <p class="sub" id="sub"></p>
        <div id="view"></div>
      </main>
    </div>`;
  wireSidebar(null);
  const toDash = () => { location.hash = ''; };
  const btn = document.getElementById('refresh');
  let detailData = null;

  // This page is a drill-in from Projects · Estimated, so it reads that tab's filters rather
  // than starting fresh: the status checkboxes and "Exclude Unassigned". Both are module-level
  // and hash routing never reloads the page, so they survive the navigation.
  const estStatusOn = s => !estStatusFilter || estStatusFilter.has(s);
  const estKept = t => estStatusOn(t.section || NO_STATUS);
  const estFilterNote = () =>
    (estStatusFilter ? ` · status: ${[...estStatusFilter].join(', ')}` : '') +
    (estHideUnassigned ? ' · Unassigned excluded' : '');

  function showChart() {
    const d = detailData;
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:d.name}]);
    // Re-aggregate per assignee from the task rows rather than using the payload's precomputed
    // totals, so the status filter can be applied on the way — a filtered bar here then equals
    // that person's slice of the bar clicked on Projects · Estimated. A subtask takes its
    // parent's status column, exactly as the dashboard chart treats it.
    const agg = {};
    const add = (name, est, act) => {
      const a = agg[name] || (agg[name] = { est: 0, act: 0, count: 0 });
      a.est += est || 0; a.act += act || 0; a.count += 1;
    };
    d.tasks.filter(estKept).forEach(t => {
      add(t.assignee, t.hours, t.actual);
      t.subtasks.forEach(s => add(s.assignee, s.hours, s.actual));
    });
    // Bars are REMAINING hours (estimated − time already tracked), the same measure as the
    // Projects · Estimated chart this page is clicked from, sorted most-remaining-first.
    // Over-run people would push the axis below zero, so bars floor at 0 and the true numbers
    // stay in the tooltip and the task table.
    const people = Object.entries(agg)
      .filter(([name]) => !(estHideUnassigned && name === 'Unassigned'))
      .map(([name, a]) => ({ name, est: r2(a.est), act: r2(a.act), count: a.count, rem: r2(a.est - a.act) }))
      .sort((a, b) => b.rem - a.rem);
    const labels = people.map(p => p.name), rem = people.map(p => p.rem);
    const barHours = rem.map(h => Math.max(0, h));
    destroyCharts();
    if (!people.length) {
      document.getElementById('view').innerHTML =
        noteBox('No tasks match the filters carried over from the Bar Chart' + estFilterNote() + '.');
      return;
    }
    document.getElementById('view').innerHTML =
      '<div class="chart-box"><canvas id="chart"></canvas></div>';
    chart = new Chart(document.getElementById('chart'), {
      type:'bar',
      // Per-bar colors, not one flat blue: the same hue this person has on every other
      // per-person chart (and in the Graph Colors tab).
      data:{ labels, datasets:[{ label:'Remaining hours', data:barHours,
        backgroundColor:labels.map(personColor), borderColor:labels.map(personColor),
        borderWidth:1, _people:people }] },
      options:{ responsive:true, maintainAspectRatio:false,
        onClick:(evt, els) => { if (els.length) showTasks(labels[els[0].index]); },
        onHover:(evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins:{ legend:{display:false}, tooltip:{ callbacks:{
          label: ctx => `Remaining: ${h2(ctx.dataset._people[ctx.dataIndex].rem)} h`,
          afterLabel: ctx => { const p = ctx.dataset._people[ctx.dataIndex];
            return `Est ${h2(p.est)} − actual ${h2(p.act)} · ${plural(p.count, 'item')}`; } } } },
        scales:{ x:{ title:{display:true,text:'Assignee'}, ticks:{ callback:(v,i) => [labels[i], h2(rem[i]) + ' h'] } },
                 y:{ min:0, title:{display:true,text:'Remaining hours'}, ticks:{ callback:v => h2(v) } } } }
    });
  }

  function showTasks(assignee) {
    destroyCharts();
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:detailData.name, fn:showChart}, {label:assignee}]);
    // Same status filter the chart is showing, so this table adds up to the clicked bar.
    const all = detailData.tasks.filter(estKept);
    // Est / actual / remaining per row, so the column of remaining hours adds up to the bar.
    let rows = '', totEst = 0, totAct = 0, items = 0;
    const remCell = (est, act) => `<td class="hours">${h2(r2(est - act))} h</td>`;

    function subRow(s, context) {
      totEst += s.hours; totAct += s.actual || 0; items++;
      // Subtasks carry no status column of their own, so the cell states what the row is.
      return `<tr class="sub"><td class="sub-name">${esc(s.name)}${context}</td>` +
        `<td><span class="badge none">Subtask</span></td>` +
        `<td class="hours">${s.hours ? h2(s.hours) + ' h' : '—'}</td>` +
        `<td class="hours">${s.actual ? h2(s.actual) + ' h' : '—'}</td>` +
        remCell(s.hours, s.actual || 0) + '</tr>';
    }

    // 1. Tasks owned by this assignee, with only THEIR subtasks nested underneath.
    all.filter(t => t.assignee === assignee).forEach(t => {
      totEst += t.hours; totAct += t.actual || 0; items++;
      rows += `<tr class="parent"><td>${esc(t.name)}</td>` +
        `<td>${t.section ? `<span class="badge">${esc(t.section)}</span>`
                         : `<span class="badge none">${NO_STATUS}</span>`}</td>` +
        `<td class="hours">${h2(t.hours)} h</td>` +
        `<td class="hours">${h2(t.actual || 0)} h</td>` +
        remCell(t.hours, t.actual || 0) + '</tr>';
      t.subtasks.filter(s => s.assignee === assignee).forEach(s => { rows += subRow(s, ''); });
    });

    // 2. This assignee's subtasks that live under someone else's task.
    all.filter(t => t.assignee !== assignee).forEach(t => {
      t.subtasks.filter(s => s.assignee === assignee).forEach(s => {
        rows += subRow(s, ` <span class="muted">(under "${esc(t.name)}" · ${esc(t.assignee)})</span>`);
      });
    });

    if (!items) rows = '<tr><td colspan="5" class="muted">No items.</td></tr>';
    document.getElementById('view').innerHTML =
      `<div class="drill-head">
         <button class="btn back" id="tochart">← Back to chart</button>
         <h2>${esc(assignee)}</h2>
       </div>
       <p class="drill-total">${plural(items, 'item')} · ${h2(r2(totEst - totAct))} h remaining · ${h2(totEst)} est − ${h2(totAct)} actual (excludes Completed)${esc(estFilterNote())}</p>
       <table class="tasks">
         <thead><tr><th>Task / Subtask</th><th>Type / Status</th><th class="hours">Est.</th><th class="hours">Actual</th><th class="hours">Remaining</th></tr></thead>
         <tbody>${rows}</tbody>
       </table>`;
    document.getElementById('tochart').onclick = showChart;
  }

  async function load(refresh) {
    btn.disabled = true; btn.textContent = refresh ? 'Refreshing…' : 'Refresh';
    try {
      detailData = await (await fetch('/api/project/' + gid + (refresh ? '?refresh=1' : ''))).json();
      document.getElementById('page-title').textContent = detailData.name;
      document.getElementById('sub').textContent =
        `Remaining estimated hours per assignee (estimated − time tracked) · ${plural(detailData.ntasks, 'task')} (excludes Completed)` +
        estFilterNote();
      document.getElementById('dash-updated').textContent = detailData.updated ? ('Updated ' + detailData.updated) : '';
      showChart();
    } catch (e) { document.getElementById('view').innerHTML = fmtErr(e); }
    finally { btn.disabled = false; btn.textContent = 'Refresh'; }
  }
  btn.onclick = () => load(true);
  load(false);   // navigation = cached
}

async function renderLoggedDetail(gid) {
  app.innerHTML = `
    <div class="layout">
      ${sidebarHtml()}
      <main class="content">
        <div id="crumbs" class="crumbs">Loading…</div>
        <div class="head">
          <h1 id="page-title"></h1>
          <div class="head-right">
            <span id="dash-updated" class="dash-updated"></span>
            <button class="btn" id="refresh">Refresh</button>
          </div>
        </div>
        <p class="sub" id="sub"></p>
        <div id="view"></div>
      </main>
    </div>`;
  wireSidebar(null);
  const toDash = () => { location.hash = ''; };
  const btn = document.getElementById('refresh');
  let data = null;

  function showChart() {
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:data.name}]);
    document.getElementById('view').innerHTML =
      '<div class="chart-box"><canvas id="chart"></canvas></div>';
    destroyCharts();
    chart = new Chart(document.getElementById('chart'), {
      type:'bar',
      // Per-bar colors, not one flat green: each person keeps the hue they carry on every
      // other per-person chart (and in the Graph Colors tab).
      data:{ labels:data.labels, datasets:[
        { label:'Hours logged', data:data.hours,
          backgroundColor:data.labels.map(personColor), borderColor:data.labels.map(personColor),
          borderWidth:1 } ] },
      options:{ responsive:true, maintainAspectRatio:false,
        onClick:(evt, els) => { if (els.length) showEntries(data.labels[els[0].index]); },
        onHover:(evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins:{ legend:{display:false}, tooltip:{ callbacks:{
          label: ctx => `Logged: ${h2(ctx.parsed.y)} h`,
          afterLabel: ctx => plural(data.counts[ctx.dataIndex], 'entry', 'entries') } } },
        scales:{ x:{ title:{display:true,text:'Logged by'}, ticks:{ callback:(v,i) => [data.labels[i], h2(data.hours[i]) + ' h'] } },
                 y:{ beginAtZero:true, title:{display:true,text:'Hours logged'}, ticks:{ callback:v => h2(v) } } } }
    });
  }

  function showEntries(person) {
    destroyCharts();
    const ml = rangeLabel(data.start, data.end);
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:data.name, fn:showChart}, {label:person}]);
    const rows_ = data.entries.filter(e => e.by === person);
    const total = rows_.reduce((a, e) => a + e.hours, 0);
    let rows = '';
    rows_.forEach(e => {
      rows += `<tr><td>${esc(e.date)}</td><td>${esc(e.task)}</td>` +
        `<td class="hours">${h2(e.hours)} h</td></tr>`;
    });
    if (!rows_.length) rows = '<tr><td colspan="3" class="muted">No entries.</td></tr>';
    document.getElementById('view').innerHTML =
      `<div class="drill-head">
         <button class="btn back" id="tochart">← Back to chart</button>
         <h2>${esc(person)}</h2>
       </div>
       <p class="drill-total">${plural(rows_.length, 'entry', 'entries')} · ${h2(total)} h logged in ${ml}</p>
       <table class="tasks">
         <thead><tr><th>Date</th><th>Task</th><th class="hours">Hours</th></tr></thead>
         <tbody>${rows}</tbody>
       </table>`;
    document.getElementById('tochart').onclick = showChart;
  }

  async function load(refresh) {
    btn.disabled = true; btn.textContent = refresh ? 'Refreshing…' : 'Refresh';
    try {
      data = await (await fetch('/api/logged/' + gid + `?start=${dateStart}&end=${dateEnd}` + (refresh ? '&refresh=1' : ''))).json();
      document.getElementById('page-title').textContent = data.name;
      document.getElementById('sub').textContent =
        `Hours logged ${rangeLabel(data.start, data.end)} per person · ${plural(data.nentries, 'time entry', 'time entries')} · ${h2(data.total_hours)} h total`;
      document.getElementById('dash-updated').textContent = data.updated ? ('Updated ' + data.updated) : '';
      showChart();
    } catch (e) { document.getElementById('view').innerHTML = fmtErr(e); }
    finally { btn.disabled = false; btn.textContent = 'Refresh'; }
  }
  btn.onclick = () => load(true);
  load(false);
}

// A combined budget bucket (e.g. CMD) on its own page: every member project in one chart
// against the shared monthly cap, so a PM can see which member is eating the bucket.
// Reads the range the dashboard was left on (no picker here, same as the other detail pages).
async function renderGroupDetail(name) {
  app.innerHTML = `
    <div class="layout">
      ${sidebarHtml()}
      <main class="content">
        <div id="crumbs" class="crumbs">${LOADING}</div>
        <div class="head">
          <h1 id="page-title"></h1>
          <div class="head-right">
            <span id="dash-updated" class="dash-updated"></span>
            <button class="btn" id="refresh">Refresh</button>
          </div>
        </div>
        <p class="sub" id="sub"></p>
        <div id="view">${note(LOADING)}</div>
      </main>
    </div>`;
  wireSidebar(null);
  const toDash = () => { location.hash = ''; };
  const btn = document.getElementById('refresh');

  function show(g) {
    const view = document.getElementById('view');
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:g.name}]);
    const used = Number(g.hours || 0), cap = Number(g.cap || 0), left = r2(cap - used);
    const members = [...g.members].sort((a, b) => (b.hours || 0) - (a.hours || 0));
    view.innerHTML = summaryBar([
      { n: h2(cap) + ' h', l: 'Capacity h/mo' },
      { n: h2(used) + ' h', l: 'Hours used' },
      { n: h2(left) + ' h', l: left < 0 ? 'Over budget' : 'Remaining', neg: left < 0 },
      { n: (cap ? (used / cap * 100).toFixed(0) : '0') + '%', l: 'Budget used' },
      { n: members.length, l: members.length === 1 ? 'Project' : 'Projects' },
    ]) + `<div class="cap-wide">${capBar(used, cap)}</div>`;
    if (!members.some(m => (m.hours || 0) > 0)) {
      view.insertAdjacentHTML('beforeend',
        noteBox(`No hours logged against ${g.name} in ${rangeLabel(dateStart, dateEnd)}.`));
      return;
    }
    view.insertAdjacentHTML('beforeend', '<div class="chart-box"><canvas id="chart"></canvas></div>');
    const labels = members.map(m => m.name), hours = members.map(m => r2(m.hours || 0));
    destroyCharts();
    chart = new Chart(document.getElementById('chart'), {
      type:'bar',
      // Each bar keeps the project's own stable color, so a member reads the same here as in
      // the Team Capacity donuts.
      data:{ labels, datasets:[{ label:'Hours logged', data:hours,
        backgroundColor:labels.map(projectColor), borderColor:labels.map(projectColor),
        borderWidth:1, _m:members }] },
      options:{ responsive:true, maintainAspectRatio:false,
        onClick:(evt, els) => { if (els.length) location.hash = '#/logged/' + members[els[0].index].gid; },
        onHover:(evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins:{ legend:{display:false},
          tooltip:{ callbacks:{
            label: ctx => `Logged: ${h2(ctx.parsed.y)} h`,
            afterLabel: ctx => { const m = ctx.dataset._m[ctx.dataIndex];
              return `${plural(m.nentries || 0, 'time entry', 'time entries')} · ` +
                `${used ? (m.hours / used * 100).toFixed(0) : '0'}% of the bucket`; } } } },
        scales:{ x:{ title:{display:true,text:'Project'},
                     ticks:{ callback:(v,i) => [labels[i], h2(hours[i]) + ' h'] } },
                 y:{ beginAtZero:true, title:{display:true,text:'Hours logged'},
                     ticks:{ callback:v => h2(v) } } } }
    });
    const rows = members.map(m =>
      `<tr class="parent" data-gid="${m.gid}"><td>${esc(m.name)}</td>` +
      `<td class="hours">${h2(m.hours)} h</td>` +
      `<td class="hours">${used ? (m.hours / used * 100).toFixed(0) : '0'}%</td>` +
      `<td class="hours">${cap ? (m.hours / cap * 100).toFixed(0) : '0'}%</td>` +
      `<td class="hours">${m.nentries || 0}</td></tr>`).join('');
    view.insertAdjacentHTML('beforeend',
      `<h2 class="section-h">Member projects</h2>
       <table class="tasks">
         <thead><tr><th>Project</th><th class="hours">Hours logged</th>
           <th class="hours">Share of bucket</th><th class="hours">Of capacity</th>
           <th class="hours">Entries</th></tr></thead>
         <tbody>${rows}</tbody>
       </table>`);
    view.querySelectorAll('tr.parent[data-gid]').forEach(r =>
      r.onclick = () => { location.hash = '#/logged/' + r.dataset.gid; });
  }

  async function load(refresh) {
    btn.disabled = true; btn.textContent = refresh ? 'Refreshing…' : 'Refresh';
    try {
      const q = `?start=${dateStart}&end=${dateEnd}` + (refresh ? '&refresh=1' : '');
      const [groups, logged] = await Promise.all([
        fetch('/api/groups').then(r => r.json()),
        fetch('/api/logged' + q).then(r => r.json()),
      ]);
      const cfg = (groups || []).find(g => g.name === name);
      if (!cfg) {
        setCrumbs([{label:'Dashboard', fn:toDash}, {label:name}]);
        document.getElementById('page-title').textContent = name;
        document.getElementById('view').innerHTML = note('No budget group named ' + name + '.');
        return;
      }
      const g = buildGroupSummary(cfg, logged);
      document.getElementById('page-title').textContent = g.name;
      document.getElementById('sub').textContent =
        `Combined monthly bucket · ${plural(g.members.length, 'project')} · ` +
        `hours logged ${rangeLabel(dateStart, dateEnd)}`;
      document.getElementById('dash-updated').textContent = g.updated ? ('Updated ' + g.updated) : '';
      show(g);
    } catch (e) { document.getElementById('view').innerHTML = fmtErr(e); }
    finally { btn.disabled = false; btn.textContent = 'Refresh'; }
  }
  btn.onclick = () => load(true);
  load(false);
}

function route() {
  destroyCharts();
  let m = location.hash.match(/^#\/logged\/(\d+)/);
  if (m) return renderLoggedDetail(m[1]);
  m = location.hash.match(/^#\/p\/(\d+)/);
  if (m) return renderDetail(m[1]);
  m = location.hash.match(/^#\/grp\/(.+)$/);
  if (m) return renderGroupDetail(decodeURIComponent(m[1]));
  renderDashboard();
}
window.addEventListener('hashchange', route);
route();
</script>
</body>
</html>
"""


# The shared UI script references a few constants that the static build (build_static.py)
# injects via its PREPEND. When serving the page ourselves we inject the same values here,
# in a <script> that runs before the UI script, so identifiers like TEAM_MEMBERS are defined.
def render_page():
    boot = ("<script>\nconst TEAM_MEMBERS = %s;\nconst PROJECT_ROSTER = %s;\n"
            "const ARCHIVED_GIDS = %s;\n</script>\n"
            % (json.dumps(TEAM_MEMBERS), json.dumps([p["name"] for p in PROJECTS]),
               json.dumps(sorted(ARCHIVED_GIDS))))
    return PAGE.replace('<div class="wrap" id="app"></div>\n<script>',
                        '<div class="wrap" id="app"></div>\n' + boot + '<script>', 1)


PAGE_HTML = render_page()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._t0, self._c0 = time.time(), req_snapshot()
        parts = self.path.split("?")
        path = parts[0]
        query = urllib.parse.parse_qs(parts[1] if len(parts) > 1 else "")
        refresh = query.get("refresh", [""])[0] == "1"
        start = query.get("start", [DEFAULT_START])[0]
        end = query.get("end", [DEFAULT_END])[0]
        if start > end:                      # tolerate inverted ranges
            start, end = end, start
        try:
            if path == "/api/projects":
                return self._json(200, get_summaries(refresh=refresh))
            if path == "/api/logged":
                return self._json(200, get_logged_summaries(refresh=refresh, start=start, end=end))
            if path == "/api/assignees":
                return self._json(200, get_assignee_load(refresh=refresh))
            if path == "/api/groups":
                return self._json(200, GROUPS)
            if path == "/api/me":
                return self._json(200, get_me(refresh=refresh))
            if path.startswith("/api/logged/"):
                gid = path.rsplit("/", 1)[-1]
                return self._json(200, get_logged_detail(gid, refresh=refresh, start=start, end=end))
            if path.startswith("/api/project/"):
                gid = path.rsplit("/", 1)[-1]
                return self._json(200, get_detail(gid, refresh=refresh))
            if path == "/" or path.startswith("/index"):
                return self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode())
            self._send(404, "text/plain", b"Not found")
        except urllib.error.HTTPError as e:
            self._json(502, {"error": f"Asana {e.code}"})

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        # What this route actually cost, by request kind — so a slow load can be read off the
        # console instead of guessed at ("/api/logged  48 Asana calls (batch 40, tasks 8)").
        after = req_snapshot()
        spent = {k: v - self._c0.get(k, 0) for k, v in after.items() if v - self._c0.get(k, 0)}
        total = sum(spent.values())
        detail = ", ".join(f"{k} {n}" for k, n in sorted(spent.items(), key=lambda x: -x[1]))
        print(f"{self.path.split('?')[0]:<22} {total:>4} Asana calls"
              f"  {time.time() - self._t0:5.1f}s  {len(body) / 1024:6.0f} KB"
              + (f"  ({detail})" if detail else ""))
        self._send(code, "application/json", body)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # quiet


def main():
    url = f"http://localhost:{PORT}"
    warm = load_entry_cache()
    print(f"Asana dashboard running at {url}  (Ctrl+C to stop)")
    if warm:
        print(f"Time-entry cache warm for {warm} tasks — only tasks whose tracked time changed "
              f"will be re-read. Click Refresh to force a full re-read.")
    webbrowser.open(url)
    ThreadingHTTPServer(("localhost", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
