#!/usr/bin/env python3
"""
Pararius new-listing alert script (personal use)

- Fetches a Pararius search URL with your filters
- Keeps a persistent record of already-seen listings (seen.json)
- Emails you only the NEW listings, with price / area / address / link
- Runs ONCE and exits - schedule it with cron/systemd timer, e.g.:
    */10 * * * *  cd ~/house-search && /usr/bin/python3 rental_watcher.py >> watcher.log 2>&1

Requirements:
    pip install requests beautifulsoup4 playwright
    playwright install chromium
"""

import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config ---
SEARCH_URL = (
    "https://www.pararius.com/apartments/rotterdam/apartment/0-2250/1-bedrooms"
)
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
CHECK_INTERVAL = 600  # seconds between checks when running as a loop

# Load email settings from a .env file next to this script.
# The file uses shell-export format, so you can also `source .env`:
#   export SMTP_HOST="smtp.gmail.com"
#   export SMTP_PORT="587"
#   ...
def load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export").strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USERNAME"]   # required - script exits with KeyError if missing
SMTP_PASS = os.environ["SMTP_PASSWORD"]   # required
MAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
MAIL_TO = os.environ.get("EMAIL_TO", MAIL_FROM)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pararius")


# ------------------------------------------------------------ fetching ----
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.pararius.com/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Cache-Control": "max-age=0",
}


def fetch_with_requests(url: str) -> str:
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"requests got HTTP {resp.status_code}")
    return resp.text


def fetch_with_playwright(url: str) -> str:
    """Fallback: real Chromium, which bypasses Pararius' bot detection."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=BROWSER_HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # wait for the listing grid to render (JS-heavy site)
        page.wait_for_selector(
            "section.listing-search-item", timeout=30_000
        )
        html = page.content()
        browser.close()
        return html


def fetch(url: str) -> str:
    try:
        return fetch_with_requests(url)
    except Exception as e:
        log.info("requests failed (%s) -> falling back to Playwright", e)
        return fetch_with_playwright(url)


# ------------------------------------------------------------- parsing ----
def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def grab_text(el, *selectors) -> str:
    """First non-empty text among selectors; '' if none match."""
    for sel in selectors:
        node = el.select_one(sel)
        if node:
            return clean(node.get_text())
    return ""


def parse_listings(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("section.listing-search-item")
    if not items:
        log.warning("no listing elements found - page layout may have changed")

    listings = []
    for item in items:
        # title + link
        a = item.select_one("a.listing-search-item__link--title") or item.select_one("a[href]")
        href = a.get("href") if a else None
        if not href:
            continue
        url_full = urljoin(base_url, href)
        # listing id from the URL path, e.g. .../apartment-for-rent/rotterdam/3b189d4b/...
        listing_id = url_full.rstrip("/").split("/")[-2] if url_full.rstrip("/").split("/")[-1].isalpha() else url_full.rstrip("/").split("/")[-1]

        title = grab_text(item, ".listing-search-item__title")
        price = grab_text(item, ".listing-search-item__price")
        address = grab_text(item, ".listing-search-item__sub-title")
        area = grab_text(item, ".illustrated-features__item--surface-area")
        rooms = grab_text(item, ".illustrated-features__item--number-of-rooms")

        listings.append({
            "id": listing_id,
            "title": title,
            "url": url_full,
            "price": price,
            "area": area,
            "rooms": rooms,
            "address": address,
        })
    return listings


# ------------------------------------------------------------ seen db -----
def load_seen() -> dict:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen: dict) -> None:
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=1, ensure_ascii=False)
    os.replace(tmp, SEEN_FILE)


def _parse_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


# -------------------------------------------------------------- email -----
def build_email(new: list[dict]) -> MIMEMultipart:
    subject = f"Pararius: {len(new)} new listing{'s' if len(new) != 1 else ''} in Rotterdam"
    msg = MIMEMultipart("alternative")
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Subject"] = subject

    # plain text part
    lines = [f"{len(new)} new listing(s):\n"]
    for l in new:
        lines.append(f"- {l['title']}\n  {l['price']} | {l['area']} | {l['address']}\n  {l['url']}\n")
    msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))

    # html part
    rows = "".join(
        "<tr>"
        f"<td><a href='{escape(l['url'])}'>{escape(l['title'] or l['url'])}</a></td>"
        f"<td>{escape(l['price'])}</td>"
        f"<td>{escape(l['area'])}</td>"
        f"<td>{escape(l['address'])}</td>"
        "</tr>"
        for l in new
    )
    html_body = f"""<html><body>
<p><b>{len(new)}</b> new Pararius listing(s) matching your filters:</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr><th>Listing</th><th>Price</th><th>Area</th><th>Address</th></tr>
{rows}
</table>
</body></html>"""
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def send_email(msg: MIMEMultipart) -> None:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    log.info("email sent to %s", MAIL_TO)


# ---------------------------------------------------------------- main ----
def main() -> None:
    # NOTE: must be computed BEFORE save_seen() writes the file
    first_run = not os.path.exists(SEEN_FILE)
    first_run = False

    html = fetch(SEARCH_URL)
    listings = parse_listings(html, SEARCH_URL)

    seen = load_seen()
    new = [l for l in listings if l["id"] not in seen]
    already_seen = len(listings) - len(new)

    log.info("parsed %d listings (%d new, %d already seen)",
             len(listings), len(new), already_seen)

    now = datetime.now(timezone.utc).isoformat()
    for l in listings:
        seen.setdefault(l["id"], {"first_seen": now, "url": l["url"]})

    # prune entries older than 90 days so the file stays small
    # (listings that disappear and reappear after 90 days count as "new" again)
    cutoff = time.time() - 90 * 86400
    current_ids = {l["id"] for l in listings}
    before = len(seen)
    seen = {k: v for k, v in seen.items()
            if _parse_ts(v.get("first_seen", "")) > cutoff or k in current_ids}
    if len(seen) < before:
        log.info("pruned %d stale entries from seen db", before - len(seen))

    save_seen(seen)

    if first_run:
        log.info("first run: recorded %d listings as baseline, no email sent "
                 "(future runs email only listings not in this baseline)", len(listings))
    elif not new:
        log.info("no new listings since last check, no email sent")
    else:
        log.info("emailing %d new listing(s) to %s", len(new), MAIL_TO)
        send_email(build_email(new))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("fatal error")
        sys.exit(1)
