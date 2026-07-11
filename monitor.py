#!/usr/bin/env python3
"""
Procurement Monitor — NYCHA / NYC SCA / NYC DDC / BPCA / NYC EDC / DASNY / NYC H+H

Checks each agency's procurement source, compares against a saved "seen" state
file, and writes a Markdown report of newly-posted RFPs/bids since the last run.

Design notes
------------
- NYCHA, NYC SCA, NYC DDC, NYC EDC, and NYC H+H are all required to publish
  solicitations in the City Record. Rather than five fragile HTML scrapers,
  this pulls all five from NYC Open Data's "City Record Online" Socrata
  dataset (id: dg92-zbpx) with one API call per agency, filtered by category.
- DASNY and BPCA are NY State authorities, not covered by the City Record,
  so they're scraped directly from their own opportunity pages.
- State is persisted to state/seen_rfps.json (one JSON file, git-tracked if
  you run this via GitHub Actions — see the accompanying workflow). Anything
  not in that file yet is reported as "new" and then added to it.

Run:
    python monitor.py

Output:
    reports/rfp_report_<DATE>.md   (always written)
    state/seen_rfps.json           (updated in place)

Exit code 0 always (so a CI schedule doesn't get marked "failed" just because
zero new postings were found). Network/parse errors for one source are caught
and reported inline so one broken source doesn't kill the whole run.
"""

import json
import os
import re
import sys
import hashlib
import datetime as dt
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
REPORTS_DIR = ROOT / "reports"
STATE_FILE = STATE_DIR / "seen_rfps.json"
HISTORY_FILE = STATE_DIR / "history.json"
DOCS_DIR = ROOT / "docs"

USER_AGENT = "Mozilla/5.0 (compatible; RFPMonitor/1.0; +https://github.com/)"
REQUEST_TIMEOUT = 30

# NYC agencies pulled via the City Record Online Socrata API.
# `match` strings are matched case-insensitively as substrings against the
# agency name field returned by the API — kept loose because City Record
# agency-name formatting is inconsistent (e.g. "NYC HOUSING AUTHORITY" vs
# "NEW YORK CITY HOUSING AUTHORITY").
#
# NYCHA, DDC, and EDC also each have their own procurement page, which
# sometimes posts a notice days before (or after) it shows up in City Record.
# Both sources are kept and cross-source duplicates are merged — see
# dedupe_items() / title_similar() below. SCA's own site is a JS app with no
# server-rendered content (confirmed — fetching it returns an empty table),
# so it isn't scraped directly. H+H explicitly states on their own site that
# all their RFPs are published exclusively via City Record, so no separate
# H+H source exists to add.
CITY_RECORD_AGENCIES = {
    "NYCHA": ["HOUSING AUTHORITY", "NYCHA"],
    "NYC SCA": ["SCHOOL CONSTRUCTION AUTHORITY", "SCA"],
    "NYC DDC": ["DESIGN AND CONSTRUCTION", "DDC"],
    "NYC EDC": ["ECONOMIC DEVELOPMENT CORPORATION", "NYCEDC", "EDC"],
    "NYC H+H": ["HEALTH AND HOSPITALS", "HHC", "H+H"],
}

# Agencies that have a second, agency-run source in addition to City Record.
# NYCHA and DDC were removed from this set deliberately:
#   - NYCHA's page (nyc.gov) is behind Akamai bot protection that returns
#     "Access Denied" to automated browsers from data-center IPs (verified
#     from the rendered page content). Not fixable in code.
#   - DDC's page (ddcrfpdocuments.nyc.gov) consistently times out from
#     GitHub Actions runners even with the loosest wait conditions, and
#     DDC's own site states all solicitations are released via PASSPort.
# Both agencies are fully covered by the Current Solicitations dataset +
# City Record + PASSPort, so dropping the blocked scrapes loses nothing
# and removes two permanent error entries from every report.
AGENCIES_WITH_OWN_SITE = {"NYC EDC", "NYC SCA"}

CITY_RECORD_DETAIL_URL = "https://a856-cityrecord.nyc.gov/RequestDetail/{id}"

# NYC Open Data "Current Solicitations" dataset — appears to be the
# PASSPort-era current-solicitations listing. Queried alongside City Record
# for all 5 NYC agencies; dedup handles the overlap.
CURRENT_SOLICITATIONS_BASE = "https://data.cityofnewyork.us/resource/3khw-qi8f.json"

# PASSPort Public — MOCS's no-login public browse of PASSPort solicitations.
# NYC agencies (DDC explicitly, per their own RFP page) now release
# solicitations through PASSPort, so this is a primary source, not a bonus.
PASSPORT_PUBLIC_URL = "https://a0333-passportpublic.nyc.gov/rfx.html"

# Per-run fetch diagnostics (source -> counts), included at the bottom of
# every report so failures/filtering behavior are visible without needing
# to reproduce runs locally.
DIAGNOSTICS = []


def diag(msg):
    DIAGNOSTICS.append(msg)

NYCHA_OWN_URL = "https://www.nyc.gov/site/nycha/business/procurement-opportunities.page"
DDC_OWN_URL = "https://ddcrfpdocuments.nyc.gov/rfp/"
EDC_OWN_URL = "https://edc.nyc/rfps"

# Cross-source dedup: two items (from any two sources, for the same agency)
# are treated as the same underlying project if their normalized-title token
# overlap is >= this ratio. 0.6 in practice means "same project referenced
# with mostly the same words" — tight enough to not merge unrelated RFPs
# that happen to share a common phrase like "construction management".
DEDUPE_OVERLAP_THRESHOLD = 0.6

STOPWORDS = {
    "the", "a", "an", "for", "of", "and", "to", "or", "with", "at", "in", "on",
    "services", "service", "rfp", "rfq", "rfei", "rfi", "request", "proposals",
    "proposal", "qualifications", "qualification", "solicitation", "contract",
    "contracts", "nyc", "nycha", "sca", "ddc", "edc", "bpca", "dasny", "hh",
    "citywide", "various", "new", "york", "city",
}

CITY_RECORD_DATASET = "dg92-zbpx"
CITY_RECORD_BASE = f"https://data.cityofnewyork.us/resource/{CITY_RECORD_DATASET}.json"

# How many days back to pull from City Record on each run. Wide enough to be
# safe against a missed run, narrow enough to keep the payload small. The
# "new" determination is made by the local seen-state diff, not this window.
LOOKBACK_DAYS = 10

# Notice categories/types worth surfacing — City Record also carries public
# hearings, personnel actions, agency rule changes, etc. we don't want.
# Matched case-insensitively as substrings against whatever category/type
# field is present on each record.
SOLICITATION_KEYWORDS = [
    "solicitation", "rfp", "request for proposal", "rfq",
    "request for qualification", "bid", "procurement", "award",
    "expression of interest", "eoi", "rfi", "request for information",
]

DASNY_URL = "https://www.dasny.org/opportunities/rfps-bids"
BPCA_URL = "https://bpca.ny.gov/apply/rfp-opp/"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _get(url, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    return requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)


def _blob_matches(blob, match_strings):
    """True if any match string is found in blob. Short strings (acronyms
    like 'EDC', 'SCA' — 4 chars or less) are matched on word boundaries to
    avoid false positives (e.g. 'EDC' inside an unrelated word); longer
    phrases use a plain substring check since they're specific enough not
    to need it."""
    for m in match_strings:
        ml = m.lower()
        if len(ml) <= 4:
            if re.search(r"\b" + re.escape(ml) + r"\b", blob):
                return True
        elif ml in blob:
            return True
    return False


def _stable_id(*parts):
    """Deterministic short hash used as a dedupe key for scraped items that
    don't have a clean native ID."""
    raw = "||".join(p.strip().lower() for p in parts if p)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse_date_safe(value):
    """Best-effort parse of a date-ish string into a date object. City
    Record's date fields show up as plain 'YYYY-MM-DD' or with a time
    component like 'YYYY-MM-DDT00:00:00.000' — this handles both and
    returns None for anything else rather than raising."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _find_date_field(row, keywords):
    """Scan row keys for the first one matching any of `keywords` (substring,
    case-insensitive) whose value parses as a date. Used instead of a fixed
    field name because City Record's exact column names have drifted before
    and aren't guaranteed to match what's hard-coded here."""
    for key, value in row.items():
        if any(kw in key.lower() for kw in keywords):
            parsed = _parse_date_safe(value)
            if parsed:
                return parsed
    return None


def _is_currently_relevant(row, lookback_days):
    """The actual bug fix: without this, every notice ever published for an
    agency (award notices, corrections, hearings going back to the 2000s)
    passes the keyword filter just as easily as a real open solicitation,
    because nothing was checking dates at all. This keeps a row only if:
      - it has a closing/due date that hasn't passed yet (still open), or
      - it has no detectable closing date but was posted within the
        lookback window (recent enough to be worth surfacing).
    Anything else — old, closed, or otherwise stale — is dropped."""
    today = dt.date.today()
    close_date = _find_date_field(row, ["end_date", "enddate", "due", "close", "closing", "deadline"])
    if close_date is not None:
        return close_date >= today

    posted_date = _find_date_field(row, ["start_date", "startdate", "issue_date", "issuedate", "posted", "date"])
    if posted_date is not None:
        return (today - posted_date).days <= lookback_days

    # No usable date at all — err toward excluding rather than flooding the
    # report with undatable historical noise.
    return False


def render_page(url, wait_ms=4000, wait_selector=None, timeout_ms=30000):
    """Fully render a JS-driven page with headless Chromium and return the
    resulting HTML. Used for sites (DDC, EDC, SCA) whose current-opportunity
    list is populated client-side after page load, where a plain requests.get
    only sees the empty shell.

    wait_selector: if given, wait for this CSS selector to appear before
    grabbing HTML (more reliable than a fixed sleep, when known). Falls back
    to a fixed wait_ms if the selector never appears, rather than failing
    outright — some of these sites may render an empty "no results" state
    that never adds the selector, which is a legitimate outcome, not an error.

    Tries wait_until="domcontentloaded" first; if even that times out
    (some sites are extremely slow to serve data-center IPs like GitHub
    Actions runners, or hold the initial response open), retries once with
    wait_until="commit" — which only waits for the response to START — and
    then relies on the settle wait / selector wait to give content time to
    arrive. Better a late page than a guaranteed timeout.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(
                user_agent=USER_AGENT,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            except Exception:
                # Retry with the loosest possible wait condition
                page.goto(url, timeout=timeout_ms, wait_until="commit")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=timeout_ms)
                except Exception:
                    pass
            page.wait_for_timeout(wait_ms)
            html = page.content()
        finally:
            browser.close()
    return html


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def save_history(history):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


# --------------------------------------------------------------------------
# Cross-source dedup
#
# The same project can post to an agency's own site and to City Record days
# apart in either order. We don't want that to produce two "new" entries —
# one when it first appears on whichever source got it first, and another
# when it shows up on the second source later. So instead of diffing on a
# raw per-source ID, "seen" state is a set of normalized-title fingerprints
# per agency: any new item whose title overlaps enough with something
# already seen (from *any* source, on *any* previous day) is treated as
# already-reported, not new.
# --------------------------------------------------------------------------

def title_tokens(title):
    t = re.sub(r"[^a-z0-9\s]", " ", title.lower())
    return frozenset(w for w in t.split() if w not in STOPWORDS and len(w) > 2)


def title_similar(tokens_a, tokens_b, threshold=DEDUPE_OVERLAP_THRESHOLD):
    if not tokens_a or not tokens_b:
        return False
    overlap = len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))
    return overlap >= threshold


def dedupe_items(items):
    """Collapse items from multiple sources (for one agency, one run) that
    describe the same project into a single entry. When two items match,
    keeps whichever has a specific per-item URL rather than a generic
    homepage link, and merges the source list so the report can show
    'seen on: NYCHA site, City Record' for transparency."""
    GENERIC_URLS = {"https://a856-cityrecord.nyc.gov/"}
    kept = []
    kept_tokens = []

    for it in items:
        tok = title_tokens(it["title"])
        match_i = next(
            (i for i, kt in enumerate(kept_tokens) if title_similar(tok, kt)),
            None,
        )
        if match_i is None:
            it = dict(it)
            it["sources"] = [it.get("source", "")]
            kept.append(it)
            kept_tokens.append(tok)
            continue

        existing = kept[match_i]
        existing_sources = existing.get("sources", [existing.get("source", "")])
        new_source = it.get("source", "")
        if new_source not in existing_sources:
            existing_sources = existing_sources + [new_source]

        existing_generic = existing.get("url") in GENERIC_URLS or not existing.get("url")
        new_generic = it.get("url") in GENERIC_URLS or not it.get("url")
        if existing_generic and not new_generic:
            merged = dict(it)
        else:
            merged = existing
        merged["sources"] = existing_sources
        kept[match_i] = merged
        # widen the fingerprint with the union of tokens so a third source
        # using slightly different wording still has a chance to match
        kept_tokens[match_i] = kept_tokens[match_i] | tok

    return kept


def diff_against_state(agency, items, state):
    """Returns (new_items, updated_fingerprint_list) for one agency using
    title-fingerprint matching against everything seen on any prior run."""
    seen_fp = [frozenset(fp) for fp in state.get(agency, [])]
    new_items = []
    for it in items:
        tok = title_tokens(it["title"])
        if any(title_similar(tok, fp) for fp in seen_fp):
            continue
        new_items.append(it)
        seen_fp.append(tok)
    updated = [sorted(fp) for fp in seen_fp]
    return new_items, updated


# --------------------------------------------------------------------------
# Source: City Record Online (NYCHA, NYC SCA, NYC DDC, NYC EDC, NYC H+H)
# --------------------------------------------------------------------------

def fetch_city_record(agency_label, match_strings):
    """Pull City Record notices for one agency and keep only ones that look
    like a solicitation/RFP/bid and are still open (or recently posted).

    Strategy: ALWAYS do the broad recent-rows pull (5000 rows ordered by
    :id DESC) — this is empirically proven to contain current postings for
    our agencies (verified against live data). The Socrata full-text $q
    search per match string is run as a supplement to catch anything that
    fell outside the broad slice. An earlier version used $q as the ONLY
    method, and it silently missed current records the broad pull was
    finding — whatever tokenization $q applies to this dataset does not
    reliably match our agency name strings, so it must never be the sole
    source again. Everything is merged, deduped, then filtered client-side
    by agency match, solicitation keywords, and date relevance.
    """
    all_rows = []
    errors = []

    # Primary: broad recent pull. Ordered by start_date DESC — the field
    # name is confirmed from live diagnostics samples (start_date/end_date/
    # due_date, ISO format). Ordering by :id DESC (the old approach) was
    # empirically dominated by 2011-2013 records; ordering by the actual
    # posting date puts current notices at the front of the slice. Falls
    # back to :id ordering if the server rejects the field name.
    try:
        params = {"$limit": 5000, "$order": "start_date DESC"}
        resp = _get(CITY_RECORD_BASE, params=params)
        resp.raise_for_status()
        all_rows.extend(resp.json())
    except Exception:
        try:
            params = {"$limit": 5000, "$order": ":id DESC"}
            resp = _get(CITY_RECORD_BASE, params=params)
            resp.raise_for_status()
            all_rows.extend(resp.json())
        except Exception as e:
            errors.append(f"broad pull: {e}")

    # Supplement: targeted full-text searches
    for match in match_strings:
        try:
            params = {"$limit": 1000, "$q": match}
            resp = _get(CITY_RECORD_BASE, params=params)
            resp.raise_for_status()
            all_rows.extend(resp.json())
        except Exception as e:
            errors.append(f"'{match}' search: {e}")

    if not all_rows:
        if errors:
            return [], f"ERROR fetching City Record for {agency_label}: {'; '.join(errors)}"
        return [], None

    n_agency = n_keyword = n_date = 0
    items = []
    seen_rows = set()
    for row in all_rows:
        row_key = json.dumps(row, sort_keys=True)
        if row_key in seen_rows:
            continue
        seen_rows.add(row_key)

        blob = " ".join(str(v) for v in row.values() if isinstance(v, str)).lower()
        if not _blob_matches(blob, match_strings):
            continue
        n_agency += 1
        if not any(k in blob for k in SOLICITATION_KEYWORDS):
            continue
        n_keyword += 1
        if not _is_currently_relevant(row, LOOKBACK_DAYS):
            continue
        n_date += 1

        title = (
            row.get("short_title") or row.get("title") or row.get("description")
            or row.get("notice_description") or next(
                (v for v in row.values() if isinstance(v, str) and len(v) > 15), "Untitled notice"
            )
        )
        # City Record notice detail pages live at a856-cityrecord.nyc.gov/RequestDetail/<id>.
        # The Socrata field holding that id has been observed as "requestid";
        # fall back to a generic (non-deep-linkable) URL if it's not present
        # under any of the likely field names, rather than guessing wrong.
        detail_id = (
            row.get("requestid") or row.get("request_id")
            or row.get(":id") or row.get("pin") or row.get("id")
        )
        if detail_id and re.match(r"^\d+$", str(detail_id)):
            url = CITY_RECORD_DETAIL_URL.format(id=detail_id)
        else:
            url = "https://a856-cityrecord.nyc.gov/"

        row_id = detail_id or _stable_id(title, agency_label)
        date_field = row.get("start_date") or row.get("date") or row.get("issue_date") or ""

        items.append({
            "id": str(row_id),
            "agency": agency_label,
            "title": title.strip()[:300],
            "date": str(date_field)[:10],
            "url": url,
            "source": "NYC City Record Online",
        })

    # de-dupe by id within this pull
    seen_ids = set()
    deduped = []
    for it in items:
        if it["id"] in seen_ids:
            continue
        seen_ids.add(it["id"])
        deduped.append(it)

    diag(
        f"City Record [{agency_label}]: {len(all_rows)} rows fetched, "
        f"{n_agency} matched agency, {n_keyword} matched solicitation keywords, "
        f"{n_date} passed date filter, {len(deduped)} after id-dedup"
    )
    # If keyword matches exist but nothing passes the date filter, sample
    # one matched row's date-ish fields so the report reveals what format
    # the dataset actually uses (the current 0-pass behavior suggests our
    # date parsing doesn't match the real field names/formats).
    if n_keyword > 0 and n_date == 0:
        for row in all_rows:
            blob = " ".join(str(v) for v in row.values() if isinstance(v, str)).lower()
            if _blob_matches(blob, match_strings) and any(k in blob for k in SOLICITATION_KEYWORDS):
                datey = {k: v for k, v in row.items()
                         if any(w in k.lower() for w in ("date", "due", "close", "deadline", "start", "end"))}
                diag(f"City Record [{agency_label}] sample date fields: {json.dumps(datey)[:400]}")
                break
    return deduped, None


# --------------------------------------------------------------------------
# Source: NYC Open Data "Current Solicitations" (PASSPort-era dataset)
# --------------------------------------------------------------------------

def fetch_current_solicitations(agency_label, match_strings):
    """NYC Open Data's 'Current Solicitations' dataset (3khw-qi8f) — by its
    name and description, a listing of currently-open solicitations, which
    is exactly the shape we want (no date-filter gymnastics needed to weed
    out decades of history, unlike City Record). Same client-side agency
    matching as City Record; field names are matched loosely since the
    schema wasn't verifiable at build time."""
    try:
        resp = _get(CURRENT_SOLICITATIONS_BASE, params={"$limit": 5000})
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        return [], f"ERROR fetching Current Solicitations for {agency_label}: {e}"

    items = []
    n_agency = 0
    n_current = 0
    for row in rows:
        blob = " ".join(str(v) for v in row.values() if isinstance(v, str)).lower()
        if not _blob_matches(blob, match_strings):
            continue
        n_agency += 1

        title = (
            row.get("title") or row.get("short_title") or row.get("solicitation_title")
            or row.get("description") or next(
                (v for v in row.values() if isinstance(v, str) and len(v) > 15),
                "Untitled solicitation",
            )
        )
        rid = (row.get("pin") or row.get("epin") or row.get("id")
               or _stable_id(title, agency_label))
        date_field = (row.get("release_date") or row.get("start_date")
                      or row.get("posted") or row.get("date") or "")
        due_field = (row.get("due_date") or row.get("end_date")
                     or row.get("deadline") or "")

        # Only keep currently-relevant items: still open (due date hasn't
        # passed), or recently posted when no due date is parseable. The
        # dataset contains the full multi-year history of solicitations,
        # not just open ones, so skipping this filter floods the report
        # with years-old closed items (verified empirically).
        today = dt.date.today()
        due_parsed = _parse_date_safe(str(due_field))
        posted_parsed = _parse_date_safe(str(date_field))
        if due_parsed is not None:
            if due_parsed < today:
                continue
        elif posted_parsed is not None:
            if (today - posted_parsed).days > LOOKBACK_DAYS:
                continue
        else:
            continue  # no usable date at all — exclude rather than flood
        n_current += 1

        items.append({
            "id": str(rid),
            "agency": agency_label,
            "title": str(title).strip()[:300],
            "date": str(date_field)[:10],
            "due": str(due_field)[:10],
            "url": "https://a0333-passportpublic.nyc.gov/rfx.html",
            "source": "NYC Current Solicitations",
        })

    diag(f"Current Solicitations [{agency_label}]: {len(rows)} rows fetched, "
         f"{n_agency} matched agency, {n_current} currently open/recent")
    return items, None


# --------------------------------------------------------------------------
# Source: PASSPort Public (MOCS public browse of PASSPort RFx)
# --------------------------------------------------------------------------

def fetch_passport(agency_label, match_strings):
    """PASSPort Public's RFx browse — no login required. NYC agencies now
    release solicitations through PASSPort (DDC states this explicitly on
    their own RFP page), making this a primary source. The page is a
    client-rendered app, so it's rendered with headless Chromium and parsed
    generically from table rows. An EPIN-style code in the row text is used
    as the stable id where present."""
    items = []
    error = None
    try:
        html = render_page(PASSPORT_PUBLIC_URL, wait_ms=8000, wait_selector="table")
        soup = BeautifulSoup(html, "html.parser")

        rows = soup.select("table tr")
        n_rows = 0
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            n_rows += 1
            row_text = " ".join(cells).lower()
            if not _blob_matches(row_text, match_strings):
                continue

            title = max(cells, key=len)
            if len(title) < 10:
                continue
            # Require an EPIN/solicitation-number-like pattern in the row.
            # Real PASSPort solicitation rows carry an EPIN; page UI
            # elements that render as table rows (e.g. the agency filter
            # dropdown — a giant concatenation of every agency name, no
            # digits) do not. This rejects those.
            epin = re.search(r"\b\d{5,}[A-Z]?\d*[A-Z0-9]*\b", " ".join(cells))
            if not epin:
                continue
            rid = epin.group(0)

            items.append({
                "id": rid,
                "agency": agency_label,
                "title": title[:300],
                "date": "",
                "url": PASSPORT_PUBLIC_URL,
                "source": "PASSPort Public",
            })
        diag(f"PASSPort [{agency_label}]: {n_rows} table rows rendered, "
             f"{len(items)} matched agency")
    except Exception as e:
        # Demoted to a diagnostic rather than a fetch error: the Current
        # Solicitations dataset appears to carry the same PASSPort-era data
        # via a stable API, so a failed render of this JS-heavy page isn't
        # a coverage gap worth alarming on every report.
        diag(f"PASSPort [{agency_label}]: render/parse failed: {e}")

    return items, error


# --------------------------------------------------------------------------
# Source: NYCHA's own procurement page
# --------------------------------------------------------------------------

def fetch_nycha_own():
    """NYCHA's procurement page includes a '<year> Proposer Pre-Bidders
    Conference Attendance List' section listing current RFPs/RFQs by
    solicitation number, e.g. 'RFQ #521185 IDIQ Contract for...'. That
    solicitation number is used as the stable id.

    Uses render_page() (headless Chromium) rather than a plain requests.get:
    nyc.gov has been observed returning 403 Forbidden to plain HTTP
    requests from a data-center IP (GitHub Actions runners included) even
    though the page itself needs no JavaScript — a real browser fingerprint
    gets through where a bare requests.get gets blocked."""
    items = []
    error = None
    try:
        html = render_page(NYCHA_OWN_URL, wait_ms=2000)
        soup = BeautifulSoup(html, "html.parser")

        heading = soup.find(
            lambda tag: tag.name in ("h2", "h3")
            and "pre-bidders conference attendance list" in tag.get_text(strip=True).lower()
        )

        candidate_links = []
        if heading is not None:
            ul = heading.find_next("ul")
            if ul is not None:
                candidate_links = ul.find_all("li")

        if not candidate_links:
            # Fallback: the exact heading wording can drift (year changes,
            # rewording) without the underlying RFQ/RFP links disappearing.
            # Scan the whole page for anything matching "RFQ #12345 ..." /
            # "RFP #12345 ..." link text instead of relying on a heading.
            candidate_links = [
                a for a in soup.find_all("a")
                if re.match(r"\s*(RFQ|RFP|RFEI)\s*#\s*\d", a.get_text(strip=True))
            ]

        if not candidate_links:
            # Include a snippet of what the page actually contained so a
            # failure is debuggable from the report alone (e.g. reveals a
            # bot-check interstitial or redirect instead of the real page).
            page_text = soup.get_text(" ", strip=True)[:300]
            error = ("ERROR: NYCHA own-site: no RFQ/RFP links found. "
                     f"Rendered page begins: \"{page_text}\"")
            return items, error

        for li in candidate_links:
            a = li.find("a") if li.name != "a" else li
            if not a:
                continue
            text = a.get_text(strip=True)
            m = re.match(r"(RFQ|RFP|RFEI)\s*#\s*([\d,\-&\s]+?)\s*[-–]\s*(.+)", text)
            if m:
                sol_num = m.group(2).strip()
                title = m.group(3).strip()
            else:
                sol_num = _stable_id(text)
                title = text

            href = a.get("href")
            if href and href.startswith("/"):
                href = "https://www1.nyc.gov" + href

            items.append({
                "id": sol_num,
                "agency": "NYCHA",
                "title": title,
                "date": "",
                "url": href or NYCHA_OWN_URL,
                "source": "NYCHA Procurement Opportunities",
            })
    except Exception as e:
        error = f"ERROR fetching NYCHA own site: {e}"

    return items, error


# --------------------------------------------------------------------------
# Source: DDC's own RFP page
# --------------------------------------------------------------------------

def fetch_ddc_own():
    """DDC's RFP documents page lists open RFPs with a PIN, title, and
    posting date, but populates the Open/Closed tab content client-side —
    a plain HTTP fetch only sees an empty shell. Rendered with headless
    Chromium first, then parsed the same way.

    This source has been observed timing out even on domcontentloaded
    (i.e. the connection itself is slow/unresponsive, not just JS-heavy) —
    possibly this subdomain rate-limits or blocks traffic from data-center
    IP ranges like GitHub Actions runners. Timeout raised to give it a fair
    chance; if it keeps failing, City Record remains the backstop for DDC
    regardless, so this isn't a silent gap."""
    items = []
    error = None
    try:
        html = render_page(DDC_OWN_URL, wait_ms=5000, timeout_ms=45000)
        soup = BeautifulSoup(html, "html.parser")
        text_blob = soup.get_text("\n", strip=True)

        # Entries look like: "<PIN> <Title>, Posted <date>" — PIN is a
        # long alphanumeric code (e.g. 8502019HW0020P).
        for m in re.finditer(
            r"\b(\d{7,}[A-Z0-9]*)\s+([^\n]+?)(?:,\s*Posted\s*([\d/]+))?\n", text_blob
        ):
            pin, title, posted = m.group(1), m.group(2).strip(), m.group(3) or ""
            if len(title) < 8:
                continue
            items.append({
                "id": pin,
                "agency": "NYC DDC",
                "title": title[:300],
                "date": posted,
                "url": DDC_OWN_URL,
                "source": "DDC RFP Documents",
            })
    except Exception as e:
        error = f"ERROR fetching DDC own site: {e}"

    return items, error


# --------------------------------------------------------------------------
# Source: EDC's own RFP page
# --------------------------------------------------------------------------

def fetch_edc_own():
    """EDC's /rfps landing page links out to individual project pages
    (edc.nyc/<project-slug>), but the live listing is populated client-side.
    Rendered with headless Chromium first, then parsed the same way."""
    items = []
    error = None
    try:
        html = render_page(EDC_OWN_URL, wait_ms=5000)
        soup = BeautifulSoup(html, "html.parser")

        skip_slugs = {"rfps", "opportunity-mwdbe", "subcontractors-and-suppliers",
                      "upcoming-procurement-opportunities", "vendor-resources"}
        # Generic hub/informational pages that happen to contain "rfp" etc.
        # in their URL slug or boilerplate copy, but aren't an actual
        # solicitation (e.g. "Join NYCEDC's Vendors List for Contracting
        # Opportunities" — a general vendor-registration page, not an RFP).
        EXCLUDE_PHRASES = re.compile(
            r"vendors?\s*list|become a vendor|vendor registration|"
            r"how to do business|mwbe program|supplier diversity",
            re.I,
        )
        seen_slugs = set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            m = re.match(r"^/([a-z0-9\-]+)/?$", href)
            if not m:
                continue
            slug = m.group(1)
            if slug in skip_slugs or slug in seen_slugs:
                continue
            text = a.get_text(strip=True)
            if len(text) < 8 or not re.search(r"rfp|rfq|rfei|proposal", text + " " + slug, re.I):
                continue
            if EXCLUDE_PHRASES.search(text):
                continue
            seen_slugs.add(slug)
            items.append({
                "id": slug,
                "agency": "NYC EDC",
                "title": text[:300],
                "date": "",
                "url": "https://edc.nyc/" + slug,
                "source": "EDC RFPs",
            })
    except Exception as e:
        error = f"ERROR fetching EDC own site: {e}"

    return items, error


# --------------------------------------------------------------------------
# Source: SCA's own bid list (Blazor app — needs rendering, previously
# skipped entirely for this reason; Playwright unblocks it)
# --------------------------------------------------------------------------

SCA_OWN_URL = "https://scainfohub.azurewebsites.net/advertised-bids"


def fetch_sca_own():
    """SCA's bid list is a client-rendered Blazor app — a plain HTTP fetch
    returns an empty table with no server-side content at all (confirmed).
    Rendered with headless Chromium and given extra time for Blazor's
    slower client-side hydration, then parsed generically: any table row
    or list item containing what looks like a bid/solicitation number."""
    items = []
    error = None
    try:
        html = render_page(SCA_OWN_URL, wait_ms=8000, wait_selector="table")
        soup = BeautifulSoup(html, "html.parser")

        rows = soup.select("table tr")
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            # Skip header rows
            if any(h.lower() in ("bid", "solicitation", "title", "description")
                   for h in cells[:1]):
                continue
            row_text = " ".join(cells)
            num_match = re.search(r"\b\d{4,}\b", row_text)
            if not num_match:
                continue
            title = max(cells, key=len)
            if len(title) < 8:
                continue
            items.append({
                "id": num_match.group(0),
                "agency": "NYC SCA",
                "title": title[:300],
                "date": "",
                "url": SCA_OWN_URL,
                "source": "SCA Advertised Bids",
            })
    except Exception as e:
        error = f"ERROR fetching SCA own site: {e}"

    return items, error


# --------------------------------------------------------------------------
# Source: DASNY
# --------------------------------------------------------------------------

def fetch_dasny():
    items = []
    error = None
    page = 0
    max_pages = 6  # safety cap

    try:
        while page < max_pages:
            url = DASNY_URL if page == 0 else f"{DASNY_URL}?page={page}"
            resp = _get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Each opportunity is an <h2> (or h3) with a link to its detail page,
            # followed by a definition/table block with Solicitation #, dates, etc.
            headings = soup.select("h2 a[href*='/opportunities/rfps-bids/']")
            if not headings:
                break

            for a in headings:
                title = a.get_text(strip=True)
                href = a.get("href")
                if href and href.startswith("/"):
                    href = "https://www.dasny.org" + href

                # Walk forward to the nearest following table for metadata
                container = a.find_parent(["h2", "div"])
                table_text = ""
                nxt = container.find_next("table") if container else None
                if nxt:
                    table_text = nxt.get_text(" ", strip=True)

                sol_match = re.search(r"Solicitation #:\s*([^\s|]+)", table_text)
                issue_match = re.search(r"Issue Date:\s*([\d/]+)", table_text)
                due_match = re.search(r"Due Date:\s*([\d/:\sAPM-]+?)(?:Classification|$)", table_text)
                status_match = re.search(r"Status:\s*(\w+)", table_text)

                sol_num = sol_match.group(1) if sol_match else None
                item_id = sol_num or _stable_id(title, href or "")

                items.append({
                    "id": item_id,
                    "agency": "DASNY",
                    "title": title,
                    "date": issue_match.group(1) if issue_match else "",
                    "due": due_match.group(1).strip() if due_match else "",
                    "status": status_match.group(1) if status_match else "",
                    "url": href or DASNY_URL,
                    "source": "DASNY RFPs & Bids",
                })

            # stop if this was the last page
            if not soup.select("li a[href*='page=']"):
                break
            page += 1

    except Exception as e:
        error = f"ERROR fetching DASNY: {e}"

    return items, error


# --------------------------------------------------------------------------
# Source: BPCA
# --------------------------------------------------------------------------

def fetch_bpca():
    """Uses render_page() rather than plain requests.get — bpca.ny.gov has
    started returning 403 Forbidden to plain HTTP requests from a
    data-center IP (same pattern observed on nyc.gov / NYCHA), even though
    this page itself needs no JavaScript. A real browser fingerprint gets
    through where a bare requests.get gets blocked."""
    items = []
    error = None
    try:
        html = render_page(BPCA_URL, wait_ms=2000)
        soup = BeautifulSoup(html, "html.parser")

        # BPCA's page is a flat list of bolded project-title links (PDFs) under
        # "Current Procurement Opportunities", grouped by project. We treat
        # each distinct bolded title (stripping trailing " – AD" / " – RFP"
        # / " – Addendum N" suffixes) as one opportunity.
        heading = soup.find(string=re.compile("Current Procurement Opportunities"))
        scope = heading.find_parent(["div", "section"]) if heading else soup

        seen_titles = set()
        for a in scope.select("a"):
            text = a.get_text(strip=True)
            if not text or "media.bpca.ny.gov" not in (a.get("href") or ""):
                continue
            # Strip common suffixes to get the project name
            base = re.split(r"\s*[–-]\s*(AD|RFP|RFI|RFQ|Addendum\s*\d*|Solicitation|Discretionary.*)$",
                             text, flags=re.IGNORECASE)[0].strip()
            if not base or base in seen_titles:
                continue
            seen_titles.add(base)

            items.append({
                "id": _stable_id(base),
                "agency": "BPCA",
                "title": base,
                "date": "",
                "url": a.get("href"),
                "source": "BPCA Procurement Opportunities",
            })
    except Exception as e:
        error = f"ERROR fetching BPCA: {e}"

    return items, error


# --------------------------------------------------------------------------
# Report generation
# --------------------------------------------------------------------------

def build_report(new_by_agency, errors, run_date):
    lines = [f"# Daily RFP Report — {run_date}", ""]

    total_new = sum(len(v) for v in new_by_agency.values())
    if total_new == 0:
        lines.append("No new postings found today across any tracked agency.")
    else:
        lines.append(f"**{total_new} new posting(s) found.**")

    lines.append("")

    for agency in ["NYCHA", "NYC SCA", "NYC DDC", "BPCA", "NYC EDC", "DASNY", "NYC H+H"]:
        items = new_by_agency.get(agency, [])
        lines.append(f"## {agency} ({len(items)} new)")
        if not items:
            lines.append("_No new postings._")
        else:
            for it in items:
                title = it["title"]
                url = it.get("url", "")
                date = it.get("date", "")
                due = it.get("due", "")
                sources = it.get("sources") or [it.get("source", "")]
                meta_bits = [b for b in [f"Posted: {date}" if date else "", f"Due: {due}" if due else ""] if b]
                if len(sources) > 1:
                    meta_bits.append("seen on: " + " + ".join(s for s in sources if s))
                meta = f" ({', '.join(meta_bits)})" if meta_bits else ""
                lines.append(f"- [{title}]({url}){meta}")
        lines.append("")

    if errors:
        lines.append("## Fetch errors")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")

    if DIAGNOSTICS:
        lines.append("## Diagnostics (for debugging — safe to ignore)")
        for d in DIAGNOSTICS:
            lines.append(f"- {d}")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Dashboard (static site, published via GitHub Pages from /docs)
#
# Design intent: a municipal filing-register look — the actual visual
# vocabulary of the subject (procurement notices, date-stamped intake,
# agency folder tabs) rather than a generic dashboard template. Each entry
# gets a rotated ink-stamp badge showing the date it was first detected,
# echoing how a real procurement office date-stamps incoming filings.
# Single self-contained HTML file: data is embedded inline as JSON so it
# works from GitHub Pages or a local double-click alike, no build step.
# --------------------------------------------------------------------------

AGENCY_ORDER = ["NYCHA", "NYC SCA", "NYC DDC", "BPCA", "NYC EDC", "DASNY", "NYC H+H"]

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Procurement Register</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --paper: #EDEAE1;
    --paper-raised: #F5F3EC;
    --ink: #1E2A44;
    --ink-soft: #4B5670;
    --rule: #C9C2AC;
    --brass: #A9782F;
    --verdigris: #3C6E62;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: 'IBM Plex Sans', sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  a { color: inherit; }
  header {
    padding: 3rem 1.5rem 1.5rem;
    max-width: 900px;
    margin: 0 auto;
    border-bottom: 1px solid var(--rule);
  }
  h1 {
    font-family: 'Fraunces', serif;
    font-weight: 700;
    font-size: clamp(1.8rem, 4vw, 2.6rem);
    margin: 0 0 0.25rem;
    letter-spacing: -0.01em;
  }
  .subtitle {
    color: var(--ink-soft);
    font-size: 0.95rem;
    margin-bottom: 1.5rem;
  }
  .subtitle .count { color: var(--brass); font-weight: 600; }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }
  input[type="search"] {
    font-family: 'IBM Plex Mono', monospace;
    background: var(--paper-raised);
    border: 1px solid var(--rule);
    color: var(--ink);
    padding: 0.55rem 0.8rem;
    border-radius: 3px;
    font-size: 0.9rem;
    flex: 1 1 220px;
    min-width: 0;
  }
  input[type="search"]:focus-visible, .tab:focus-visible {
    outline: 2px solid var(--verdigris);
    outline-offset: 2px;
  }
  input[type="search"]::placeholder { color: var(--ink-soft); }
  .tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  .tab {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.78rem;
    letter-spacing: 0.02em;
    padding: 0.4rem 0.7rem;
    background: transparent;
    border: 1px solid var(--rule);
    color: var(--ink-soft);
    border-radius: 3px;
    cursor: pointer;
  }
  .tab.active {
    background: var(--ink);
    color: var(--paper);
    border-color: var(--ink);
  }
  main { max-width: 900px; margin: 0 auto; padding: 1.5rem; }
  .agency-section { margin-bottom: 2.2rem; }
  .agency-section.hidden { display: none; }
  .agency-tab-label {
    font-family: 'Fraunces', serif;
    font-weight: 600;
    font-size: 1.15rem;
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    padding-bottom: 0.4rem;
    border-bottom: 2px solid var(--ink);
    margin-bottom: 0.2rem;
  }
  .agency-tab-label .n {
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 400;
    font-size: 0.8rem;
    color: var(--ink-soft);
  }
  .entry {
    display: flex;
    gap: 0.9rem;
    padding: 0.85rem 0;
    border-bottom: 1px solid var(--rule);
  }
  .entry.hidden { display: none; }
  .stamp {
    flex: none;
    width: 58px;
    height: 58px;
    border: 2px solid var(--brass);
    border-radius: 50%;
    color: var(--brass);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.62rem;
    line-height: 1.15;
    text-align: center;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    transform: rotate(-6deg);
    opacity: 0.85;
    letter-spacing: 0.03em;
  }
  .stamp .found { font-size: 0.55rem; opacity: 0.8; }
  .stamp .date { font-weight: 600; font-size: 0.8rem; }
  .entry-body { flex: 1; min-width: 0; }
  .entry-title {
    font-size: 0.98rem;
    font-weight: 500;
    text-decoration: none;
    display: inline-block;
    margin-bottom: 0.3rem;
  }
  .entry-title:hover { color: var(--verdigris); }
  .entry-meta {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.75rem;
    color: var(--ink-soft);
  }
  .entry-meta .sources { color: var(--verdigris); }
  .empty-state {
    color: var(--ink-soft);
    font-size: 0.9rem;
    padding: 1rem 0;
    font-style: italic;
  }
  footer {
    max-width: 900px;
    margin: 0 auto;
    padding: 1.5rem;
    color: var(--ink-soft);
    font-size: 0.78rem;
    font-family: 'IBM Plex Mono', monospace;
  }
  @media (prefers-reduced-motion: no-preference) {
    .entry { transition: opacity 0.15s ease; }
  }
</style>
</head>
<body>
<header>
  <h1>Procurement Register</h1>
  <div class="subtitle"><span class="count">__TOTAL__</span> notice(s) logged since tracking began &mdash; NYCHA, NYC SCA, NYC DDC, NYC EDC, NYC H+H, DASNY, BPCA</div>
  <div class="controls">
    <input type="search" id="search" placeholder="search title...">
    <div class="tabs" id="tabs"></div>
  </div>
</header>
<main id="main"></main>
<footer>Last updated __UPDATED__ &middot; generated by the daily procurement monitor</footer>
<script>
const DATA = __DATA_JSON__;
const AGENCY_ORDER = __AGENCY_ORDER_JSON__;

const main = document.getElementById('main');
const tabsEl = document.getElementById('tabs');
const searchEl = document.getElementById('search');
let activeAgency = 'ALL';

function stampDate(d) {
  if (!d) return '?';
  const parts = d.split('-');
  return parts.length === 3 ? parts[1] + '/' + parts[2] : d;
}

function render() {
  main.innerHTML = '';
  const q = searchEl.value.trim().toLowerCase();

  AGENCY_ORDER.forEach(agency => {
    const items = (DATA[agency] || []).slice().sort((a, b) => (b.found_date || '').localeCompare(a.found_date || ''));
    const section = document.createElement('section');
    section.className = 'agency-section';
    if (activeAgency !== 'ALL' && activeAgency !== agency) section.classList.add('hidden');

    const label = document.createElement('div');
    label.className = 'agency-tab-label';
    label.innerHTML = agency + ' <span class="n">(' + items.length + ')</span>';
    section.appendChild(label);

    const visibleItems = items.filter(it => !q || it.title.toLowerCase().includes(q));
    if (visibleItems.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = q ? 'No matches.' : 'Nothing logged yet.';
      section.appendChild(empty);
    } else {
      visibleItems.forEach(it => {
        const entry = document.createElement('div');
        entry.className = 'entry';
        const sources = it.sources && it.sources.length > 1 ? it.sources.join(' + ') : '';
        entry.innerHTML =
          '<div class="stamp"><span class="found">FOUND</span><span class="date">' + stampDate(it.found_date) + '</span></div>' +
          '<div class="entry-body">' +
            '<a class="entry-title" href="' + (it.url || '#') + '" target="_blank" rel="noopener">' + it.title + '</a>' +
            '<div class="entry-meta">' +
              (it.date ? 'posted ' + it.date + '  ' : '') +
              (it.due ? 'due ' + it.due + '  ' : '') +
              (sources ? '<span class="sources">' + sources + '</span>' : '') +
            '</div>' +
          '</div>';
        section.appendChild(entry);
      });
    }
    main.appendChild(section);
  });
}

function buildTabs() {
  const allTab = document.createElement('button');
  allTab.className = 'tab active';
  allTab.textContent = 'ALL';
  allTab.onclick = () => setActive('ALL', allTab);
  tabsEl.appendChild(allTab);

  AGENCY_ORDER.forEach(agency => {
    const btn = document.createElement('button');
    btn.className = 'tab';
    btn.textContent = agency;
    btn.onclick = () => setActive(agency, btn);
    tabsEl.appendChild(btn);
  });
}

function setActive(agency, btnEl) {
  activeAgency = agency;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btnEl.classList.add('active');
  render();
}

searchEl.addEventListener('input', render);
buildTabs();
render();
</script>
</body>
</html>
"""


def build_dashboard(history):
    """Writes docs/index.html — a static, GitHub-Pages-servable dashboard
    over the full history of everything the monitor has ever found."""
    by_agency = {a: [] for a in AGENCY_ORDER}
    for record in history:
        agency = record.get("agency")
        if agency in by_agency:
            by_agency[agency].append(record)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    html = (
        DASHBOARD_TEMPLATE
        .replace("__TOTAL__", str(len(history)))
        .replace("__UPDATED__", dt.date.today().isoformat())
        .replace("__DATA_JSON__", json.dumps(by_agency))
        .replace("__AGENCY_ORDER_JSON__", json.dumps(AGENCY_ORDER))
    )
    (DOCS_DIR / "index.html").write_text(html)

OWN_SITE_FETCHERS = {
    "NYCHA": fetch_nycha_own,
    "NYC DDC": fetch_ddc_own,
    "NYC EDC": fetch_edc_own,
    "NYC SCA": fetch_sca_own,
}


def main():
    state = load_state()
    new_by_agency = {}
    errors = []

    # --- City Record + Current Solicitations + PASSPort (+ own site) ---
    # PASSPort's browse page is rendered once and reused for all 5 agencies
    # rather than re-rendered 5 times.
    for agency, match_strings in CITY_RECORD_AGENCIES.items():
        cr_items, err = fetch_city_record(agency, match_strings)
        if err:
            errors.append(err)

        cs_items, cs_err = fetch_current_solicitations(agency, match_strings)
        if cs_err:
            errors.append(cs_err)

        pp_items, pp_err = fetch_passport(agency, match_strings)
        if pp_err:
            errors.append(pp_err)

        all_items = list(cr_items) + list(cs_items) + list(pp_items)
        if agency in AGENCIES_WITH_OWN_SITE:
            own_items, own_err = OWN_SITE_FETCHERS[agency]()
            if own_err:
                errors.append(own_err)
            all_items.extend(own_items)

        # Merge same-project duplicates across sources BEFORE diffing, so a
        # project that posts to the agency site first and City Record days
        # later (or vice versa) is only ever reported once.
        deduped = dedupe_items(all_items)
        new_items, updated_fp = diff_against_state(agency, deduped, state)
        new_by_agency[agency] = new_items
        state[agency] = updated_fp

    # --- DASNY (single source) ---
    dasny_items, dasny_err = fetch_dasny()
    if dasny_err:
        errors.append(dasny_err)
    new_dasny, updated_fp = diff_against_state("DASNY", dasny_items, state)
    new_by_agency["DASNY"] = new_dasny
    state["DASNY"] = updated_fp

    # --- BPCA (single source) ---
    bpca_items, bpca_err = fetch_bpca()
    if bpca_err:
        errors.append(bpca_err)
    new_bpca, updated_fp = diff_against_state("BPCA", bpca_items, state)
    new_by_agency["BPCA"] = new_bpca
    state["BPCA"] = updated_fp

    # --- Write outputs ---
    run_date = dt.date.today().isoformat()
    report_md = build_report(new_by_agency, errors, run_date)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"rfp_report_{run_date}.md"
    report_path.write_text(report_md)

    latest_path = REPORTS_DIR / "latest.md"
    latest_path.write_text(report_md)

    save_state(state)

    # Append every new item found today to the permanent history log, and
    # rebuild the dashboard from the full history (not just today's run) so
    # the site always shows everything found so far, not just the latest day.
    history = load_history()
    for agency, items in new_by_agency.items():
        for it in items:
            history.append({**it, "found_date": run_date})
    save_history(history)
    build_dashboard(history)

    print(report_md)
    print(f"\nReport written to {report_path}")

    if errors:
        print(f"\n{len(errors)} source(s) had errors — see report above.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
