# Federal Register Monitor

Tracks U.S. Federal Register documents relevant to Canadian trade, industry and
policy interests. Same pattern as the Canada Gazette Regulatory Monitor
(Flask + SQLite + cron), but fed by the Federal Register's public REST API —
no scraping, no API key.

## Data sources
- **Documents API** — `api/v1/documents.json`: the official published record.
- **Public Inspection API** — `api/v1/public-inspection-documents/current.json`:
  documents filed ahead of publication (the advance-warning layer). Regular
  filings post at 8:45 a.m. ET; FR flags off-schedule filings as
  `filing_type: "special"`, which this monitor uses as the emergency signal.

## Files
- `config.json` — watchlist (agency name substrings + search terms) and
  document types. Edit freely; matching runs at ingest time.
- `models.py` — SQLite schema (`documents` table keyed on `document_number`).
- `ingest.py` — poll job: `python3 ingest.py public-inspection` or
  `python3 ingest.py published [--date YYYY-MM-DD]`. Add `--force` to run on
  weekends/US federal holidays (normally skipped).
- `app.py` — Flask app on **port 5007**: dashboard + Cowork API.

## Ingestion behaviour
- Upserts on `document_number`. When a doc moves from Public Inspection to
  Published, the row is updated in place (stage, official pub date, abstract,
  significance flag) — `scheduled_pub_date`, `first_seen_at` and `notified_at`
  are preserved, and matched terms are merged.
- Watchlist matching: agency substring match (against FR agency names) OR
  word-boundary term match in title + abstract. Non-matching docs are stored
  too (`is_watchlist_hit = 0`) for archive value.
- Urgent (`is_urgent = 1`) if watchlist hit AND any of:
  - `PRESDOCU` (proclamation / executive order)
  - `significant = true` and type RULE/PRORULE
  - Public Inspection `filing_type = "special"` (emergency/off-schedule filing)

## Cowork API
- `GET /api/pending?type=urgent` — urgent hits with `notified_at IS NULL`
- `GET /api/pending?type=digest` — non-urgent hits with `notified_at IS NULL`
- `POST /api/mark_notified` — `{"document_numbers": ["2026-12345", ...]}`
- `GET /api/health`

## Cron schedule (ET = Toronto local, Mon–Fri; holidays skipped in-code)
```
0  6  * * 1-5  published            # official record for today
0  9  * * 1-5  public-inspection    # 8:45 a.m. regular filings
30 11 * * 1-5  public-inspection    # ~11:15 a.m. updates
30 16 * * 1-5  public-inspection    # ~4:15 p.m. updates + special filings
```
All entries `cd` into this directory and append to `ingest.log`.

## Run the dashboard
```
python3 app.py    # http://localhost:5007
```
