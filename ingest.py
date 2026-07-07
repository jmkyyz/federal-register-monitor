"""Federal Register Monitor — ingestion job.

Usage:
    python3 ingest.py public-inspection            # poll PI current.json
    python3 ingest.py published [--date YYYY-MM-DD]  # poll main Documents API
    (add --force to run on a weekend/US federal holiday)

Clean JSON API, no scraping. Request failures are logged and the job exits
gracefully rather than crashing the cron run.
"""

import argparse
import datetime as dt
import json
import re
import sys

import requests

from models import get_db, init_db

BASE = "https://www.federalregister.gov/api/v1"
CONFIG_PATH = __file__.rsplit("/", 1)[0] + "/config.json"

# The API returns human-readable type names; normalize to FR type codes.
TYPE_CODES = {
    "rule": "RULE",
    "proposed rule": "PRORULE",
    "notice": "NOTICE",
    "presidential document": "PRESDOCU",
}


def log(msg):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# US federal holiday check (FR publishes nothing on these days)
# ---------------------------------------------------------------------------

def _nth_weekday(year, month, weekday, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = dt.date(year, month + 1, 1) if month < 12 else dt.date(year + 1, 1, 1)
    d -= dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    if d.weekday() == 5:  # Saturday -> Friday
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday
        return d + dt.timedelta(days=1)
    return d


def us_federal_holidays(year):
    fixed = [dt.date(year, 1, 1), dt.date(year, 6, 19), dt.date(year, 7, 4),
             dt.date(year, 11, 11), dt.date(year, 12, 25)]
    floating = [
        _nth_weekday(year, 1, 0, 3),    # MLK Day
        _nth_weekday(year, 2, 0, 3),    # Washington's Birthday
        _last_weekday(year, 5, 0),      # Memorial Day
        _nth_weekday(year, 9, 0, 1),    # Labor Day
        _nth_weekday(year, 10, 0, 2),   # Columbus Day
        _nth_weekday(year, 11, 3, 4),   # Thanksgiving
    ]
    return {_observed(d) for d in fixed} | set(floating)


def is_federal_business_day(d):
    return d.weekday() < 5 and d not in us_federal_holidays(d.year)


# ---------------------------------------------------------------------------
# Watchlist matching
# ---------------------------------------------------------------------------

def match_watchlist(config, title, abstract, agency_names):
    """Return list of watchlist agencies/terms that hit this document."""
    hits = []
    agency_blob = " | ".join(agency_names).lower()
    for agency in config["watchlist"]["agencies"]:
        if agency.lower() in agency_blob:
            hits.append(agency)
    text = f"{title or ''} {abstract or ''}"
    for term in config["watchlist"]["terms"]:
        # Word-boundary match so e.g. "Canada" doesn't fire on "Canadarm" text runs
        if re.search(r"\b" + re.escape(term) + r"\b", text, re.IGNORECASE):
            hits.append(term)
    return hits


def compute_urgent(doc_type, significant, source_stage, filing_type, is_hit):
    if not is_hit:
        return False
    if doc_type == "PRESDOCU":
        return True
    if significant and doc_type in ("RULE", "PRORULE"):
        return True
    # PI 'special' filings are the emergency/off-schedule track (regular
    # filings post at 8:45 a.m. ET; anything else is flagged special by FR)
    if source_stage == "public_inspection" and filing_type == "special":
        return True
    return False


# ---------------------------------------------------------------------------
# Fetch + normalize
# ---------------------------------------------------------------------------

def fetch_json(url, params=None, timeout=30):
    resp = requests.get(url, params=params, timeout=timeout,
                        headers={"User-Agent": "federal-register-monitor (personal watchlist)"})
    resp.raise_for_status()
    return resp.json()


def normalize_type(raw):
    if not raw:
        return None
    return TYPE_CODES.get(raw.strip().lower(), raw.strip().upper())


def agency_names_of(doc):
    """Prefer parsed agency names; fall back to agency_names strings.
    Case-insensitive dedup (raw_name is often just the uppercase variant)."""
    dicts = [a for a in doc.get("agencies") or [] if isinstance(a, dict)]
    candidates = [a.get("name") or a.get("raw_name") for a in dicts]
    if not candidates:
        candidates = doc.get("agency_names") or []
    names, seen = [], set()
    for n in candidates:
        n = (n or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)
    return names


def fetch_public_inspection(timeout):
    data = fetch_json(f"{BASE}/public-inspection-documents/current.json", timeout=timeout)
    docs = []
    for d in data.get("results", []):
        docs.append({
            "document_number": d.get("document_number"),
            "title": (d.get("title") or "").strip(),
            "document_type": normalize_type(d.get("type")),
            "agency_names": agency_names_of(d),
            "abstract": d.get("excerpts"),          # PI docs rarely have abstracts
            "publication_date": None,               # not officially published yet
            "scheduled_pub_date": d.get("publication_date"),
            "significant": None,                    # not exposed at PI stage
            "filing_type": d.get("filing_type"),
            "html_url": d.get("html_url"),
            "pdf_url": d.get("pdf_url"),
            "source_stage": "public_inspection",
        })
    return docs


def fetch_published(date, doc_types, timeout):
    params = {
        "per_page": 1000,
        "conditions[publication_date][is]": date.isoformat(),
        "fields[]": ["document_number", "title", "type", "abstract", "agencies",
                     "agency_names", "publication_date", "significant",
                     "html_url", "pdf_url"],
        "conditions[type][]": doc_types,
    }
    docs, url = [], f"{BASE}/documents.json"
    while url:
        data = fetch_json(url, params=params, timeout=timeout)
        params = None  # next_page_url already carries the query string
        for d in data.get("results", []):
            docs.append({
                "document_number": d.get("document_number"),
                "title": (d.get("title") or "").strip(),
                "document_type": normalize_type(d.get("type")),
                "agency_names": agency_names_of(d),
                "abstract": d.get("abstract"),
                "publication_date": d.get("publication_date"),
                "scheduled_pub_date": None,
                "significant": d.get("significant"),
                "filing_type": None,
                "html_url": d.get("html_url"),
                "pdf_url": d.get("pdf_url"),
                "source_stage": "published",
            })
        url = data.get("next_page_url")
    return docs


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert(conn, config, doc):
    """Insert or merge one normalized document. Returns 'new', 'updated' or 'unchanged'."""
    matched = match_watchlist(config, doc["title"], doc["abstract"], doc["agency_names"])
    is_hit = bool(matched)
    urgent = compute_urgent(doc["document_type"], doc["significant"],
                            doc["source_stage"], doc["filing_type"], is_hit)
    now = dt.datetime.now().isoformat(timespec="seconds")

    old = conn.execute("SELECT * FROM documents WHERE document_number = ?",
                       (doc["document_number"],)).fetchone()
    if old is None:
        conn.execute(
            """INSERT INTO documents (document_number, title, document_type, agencies,
                   abstract, publication_date, scheduled_pub_date, significant,
                   filing_type, html_url, pdf_url, source_stage, matched_terms,
                   is_watchlist_hit, is_urgent, first_seen_at, notified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (doc["document_number"], doc["title"], doc["document_type"],
             json.dumps(doc["agency_names"]), doc["abstract"],
             doc["publication_date"], doc["scheduled_pub_date"], doc["significant"],
             doc["filing_type"], doc["html_url"], doc["pdf_url"],
             doc["source_stage"], json.dumps(matched),
             int(is_hit), int(urgent), now))
        return "new"

    # A doc already marked published never regresses to public_inspection
    # (PI current.json keeps listing docs until their pub date).
    if old["source_stage"] == "published" and doc["source_stage"] == "public_inspection":
        return "unchanged"

    merged_terms = sorted(set(json.loads(old["matched_terms"] or "[]")) | set(matched))
    conn.execute(
        """UPDATE documents SET title = ?, document_type = ?, agencies = ?,
               abstract = COALESCE(?, abstract),
               publication_date = COALESCE(?, publication_date),
               scheduled_pub_date = COALESCE(scheduled_pub_date, ?),
               significant = COALESCE(?, significant),
               filing_type = COALESCE(filing_type, ?),
               html_url = ?, pdf_url = ?, source_stage = ?,
               matched_terms = ?, is_watchlist_hit = ?, is_urgent = ?
           WHERE document_number = ?""",
        (doc["title"], doc["document_type"], json.dumps(doc["agency_names"]),
         doc["abstract"], doc["publication_date"], doc["scheduled_pub_date"],
         doc["significant"], doc["filing_type"],
         doc["html_url"], doc["pdf_url"], doc["source_stage"],
         json.dumps(merged_terms),
         int(is_hit or bool(old["is_watchlist_hit"])),
         int(urgent or bool(old["is_urgent"])),
         doc["document_number"]))
    return "updated" if doc["source_stage"] != old["source_stage"] else "unchanged"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(source, date=None, force=False):
    config = load_config()
    today = dt.date.today()
    if not force and not is_federal_business_day(today):
        log(f"{today} is a weekend/US federal holiday — skipping {source} poll")
        return

    timeout = config.get("request_timeout_seconds", 30)
    try:
        if source == "public-inspection":
            docs = fetch_public_inspection(timeout)
        else:
            docs = fetch_published(date or today, config["document_types"], timeout)
    except requests.RequestException as exc:
        log(f"ERROR fetching {source}: {exc}")
        sys.exit(0)  # logged, not crashed — next cron run will retry

    init_db()
    conn = get_db()
    counts = {"new": 0, "updated": 0, "unchanged": 0}
    hits = urgents = 0
    for doc in docs:
        if not doc["document_number"]:
            continue
        counts[upsert(conn, config, doc)] += 1
    conn.commit()
    row = conn.execute(
        "SELECT SUM(is_watchlist_hit), SUM(is_urgent) FROM documents"
        " WHERE notified_at IS NULL").fetchone()
    hits, urgents = row[0] or 0, row[1] or 0
    conn.close()
    log(f"{source}: fetched {len(docs)} docs — {counts['new']} new, "
        f"{counts['updated']} stage-updated; pending unnotified: "
        f"{hits} watchlist hits ({urgents} urgent)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("source", choices=["public-inspection", "published"])
    ap.add_argument("--date", type=dt.date.fromisoformat, default=None,
                    help="publication date for the published poll (default today)")
    ap.add_argument("--force", action="store_true",
                    help="run even on weekends/holidays")
    args = ap.parse_args()
    run(args.source, date=args.date, force=args.force)
