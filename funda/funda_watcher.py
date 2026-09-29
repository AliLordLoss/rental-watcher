#!/usr/bin/env python3
"""
Funda new-listing alert script (personal use)

- Fetches a Funda search URL with your filters
- Keeps a persistent record of already-seen listings (seen.json)
- Emails you only the NEW listings, with price / area / address / link
- Runs ONCE and exits - schedule it with cron, or via run_all.sh in the repo root

Requirements:
    pip install requests beautifulsoup4 playwright playwright-stealth
    playwright install chromium

Note: Funda's bot protection is strict; plain requests usually gets a 403,
so this script relies on Playwright (headless Chromium + stealth). Parsing is
regex-based over the result cards because Funda's markup changes often - if it
breaks, dump the page (see README) and adjust parse_listings().
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
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config ---
SEARCH_URL = (
    "https://www.funda.nl/en/zoeken/huur?selected_area=rotterdam"
    "&price=0-2250&object_type=apartment"
)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(SCRIPT_DIR, "seen.json")

# Load email settings from .env - checks this folder, then the repo root,
# so one .env at the repo root works for both watchers.
def load_dotenv() -> None:
    for path in (os.path.join(SCRIPT_DIR, ".env"),
                 os.path.join(SCRIPT_DIR, "..", ".env")):
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().removeprefix("export").strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
        logging.getLogger("funda").info("loaded config from %s", path)
        return

load_dotenv()

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USERNAME"]   # required
SMTP_PASS = os.environ["SMTP_PASSWORD"]   # required
MAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
MAIL_TO = os.environ.get("EMAIL_TO", MAIL_FROM)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("funda")


# ------------------------------------------------------------ fetching ----
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    "Referer": "https://www.funda.nl/",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_with_requests(url: str) -> str:
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"requests got HTTP {resp.status_code}")
    return resp.text


def fetch_with_playwright(url: str) -> str:
    """Fetch with a persistent Chromium profile and optional headed mode."""
    from playwright.sync_api import sync_playwright

    headful = os.environ.get("FUNDA_HEADFUL", "1").lower() in {"1", "true", "yes"}
    profile_dir = os.path.expanduser(os.environ.get(
        "FUNDA_PROFILE_DIR", "~/.funda-watcher-chromium"
    ))

    with sync_playwright() as p:
        # A persistent context keeps cookies between runs. Keep the profile
        # outside the repo because it contains browser session data.
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=not headful,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=BROWSER_HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9,nl;q=0.8"},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = ctx.new_page()
        try:
            try:
                # playwright-stealth 2.x exposes Stealth; 1.x exposed
                # stealth_sync. Support both APIs.
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(ctx)
            except ImportError:
                from playwright_stealth import stealth_sync
                stealth_sync(page)
        except ImportError:
            log.warning("playwright-stealth unavailable - continuing without it")
        if headful:
            log.info("headed browser opened with persistent profile at %s", profile_dir)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # Funda renders results client-side; give it time and scroll to
        # trigger any lazy-loaded cards
        page.wait_for_timeout(4000)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(2000)
        html = page.content()
        if headful and os.environ.get("FUNDA_WAIT_FOR_VERIFICATION", "").lower() in {
            "1", "true", "yes"
        }:
            input("If Funda is showing verification, complete it, then press Enter here...")
            page.wait_for_timeout(1500)
            html = page.content()
        ctx.close()
        return html


def fetch(url: str) -> str:
    headful = os.environ.get("FUNDA_HEADFUL", "1").lower() in {"1", "true", "yes"}
    profile_dir = os.path.expanduser(os.environ.get(
        "FUNDA_PROFILE_DIR", "~/.funda-watcher-chromium"
    ))
    # Explicit headed runs must open Chromium even when requests can retrieve
    # an HTTP 200 verification page. Once the profile exists, use it on later
    # runs so Playwright can send its saved cookies.
    if headful or os.path.isdir(profile_dir):
        return fetch_with_playwright(url)
    try:
        return fetch_with_requests(url)
    except Exception as e:
        log.info("requests failed (%s) -> using Playwright", e)
        return fetch_with_playwright(url)


# ------------------------------------------------------------- parsing ----
def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


LISTING_PATH_RE = re.compile(r"^/(?:en/)?(?:detail/)?huur/")  # listing links only
PRICE_RE = re.compile(r"€\s*[\d.,]+")
AREA_RE = re.compile(r"([\d.,]+)\s*m²")
BADGE_TEXT_RE = re.compile(
    r"\b(?:new|nieuw|view this house|bekijk deze woning|upp?märkt)\b",
    re.IGNORECASE,
)


def address_from_url(url: str) -> str:
    """Use Funda's detail URL slug as an address fallback."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    if parts and parts[-1].isdigit():
        parts.pop()
    if not parts:
        return ""
    slug = unquote(parts[-1])
    slug = re.sub(
        r"^(?:(?:appartement|woning|huis|studio|kamer|penthouse|bovenwoning|"
        r"benedenwoning|eengezinswoning|vrijstaande-woning)-)+",
        "",
        slug,
        flags=re.IGNORECASE,
    )
    return slug.replace("-", " ").strip().title()


def without_badges(text: str) -> str:
    return clean(BADGE_TEXT_RE.sub(" ", text))


def parse_listings(html: str, base_url: str) -> list[dict]:
    """
    Funda card markup changes frequently, so this is deliberately defensive:
    1. find all anchors that look like listing links
    2. walk up ancestors until we find the card (identified by containing € and m²)
    3. pull price / area / address out of the card text and listing URL
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    seen_hrefs = set()

    for a in soup.select("a[href]"):
        href = a["href"].split("?")[0].split("#")[0]
        path = urlparse(href).path
        if not LISTING_PATH_RE.match(path) or href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        # locate the card container
        card, node = None, a
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            txt = node.get_text()
            if "€" in txt and "m²" in txt:
                card = node
                break
        text = clean(card.get_text()) if card else clean(a.get_text())

        price = PRICE_RE.search(text)
        area = AREA_RE.search(text)
        url_full = urljoin(base_url, href)

        # Prefer explicit address markup, then clean the text before the price
        # of status badges, and finally use the address in Funda's URL slug.
        address = ""
        if card is not None:
            addr_el = card.select_one(
                '[data-test-id*="address" i], '
                '[data-testid*="address" i], '
                '[class*="address" i]'
            )
            if addr_el:
                address = without_badges(addr_el.get_text())
        if not address:
            prefix = text[:price.start()] if price else text[:80]
            address = without_badges(prefix)[:80]
        if not address:
            address = address_from_url(url_full)

        listing_id = url_full.rstrip("/").split("/")[-1] or url_full
        title = ""
        if card is not None:
            title_el = card.select_one(
                "h1, h2, h3, [data-test-id*='title' i], [data-testid*='title' i]"
            )
            if title_el:
                title = without_badges(title_el.get_text())
        if not title:
            title = without_badges(a.get("aria-label", "") or a.get("title", "") or a.get_text())
        title = title or address or listing_id

        listings.append({
            "id": listing_id,
            "title": title,
            "url": url_full,
            "price": price.group(0) if price else "",
            "area": f"{area.group(1)} m²" if area else "",
            "address": address,
        })

    if not listings:
        log.warning("no listings parsed - Funda markup may have changed")
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
    subject = f"Funda: {len(new)} new listing{'s' if len(new) != 1 else ''} in Rotterdam"
    msg = MIMEMultipart("alternative")
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Subject"] = subject

    lines = ["Listing | Price | Area | Address\n"]
    for l in new:
        lines.append(
            f"- {l['title']} | {l['price']} | {l['area']} | {l['address']}\n"
            f"  {l['url']}\n"
        )
    msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))

    rows = "".join(
        "<tr>"
        f"<td><a href='{escape(l['url'])}'>{escape(l['title'])}</a></td>"
        f"<td>{escape(l['price'])}</td>"
        f"<td>{escape(l['area'])}</td>"
        f"<td>{escape(l['address'])}</td>"
        "</tr>"
        for l in new
    )
    html_body = f"""<html><body>
<p><b>{len(new)}</b> new Funda listing(s) matching your filters:</p>
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
    seen = load_seen()

    html = fetch(SEARCH_URL)
    listings = parse_listings(html, SEARCH_URL)
    if not listings:
        raise RuntimeError(
            "Funda returned no listings; refusing to update seen.json. "
            "The page may be a verification screen or the parser may need an update."
        )

    new = [l for l in listings if l["id"] not in seen]
    already_seen = len(listings) - len(new)

    log.info("parsed %d listings (%d new, %d already seen)",
             len(listings), len(new), already_seen)

    now = datetime.now(timezone.utc).isoformat()
    for l in listings:
        seen.setdefault(l["id"], {"first_seen": now, "url": l["url"]})

    # prune entries older than 90 days
    cutoff = time.time() - 90 * 86400
    current_ids = {l["id"] for l in listings}
    before = len(seen)
    seen = {k: v for k, v in seen.items()
            if _parse_ts(v.get("first_seen", "")) > cutoff or k in current_ids}
    if len(seen) < before:
        log.info("pruned %d stale entries from seen db", before - len(seen))

    save_seen(seen)

    if not new:
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
