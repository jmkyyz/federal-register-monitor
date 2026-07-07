"""Federal Register Monitor — Flask app.

Routes:
  /                    read-only dashboard of stored documents with filters
  /api/pending         ?type=urgent|digest — unnotified watchlist hits for Cowork
  /api/mark_notified   POST {"document_numbers": [...]} — stamp notified_at
  /api/health          liveness check
  /run-ingest          manual poll trigger (both sources)
"""

import datetime as dt
import json

from flask import Flask, jsonify, render_template, request

import ingest
from models import get_db, init_db

app = Flask(__name__)
init_db()

PORT = 5007


@app.template_filter("fromjson")
def fromjson_filter(s):
    try:
        return json.loads(s or "[]")
    except json.JSONDecodeError:
        return []


def _doc_dict(row):
    d = dict(row)
    d["agencies"] = json.loads(d["agencies"] or "[]")
    d["matched_terms"] = json.loads(d["matched_terms"] or "[]")
    return d


# ---------------------------------------------------------------------------
# Cowork API
# ---------------------------------------------------------------------------

@app.route("/api/pending")
def api_pending():
    kind = request.args.get("type", "digest")
    if kind not in ("urgent", "digest"):
        return jsonify({"error": "type must be 'urgent' or 'digest'"}), 400
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM documents
           WHERE is_watchlist_hit = 1 AND notified_at IS NULL AND is_urgent = ?
           ORDER BY COALESCE(publication_date, scheduled_pub_date) DESC,
                    document_number DESC""",
        (1 if kind == "urgent" else 0,)).fetchall()
    conn.close()
    return jsonify({"type": kind, "count": len(rows),
                    "documents": [_doc_dict(r) for r in rows]})


@app.route("/api/mark_notified", methods=["POST"])
def api_mark_notified():
    payload = request.get_json(silent=True) or {}
    numbers = payload.get("document_numbers", [])
    if not isinstance(numbers, list) or not all(isinstance(n, str) for n in numbers):
        return jsonify({"error": "document_numbers must be a list of strings"}), 400
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    updated = 0
    for n in numbers:
        cur = conn.execute(
            "UPDATE documents SET notified_at = ? WHERE document_number = ?"
            " AND notified_at IS NULL", (now, n))
        updated += cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({"updated": updated, "notified_at": now})


@app.route("/api/health")
def api_health():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    return jsonify({"status": "ok", "documents": total})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def feed():
    agency = request.args.get("agency", "").strip()
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    status = request.args.get("status", "watchlist")  # all | watchlist | urgent

    sql = "SELECT * FROM documents WHERE 1=1"
    params = []
    if status == "watchlist":
        sql += " AND is_watchlist_hit = 1"
    elif status == "urgent":
        sql += " AND is_urgent = 1"
    if agency:
        sql += " AND agencies LIKE ?"
        params.append(f"%{agency}%")
    if date_from:
        sql += " AND COALESCE(publication_date, scheduled_pub_date) >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND COALESCE(publication_date, scheduled_pub_date) <= ?"
        params.append(date_to)
    sql += (" ORDER BY COALESCE(publication_date, scheduled_pub_date) DESC,"
            " first_seen_at DESC LIMIT 300")

    conn = get_db()
    docs = [_doc_dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return render_template("feed.html", docs=docs, agency=agency,
                           date_from=date_from, date_to=date_to, status=status)


@app.route("/run-ingest", methods=["POST"])
def run_ingest_view():
    for source in ("public-inspection", "published"):
        try:
            ingest.run(source, force=True)
        except Exception as exc:
            return jsonify({"error": f"{source}: {exc}"}), 500
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=PORT)
