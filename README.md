# Rental Watchers — Pararius & Funda new-listing alerts

Small personal-use scripts that check Pararius and Funda search URLs (with your
filters already applied) and email you whenever a new listing appears. Each
watcher keeps a persistent record of seen listings so you only get notified
about what's new.

Both watchers run **once per invocation** — schedule them with cron via
`run.sh`.

## Repo layout

```
.
├── run.sh                      # runs every watcher once (use this in cron)
├── pararius/
│   ├── pararius_watcher.py
│   ├── seen.json               # auto-created
│   └── .env                    # your SMTP credentials (gitignored)
├── funda/
│   ├── funda_watcher.py
│   └── seen.json               # auto-created
├── requirements.txt
└── README.md
```

## Requirements

- Python 3.10+
- Linux / macOS / Windows (cron examples are Linux/macOS)

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

The `playwright install chromium` step downloads Playwright's own browser —
no system Chrome needed.

On a Linux server, install Chromium's system libraries as well (run as root or
with `sudo`, also make sure the venv is active):

```bash
playwright install-deps chromium
```

### 2. Configure the search URLs

Each watcher has a `SEARCH_URL` at the top of its file. Apply all filters
(city, price, bedrooms, object type...) in your browser first, then copy the
resulting URL into the script.

- `pararius/pararius_watcher.py` — your Pararius search
- `funda/funda_watcher.py` — your Funda search

### 3. Configure email via `.env`

Create **one** `.env` at the repo root (the Funda watcher also checks its own
folder, but one root-level file is simplest):

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
- Any SMTP server works (ISP, Outlook, ...). Adjust `SMTP_HOST`/`SMTP_PORT`.
- The file contains a password: `chmod 600 .env`, and keep it gitignored.

### 4. Test each watcher

```bash
source .venv/bin/activate
python pararius/pararius_watcher.py
python funda/funda_watcher.py
```

When `seen.json` is empty, both watchers email all matching listings. On later
runs, they email only listings not already seen. The expected first-run output
includes:

```
INFO parsed N listings (N new, 0 already seen)
INFO emailing N new listing(s) to ...
INFO email sent to ...
```

The next run should say `(0 new, N already seen)` and `no new listings since
last check, no email sent`.

## Scheduling with cron

Edit your crontab (`crontab -e`) and add (every hour, on the hour):

```cron
0 * * * *  cd /absolute/path/to/repo && ./run.sh >> watcher.log 2>&1
```

Notes:

- Use absolute paths in cron.
- `run.sh` uses `.venv` when present, runs every watcher even if one fails,
  and exits non-zero if any of them did — failures land in `watcher.log`.
- Keep intervals reasonable (hourly is fine; 10–30 min for Pararius if you
  want faster alerts). Aggressive polling from one IP is the main way to get
  permanently blocked.

## How it works

|       | Pararius watcher                                            | Funda watcher                                                                     |
| ----- | ----------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Fetch | `requests` with browser headers; Playwright fallback on 403 | Playwright primary (Funda's bot protection blocks plain `requests` almost always) |
| Parse | CSS selectors (`section.listing-search-item`)               | Defensive regex-based card parsing                                                |
| State | `seen.json` per folder, 90-day auto-prune                   | same                                                                              |
| Email | HTML + plain text: listing, price, area, address            | HTML + plain text: title, price, area, address                                    |

Both scripts are idempotent, write state atomically, and are safe to run
from overlapping cron jobs.

## Known limitations

- **Funda pagination**: only the first page of results is checked (~15
  listings). For a narrow filter like this one that's fine — new listings
  appear on page 1 — but if you broaden the search, listings could shift to
  page 2+ before you see them.
- **Funda markup changes often**: the Funda parser is intentionally
  heuristic. If `parsed 0 listings` shows up in the log, their HTML changed.
- **"Under option" listings**: Funda keeps listings with status "Onder optie"
  in the results; the watchers don't filter them out yet.

## Troubleshooting

- **`KeyError: 'SMTP_USERNAME'`** — `.env` missing or misnamed. Put it at the
  repo root (or next to the script).
- **`parsed 0 listings` / `no listing elements found`** — the site's markup
  changed. Dump the page and update the selectors:
  ```bash
  python3 -c "
  import importlib.util
  spec = importlib.util.spec_from_file_location('w', 'pararius/rental_watcher.py')
  w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
  open('debug_page.html', 'w').write(w.fetch(w.SEARCH_URL))
  "
  ```
- **Every run reports everything as new** — listing IDs are unstable, or
  you're running a different copy of the script (`seen.json` lives next to
  the script — don't run copies from other folders).
- **Funda always falls back / times out** — your IP may be flagged. The
  stealth measures handle most cases; if Funda starts showing a verification
  page, seed a persistent browser profile with a headed run:
  ```bash
  source .venv/bin/activate
  FUNDA_WAIT_FOR_VERIFICATION=1 python funda/funda_watcher.py
  ```
  Headed mode is enabled by default. The optional wait flag lets you complete
  verification in the Chromium window and press Enter; regular runs do not
  pause for input. The profile is saved at `~/.funda-watcher-chromium` and
  reused on later runs. You can set `FUNDA_PROFILE_DIR` to use a different
  location. Keep the profile private because it contains browser session
  data. Set `FUNDA_HEADFUL=0` to try headless mode. Headed runs on a server need
  an available graphical display (for example, Xvfb on a headless Linux host).

## Files

| File                           | Purpose                                           |
| ------------------------------ | ------------------------------------------------- |
| `run.sh`                       | runs all watchers once; use in cron               |
| `pararius/pararius_watcher.py` | Pararius watcher                                  |
| `funda/funda_watcher.py`       | Funda watcher                                     |
| `.env`                         | SMTP credentials (gitignored, root or per-folder) |
| `*/seen.json`                  | per-site seen-listing database (auto-created)     |
| `watcher.log`                  | cron output                                       |
