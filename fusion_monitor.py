#!/usr/bin/env python3
"""
fusion_monitor.py — Read-only ticket-offer watcher for forum.fusion-festival.de

Usage:
    python fusion_monitor.py            # normal background loop
    python fusion_monitor.py --dry-run  # fetch once, print matches, no alerts
"""

# ── CONFIG ───────────────────────────────────────────────────────────────────

FORUM_URL = "https://forum.fusion-festival.de/viewforum.php?f=82"

# Titles containing any of these words (case-insensitive) are candidate offers.
OFFER_KEYWORDS = [
    "biete",
    "abzugeben",
    "gebe ab",
    "verkaufe",
    "übertrage",
    "übertragen",
    "tausche",
    "verschenke",
    "frei geworden",
]

# If the title ONLY matches these (no offer keyword present), skip it.
SEARCH_KEYWORDS = ["suche", "gesucht"]

# ── Email ────────────────────────────────────────────────────────────────────
EMAIL_FROM = "your-gmail@gmail.com"
EMAIL_TO   = [
    "your-gmail@gmail.com",
    "friend@gmail.com",
]

# Gmail SMTP — password is read from env var FUSION_SMTP_PASSWORD (see README).
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# ── Polling ──────────────────────────────────────────────────────────────────
POLL_INTERVAL_S = 300   # base interval between polls (seconds)
JITTER_MAX_S    = 60    # add up to this many extra seconds of random jitter
MIN_INTERVAL_S  = 120   # hard floor — never poll faster than this

# ── HTTP ─────────────────────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15  # seconds

# ── State ────────────────────────────────────────────────────────────────────
STATE_FILENAME = "fusion_state.json"

# ── END CONFIG ────────────────────────────────────────────────────────────────

import json
import logging
import os
import random
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load .env from the directory this script lives in.
load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).parent / STATE_FILENAME


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> set:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text())
            return set(data.get("seen_ids", []))
        except Exception as exc:
            log.warning("Could not read state file: %s", exc)
    return set()


def save_state(seen_ids: set) -> None:
    try:
        STATE_PATH.write_text(json.dumps({"seen_ids": sorted(seen_ids)}, indent=2))
    except Exception as exc:
        log.warning("Could not write state file: %s", exc)


# ── Scraping ──────────────────────────────────────────────────────────────────

def _nearest_time(tag) -> str:
    """Walk up the DOM from a tag to find the closest <time datetime="..."> in the same row."""
    node = tag.parent
    for _ in range(8):
        if node is None or node.name in ("html", "body"):
            break
        # phpBB wraps each thread in a <li> or <dl> — stop climbing once we're there
        time_el = node.find("time", datetime=True)
        if time_el:
            return time_el.get("datetime", "")
        if node.name in ("li", "dl"):
            break
        node = node.parent
    return ""


def fetch_threads() -> list:
    """Return [{"id", "title", "url", "last_post_dt"}, ...] from the forum page."""
    try:
        resp = requests.get(
            FORUM_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Fetch failed: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    seen_ids = set()
    threads  = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "viewtopic.php" not in href:
            continue
        # Extract t= parameter.
        tid = None
        for part in href.split("?", 1)[-1].split("&"):
            if part.startswith("t="):
                tid = part[2:]
                break
        if not tid or tid in seen_ids:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        clean_href = href.lstrip("./")
        url = f"https://forum.fusion-festival.de/{clean_href}"
        seen_ids.add(tid)
        threads.append({
            "id": tid,
            "title": title,
            "url": url,
            "last_post_dt": _nearest_time(a),
        })

    return threads


# ── Filtering ─────────────────────────────────────────────────────────────────

def is_offer(title: str) -> bool:
    """True for a clean ticket offer — no mixed-intent titles."""
    lower = title.lower()
    has_offer  = any(kw in lower for kw in OFFER_KEYWORDS)
    has_search = any(kw in lower for kw in SEARCH_KEYWORDS)
    # Reject mixed-intent posts: "suche Ticket, biete Mitfahrgelegenheit" style.
    # A title with both offer AND search keywords means the person wants a ticket
    # and is offering something else in return — not a real offer for us.
    if has_offer and has_search:
        return False
    return has_offer


def is_today(dt_str: str) -> bool:
    """True if the ISO datetime string falls on the current local date.
    Returns True when the string is empty (give benefit of the doubt)."""
    if not dt_str:
        return True
    try:
        dt  = datetime.fromisoformat(dt_str)
        tz  = dt.tzinfo or timezone.utc
        return dt.date() == datetime.now(tz).date()
    except Exception:
        return True  # unparseable → don't filter


def should_alert(thread: dict) -> bool:
    return is_offer(thread["title"]) and is_today(thread.get("last_post_dt", ""))


# ── Alerting ──────────────────────────────────────────────────────────────────

def notify_macos(title: str, message: str) -> None:
    script = (
        f"display notification {json.dumps(message)} "
        f"with title {json.dumps(title)}"
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=5)
    except Exception as exc:
        log.warning("macOS notification failed: %s", exc)


def send_email(subject: str, body: str) -> None:
    password = os.environ.get("FUSION_SMTP_PASSWORD")
    if not password:
        log.warning("FUSION_SMTP_PASSWORD not set — email alert skipped")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(EMAIL_TO)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_FROM, password)
            smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info("Email sent → %s", ", ".join(EMAIL_TO))
    except Exception as exc:
        log.warning("Email failed: %s", exc)


def alert(thread: dict) -> None:
    subject = f"[Fusion Ticket] {thread['title']}"
    body    = f"New offer thread spotted:\n\n{thread['title']}\n{thread['url']}\n"
    log.info("ALERT  id=%s  %s", thread["id"], thread["title"])
    notify_macos("Fusion Ticket Offer", f"{thread['title']}\n{thread['url']}")
    send_email(subject, body)


# ── Poll loop ─────────────────────────────────────────────────────────────────

def poll(seen_ids: set, *, first_run: bool = False) -> set:
    threads = fetch_threads()
    if not threads:
        log.warning("No threads parsed — the page structure may have changed.")
        return seen_ids

    new_count = 0
    for t in threads:
        if t["id"] in seen_ids:
            continue
        new_count += 1
        if first_run:
            pass  # silent baseline
        elif should_alert(t):
            alert(t)
        else:
            log.debug("New thread filtered: %s", t["title"])

    if first_run and new_count:
        log.info("First run: %d existing threads recorded as baseline (no alerts sent).", new_count)
    elif not first_run and new_count:
        log.info("Polled — %d new thread(s) found.", new_count)
    else:
        log.info("Polled — nothing new.")

    return seen_ids | {t["id"] for t in threads}


# ── Dry run ───────────────────────────────────────────────────────────────────

def dry_run() -> None:
    print()
    print("── DRY RUN ──────────────────────────────────────────────────────────")
    print(f"Fetching: {FORUM_URL}")
    print()

    threads = fetch_threads()
    if not threads:
        print("ERROR: No threads returned. Check the URL or your internet connection.")
        return

    matches = [t for t in threads if should_alert(t)]

    print(f"Threads found on page : {len(threads)}")
    print(f"Would alert on        : {len(matches)}")
    print()

    if matches:
        print("WOULD ALERT:")
        print()
        for t in matches:
            print(f"  [{t['id']}]  {t['title']}")
            print(f"            {t['url']}")
    else:
        print("No alertable threads on the current page.")
        print("(Quiet right now, or filters are too tight — see breakdown below.)")

    print()
    print("Filter breakdown (all threads):")
    for t in threads:
        lower = t["title"].lower()
        has_offer  = any(kw in lower for kw in OFFER_KEYWORDS)
        has_search = any(kw in lower for kw in SEARCH_KEYWORDS)
        if has_offer and has_search:
            tag = "MIXED  "
        elif has_offer and not is_today(t.get("last_post_dt", "")):
            tag = "OLD    "
        elif has_offer:
            tag = "ALERT  "
        else:
            tag = "       "
        date_str = t.get("last_post_dt", "")[:10] or "no-date"
        print(f"  {tag} [{date_str}]  {t['title']}")

    print()
    print("── END DRY RUN ──────────────────────────────────────────────────────")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if "--dry-run" in sys.argv:
        dry_run()
        return

    log.info("Fusion ticket monitor starting (Ctrl-C to stop).")
    seen_ids  = load_state()
    first_run = len(seen_ids) == 0

    seen_ids = poll(seen_ids, first_run=first_run)
    save_state(seen_ids)

    while True:
        interval = max(MIN_INTERVAL_S, POLL_INTERVAL_S + random.uniform(0, JITTER_MAX_S))
        log.info("Sleeping %.0f s until next poll.", interval)
        time.sleep(interval)
        seen_ids = poll(seen_ids)
        save_state(seen_ids)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
