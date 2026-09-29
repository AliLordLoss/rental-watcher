# Rental Watcher — Pararius new-listing alerts

A small personal-use script that checks a Pararius search URL (with your filters
already applied) and emails you whenever a new listing appears. It keeps a
persistent record of seen listings so you only get notified about what's new.

Runs **once per invocation** — schedule it with cron (or a systemd timer).

## Requirements

- Python 3.10+
- Works on Linux, macOS, and Windows (cron examples below are Linux/macOS)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

The `playwright install chromium` step is required for the bot-detection
fallback. Playwright downloads its own browser, so no system Chrome is needed.

### 2. Configure your search URL

Open `rental_watcher.py` and set `SEARCH_URL` at the top to your Pararius search
page — apply all filters (city, price range, bedrooms, etc.) in your browser
first, then copy the resulting URL. The script uses exactly those filters.

### 3. Configure email via `.env`

Create a `.env` file in the same directory as the script:

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USERNAME="a@gmail.com"
export SMTP_PASSWORD="pass"
export EMAIL_FROM="a@gmail.com"
export EMAIL_TO="a@gmail.com"
```

Notes:

- **Gmail**: you cannot use your normal account password — create an
  [App Password](https://myaccount.google.com/apppasswords) and use that as
  `SMTP_PASSWORD`.
- Any SMTP server works (your ISP, Outlook, etc.) — adjust `SMTP_HOST`/`SMTP_PORT`.
- Since the `.env` file contains a password, keep it out of version control
  (`chmod 600 .env`).

### 4. Test it

```bash
python3 rental_watcher.py
```

Expected output on the very first run:

```
INFO parsed 30 listings (30 new, 0 already seen)
INFO first run: recorded 30 listings as baseline, no email sent
```

The first run deliberately sends **no** email — it records everything already on
the page as the baseline. Run it a second time:

```bash
python3 rental_watcher.py
```

```
INFO parsed 30 listings (0 new, 30 already seen)
INFO no new listings since last check, no email sent
```

Want to verify the email pipeline (credentials, formatting) before going live?
Temporarily add `first_run = False` as the first line of `main()`, run once to
receive the full baseline by email, then remove the line. `seen.json` already
contains the baseline, so nothing gets re-sent.

## Scheduling with cron

Edit your crontab (`crontab -e`) and add, e.g. every 10 minutes:

```cron
*/10 * * * *  cd /absolute/path/to/house-search && /usr/bin/python3 rental_watcher.py >> watcher.log 2>&1
```

Notes:

- Use absolute paths in cron; `cd` first so logs and state stay in one place.
- The script exits `1` on fatal errors, so failures show up in `watcher.log`.
- Keep the interval reasonable (10–30 min). Faster gains you little and is
  more likely to get your IP rate-limited.

## How it works

- **Fetching**: tries plain `requests` with browser-like headers (this works
  against Pararius' bot protection); if it gets a 403 it automatically falls
  back to headless Chromium via Playwright.
- **State**: `seen.json` (next to the script) records every listing ID with a
  first-seen timestamp. Entries older than 90 days are pruned automatically.
- **Email**: on new listings, sends an HTML + plain-text email with title
  (linked to Pararius), price, area, rooms, and address.
- **Idempotent**: safe to run overlapping; state is written atomically.

## Files

| File | Purpose |
|---|---|
| `rental_watcher.py` | the script |
| `.env` | your SMTP credentials (not committed) |
| `seen.json` | persistent seen-listing database (auto-created) |
| `watcher.log` | cron output (if you redirect it) |

## Troubleshooting

- **`KeyError: 'SMTP_USERNAME'`** — your `.env` is missing or misnamed. It must
  sit in the same directory as the script.
- **`no listing elements found` warning, 0 listings parsed** — Pararius changed
  their page markup. Re-dump the page (`debug_page.html`) and update the
  selectors in `parse_listings()`.
- **Everything parses but every run says "30 new"** — listing IDs aren't stable;
  check that `seen.json` is being written in the same directory as the script
  you're running (don't run copies from different locations).
- **403 errors in the log followed by Playwright fallback every run** — your IP
  is flagged; the fallback still works, but consider lengthening the interval.
