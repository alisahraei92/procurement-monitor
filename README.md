# Procurement Monitor

Daily check of 7 agencies for newly-posted RFPs/bids:

| Agency | Source(s) |
|---|---|
| NYCHA | NYC City Record Online (API) **+** NYCHA's own procurement page |
| NYC SCA | NYC City Record Online (API) **+** SCA's own bid list (rendered — Blazor SPA) |
| NYC DDC | NYC City Record Online (API) **+** DDC's own RFP page (rendered — JS-populated) |
| NYC EDC | NYC City Record Online (API) **+** EDC's own RFP page (rendered — JS-populated) |
| NYC H+H | NYC City Record Online (API) only — H+H states on their own site that all their RFPs are published exclusively via City Record |
| DASNY | dasny.org/opportunities/rfps-bids (scraped) |
| BPCA | bpca.ny.gov/apply/rfp-opp (scraped) |

NYCHA, SCA, DDC, EDC, and H+H are all required to publish solicitations in
the City Record, so those are pulled from one structured API. On top of
that, NYCHA, SCA, DDC, and EDC each also have their own procurement page —
and the same project doesn't always appear on both at the same time, so
both sources are kept and merged (see dedup section below).

## Why some sources need a headless browser

DDC's, EDC's, and SCA's own procurement pages populate their actual listing
*after* the page loads — via client-side JavaScript, either a delayed
render or a separate API call the page makes once it's up. A plain HTTP
fetch (`requests.get`) only ever sees the empty shell markup, before that
JS has run — which is exactly why SCA was skipped entirely at first, and
DDC/EDC were flagged low-confidence.

The fix: `render_page()` in `monitor.py` opens the page in headless
Chromium (via Playwright), waits for it to actually finish loading, then
hands the *fully rendered* HTML to the same BeautifulSoup parsing used
everywhere else. `test_render.py` proves this concretely — it spins up a
local page that loads its content via a JS `fetch()` call (the same pattern
these real sites use), shows that a plain `requests.get()` genuinely misses
it, and that `render_page()` correctly captures it:

```bash
python test_render.py
```

This does mean the workflow needs `playwright install --with-deps chromium`
as an extra setup step (already in the GitHub Actions workflow) and each
run takes a bit longer — rendering four pages instead of firing off plain
HTTP requests. Still comfortably fits in a free daily Actions run.

## Confidence levels by source

- **High**: City Record API (all 5 agencies), NYCHA own site, DASNY, BPCA —
  scrape/parse against confirmed live page structure.
- **Medium**: DDC, EDC, and SCA own sites — the *rendering* is proven to
  work (see `test_render.py`), but I couldn't verify the exact selectors
  against each site's real DOM from this sandbox (no network access to
  those domains here). The parsing is written generically (regex over
  rendered text / table rows) rather than pinned to fragile specific CSS
  selectors, specifically to survive that uncertainty — but check the
  "Fetch errors" section of your first few real reports, and eyeball the
  output against the live sites once. City Record remains the authoritative
  backstop for all three regardless, so a parsing miss here costs you a
  few days of early notice, not a missed posting.

## Cross-source duplicate handling (this is the part you asked for)

For NYCHA, SCA, DDC, and EDC, every run pulls from **both** the agency's
own site and City Record, then merges them *before* deciding what's new:

1. Items from both sources for an agency are compared by normalized title
   (punctuation/stopwords stripped, compared as token sets). Two items whose
   titles overlap enough are treated as the same underlying project and
   merged into one report entry — you'll see `seen on: NYCHA Procurement
   Opportunities + NYC City Record Online` on merged entries.
2. "New" is tracked as a set of title fingerprints per agency, not raw
   per-source IDs. So if a project posts to NYCHA's own site today and the
   matching City Record notice doesn't show up for another week, the City
   Record version is recognized as the same project and **not** re-reported
   — no matter which source sees it first.
3. This is tested directly in `test_dedup.py` against the exact scenario
   you described (project appears on one source, then the other, days
   apart) — run it yourself to see it work: `python test_dedup.py`.

## How "new" is detected (mechanics)

Every run reads `state/seen_rfps.json` (title fingerprints per agency),
compares the current pull against it, reports anything that doesn't
fingerprint-match something already seen, then updates the file. First run
will report everything currently posted as "new" (expected — that's your
baseline). After that, only genuinely new postings show up.

## Run it locally

```bash
pip install -r requirements.txt
playwright install chromium
python monitor.py
```

Writes `reports/rfp_report_<date>.md` and updates `state/seen_rfps.json`.
Run it again immediately and you should see 0 new postings — that confirms
the diffing is working.

## Run it daily for free — GitHub Actions

This sandbox I built it in doesn't have open internet access, so I couldn't
execute a live end-to-end test against the real agency sites — I tested the
report/diff logic directly and the HTML/JSON parsers against synthetic
markup matching each site's actual structure, but you should run it once
yourself before trusting the schedule.

1. Create a new (can be private) GitHub repo and push this folder to it.
2. The workflow at `.github/workflows/daily-rfp-check.yml` is already set up
   to run every day at 12:00 UTC (8am ET) and commit the updated report +
   state file back to the repo. You can also trigger it manually anytime
   from the repo's **Actions** tab → "Daily RFP Check" → **Run workflow**.
3. **First run matters**: it'll report every currently-open solicitation as
   "new," which is your baseline. Let that one run, then everything after
   is genuinely new postings only.
4. Set up the daily email and the dashboard — see the two sections below.

## Set up the daily email

Uses Gmail SMTP with an **app password** (not your real Gmail password —
Google requires a separate one for this). Takes about 2 minutes:

1. Go to https://myaccount.google.com/apppasswords (your Google account
   needs 2-Step Verification turned on first, if it isn't already).
2. Create an app password for "Mail" — Google gives you a 16-character code.
3. In your GitHub repo: **Settings → Secrets and variables → Actions → New
   repository secret**, and add three:
   - `MAIL_USERNAME` — your full Gmail address
   - `MAIL_APP_PASSWORD` — the 16-character code from step 2
   - `MAIL_TO` — the email address you want the report sent to (can be the
     same Gmail address, or anywhere else)
4. That's it — the workflow already has the email step wired up. Next
   scheduled run (or a manual **Run workflow** click) will email you.

You'll get one email a day even when there's nothing new (it'll just say
"No new postings found today"). If you'd rather only be emailed when
something new actually shows up, tell me and I'll add a step that checks
the report and skips the email when there's nothing to report.

## The dashboard (a real webpage you can open)

Every run also rebuilds `docs/index.html` — a static page listing everything
the monitor has ever found, filterable by agency, searchable by keyword. No
backend, no database — it's one HTML file with the data embedded in it,
which is what makes it free to host.

**To turn it into an actual URL you can visit:**
1. In your GitHub repo: **Settings → Pages**.
2. Under "Build and deployment," set **Source: Deploy from a branch**,
   branch **main**, folder **/docs**. Save.
3. GitHub gives you a URL like `https://<your-username>.github.io/<repo-name>/`
   — that's your dashboard, live on the internet, updated automatically
   every day the workflow runs.

You can also just double-click `docs/index.html` to preview it locally any
time — that's how I checked the design while building it.

## Adjusting scope

- **Add/remove agencies:** edit `CITY_RECORD_AGENCIES` in `monitor.py` for
  anything covered by the City Record, or add a new `fetch_x()` function
  following the DASNY/BPCA pattern for anything else.
- **Lookback window / notice types:** `LOOKBACK_DAYS` and
  `SOLICITATION_KEYWORDS` near the top of `monitor.py`.
- **Schedule:** the `cron` line in the workflow file (UTC).

## Known fragility / things to sanity-check after your first real run

- **City Record field names**: NYC's Socrata schema for this dataset has
  drifted before. The script matches agency names and keywords against *all*
  string fields on each row rather than hard-coded field names, specifically
  to survive that — but if City Record ever restructures the dataset ID
  itself, the API call will start failing and you'll see it in the "Fetch
  errors" section of the report rather than silently going quiet.
- **DASNY/BPCA selectors**: built and tested against the live page structure
  as of today, but both are ordinary websites that can be redesigned without
  notice. Same failure mode — errors show up in the report, they don't fail
  silently.
- **BPCA has no per-item stable ID** (it's a flat list of PDF links, not a
  database), so new-item detection there is by project title text. If BPCA
  ever renames a project mid-solicitation you could get a false "new" entry
  — low cost (you just see something you'd already noticed), so I left it
  simple rather than over-engineering it.
