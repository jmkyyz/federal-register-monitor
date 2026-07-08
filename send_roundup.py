#!/usr/bin/env python3
"""
Federal Register 5 p.m. round-up email.

Collects watchlist hits that haven't been notified yet (notified_at IS NULL),
emails them as a single daily round-up (urgent items on top), then stamps
notified_at so the same items are never sent twice. This shares notified_at
with the Cowork /api/pending endpoints, so whichever channel reports an item
first claims it.

Credentials: read from ~/statcan-explorer/.env (SMTP_HOST, SMTP_PORT,
SMTP_USER, SMTP_PASS, NOTIFY_TO) — same file the Gazette digest uses — with
the Desktop RTF app-password file as a manual-run fallback.

Cron (home machine only — the work-computer copy is dashboard-only):
    0 17 * * 1-5  cd ~/statcan-explorer/federal-register-monitor && /usr/local/bin/python3 send_roundup.py >> ingest.log 2>&1

Usage:
    python3 send_roundup.py             # send + mark notified
    python3 send_roundup.py --dry-run   # print what would be sent, mark nothing
"""
import html
import json
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from models import get_db, init_db

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE.parent / ".env"
APP_PASSWORD_RTF = Path("/Users/jasonkirby/Desktop/StatCanApp/gmail_app_password.txt.rtf")

FROM_DEFAULT = "jmk.yyz.data@gmail.com"
TO_DEFAULT = "jasonkirby@gmail.com"

TYPE_LABELS = {
    "RULE": "Final rule",
    "PRORULE": "Proposed rule",
    "NOTICE": "Notice",
    "PRESDOCU": "Presidential document",
}


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    # Treat the unfilled template as absent.
    if env.get("SMTP_USER", "").startswith("you@"):
        env.pop("SMTP_USER", None)
        env.pop("SMTP_PASS", None)
    if env.get("NOTIFY_TO", "").startswith("you@"):
        env.pop("NOTIFY_TO", None)
    # Fallback: the Desktop RTF only works when run manually (launchd/cron
    # can't read the Desktop — macOS TCC).
    if not env.get("SMTP_PASS"):
        env["SMTP_USER"] = env.get("SMTP_USER") or FROM_DEFAULT
        env["SMTP_PASS"] = get_app_password()
    return env


def get_app_password():
    content = APP_PASSWORD_RTF.read_text()
    for line in reversed(content.split("\n")):
        clean = re.sub(r"\\[a-z]+\d*\s?", "", line)
        clean = re.sub(r"[{}]", "", clean).strip()
        candidate = clean.replace(" ", "")
        if len(candidate) == 16 and candidate.isalpha():
            return candidate
    raise ValueError(f"Could not extract a 16-char password from {APP_PASSWORD_RTF}")


def fetch_pending():
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM documents
           WHERE is_watchlist_hit = 1 AND notified_at IS NULL
           ORDER BY is_urgent DESC, document_type = 'PRESDOCU' DESC,
                    COALESCE(publication_date, scheduled_pub_date) DESC"""
    ).fetchall()
    conn.close()
    return rows


def item_html(row):
    title = html.escape(row["title"] or row["document_number"])
    url = row["html_url"] or row["pdf_url"] or "#"
    agencies = ", ".join(json.loads(row["agencies"] or "[]"))
    terms = ", ".join(json.loads(row["matched_terms"] or "[]"))
    dtype = TYPE_LABELS.get(row["document_type"], row["document_type"] or "—")
    date = row["publication_date"] or f"scheduled {row['scheduled_pub_date'] or '?'}"
    badges = []
    if row["is_urgent"]:
        badges.append("<span style='color:#b00020;font-weight:bold'>URGENT</span>")
    if row["filing_type"] == "special":
        badges.append("<span style='color:#b00020'>special filing</span>")
    if row["significant"]:
        badges.append("significant")
    if row["source_stage"] == "public_inspection":
        badges.append("public inspection")
    badge_str = (" · " + " · ".join(badges)) if badges else ""
    return (
        f"<li style='margin-bottom:10px'>"
        f"<a href='{html.escape(url)}'>{title}</a><br>"
        f"<small>{html.escape(dtype)} · {html.escape(agencies)} · {html.escape(date)}"
        f"{badge_str}<br>Matched: {html.escape(terms)}</small></li>"
    )


def compose(rows):
    today = datetime.now().strftime("%A, %B %-d, %Y")
    urgent = [r for r in rows if r["is_urgent"]]
    regular = [r for r in rows if not r["is_urgent"]]
    n_urg = f", {len(urgent)} urgent" if urgent else ""
    subject = f"Federal Register round-up — {len(rows)} watchlist hit{'s' if len(rows) != 1 else ''}{n_urg} ({today})"

    parts = [f"<h2 style='margin-bottom:4px'>Federal Register round-up</h2>"
             f"<p style='margin-top:0;color:#555'>{today}</p>"]
    if urgent:
        parts.append("<h3 style='color:#b00020'>🚨 Urgent</h3><ul>"
                     + "".join(item_html(r) for r in urgent) + "</ul>")
    if regular:
        parts.append(("<h3>Watchlist hits</h3>" if urgent else "") + "<ul>"
                     + "".join(item_html(r) for r in regular) + "</ul>")
    parts.append("<p><small>Sent by federal-register-monitor · "
                 "<a href='http://localhost:5007'>dashboard</a></small></p>")
    text = "\n".join(
        f"{'[URGENT] ' if r['is_urgent'] else ''}{r['title']} — {r['html_url'] or r['pdf_url']}"
        for r in rows)
    return subject, "".join(parts), text


def send(env, subject, html_body, text):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = env["SMTP_USER"]
    msg["To"] = env.get("NOTIFY_TO") or TO_DEFAULT
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(env.get("SMTP_HOST", "smtp.gmail.com"),
                      int(env.get("SMTP_PORT", 587)), timeout=30) as s:
        s.starttls()
        s.login(env["SMTP_USER"], env["SMTP_PASS"])
        s.send_message(msg)
    return msg["To"]


def mark_notified(rows):
    stamp = datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    conn.executemany(
        "UPDATE documents SET notified_at = ? WHERE document_number = ?",
        [(stamp, r["document_number"]) for r in rows])
    conn.commit()
    conn.close()


def main():
    dry_run = "--dry-run" in sys.argv
    stamp = datetime.now().isoformat(timespec="seconds")
    init_db()
    rows = fetch_pending()
    if not rows:
        print(f"[{stamp}] round-up: nothing pending, no email sent")
        return
    subject, html_body, text = compose(rows)
    if dry_run:
        print(f"[{stamp}] DRY RUN — would send: {subject}\n")
        print(text)
        return
    env = load_env(ENV_FILE)
    to = send(env, subject, html_body, text)
    mark_notified(rows)
    print(f"[{stamp}] round-up: sent {len(rows)} item(s) to {to}")


if __name__ == "__main__":
    main()
