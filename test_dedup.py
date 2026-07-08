"""
Tests the cross-source dedup behavior explicitly requested: for NYCHA/DDC/EDC,
a project may post to the agency's own site and to City Record days apart,
in either order. It should still only ever be reported as "new" once.
"""
import monitor as m


def test_same_run_merge():
    """Same project from two sources in one run -> merged into one item."""
    items = [
        {"id": "own-1", "agency": "NYCHA", "title": "RFP #515836 A/E and Local Law Inspection Services",
         "date": "2026-07-01", "url": "https://www1.nyc.gov/assets/nycha/downloads/pdf/rfp515836.pdf",
         "source": "NYCHA Procurement Opportunities"},
        {"id": "cr-99182", "agency": "NYCHA",
         "title": "A/E and Local Law Inspection Services related to LL-11, LL-126, LL-37",
         "date": "2026-07-04", "url": "https://a856-cityrecord.nyc.gov/RequestDetail/99182",
         "source": "NYC City Record Online"},
    ]
    deduped = m.dedupe_items(items)
    assert len(deduped) == 1, f"expected 1 merged item, got {len(deduped)}"
    assert set(deduped[0]["sources"]) == {"NYCHA Procurement Opportunities", "NYC City Record Online"}
    print("same_run_merge OK")


def test_staggered_across_days_not_reported_twice():
    """Project appears on source A on day 1, source B (same project, worded
    slightly differently) on day 4 -> zero new items on day 4."""
    state = {}

    day1 = [{"id": "own-1", "agency": "NYCHA", "title": "RFP #515836 A/E and Local Law Inspection Services",
             "date": "2026-07-01", "url": "https://www1.nyc.gov/assets/nycha/downloads/pdf/rfp515836.pdf",
             "source": "NYCHA Procurement Opportunities"}]
    new1, fp1 = m.diff_against_state("NYCHA", m.dedupe_items(day1), state)
    state["NYCHA"] = fp1
    assert len(new1) == 1

    day4 = day1 + [{"id": "cr-99182", "agency": "NYCHA",
                     "title": "A/E and Local Law Inspection Services related to LL-11, LL-126, LL-37",
                     "date": "2026-07-04", "url": "https://a856-cityrecord.nyc.gov/RequestDetail/99182",
                     "source": "NYC City Record Online"}]
    new4, fp4 = m.diff_against_state("NYCHA", m.dedupe_items(day4), state)
    state["NYCHA"] = fp4
    assert len(new4) == 0, f"expected 0 new (duplicate), got {len(new4)}: {[i['title'] for i in new4]}"
    print("staggered_across_days_not_reported_twice OK")


def test_genuinely_new_project_still_detected():
    """A real second, unrelated project should still show up as new."""
    state = {}
    baseline = [{"id": "cr-1", "agency": "NYCHA", "title": "A/E and Local Law Inspection Services",
                 "date": "2026-07-01", "url": "https://a856-cityrecord.nyc.gov/RequestDetail/1",
                 "source": "NYC City Record Online"}]
    _, fp = m.diff_against_state("NYCHA", m.dedupe_items(baseline), state)
    state["NYCHA"] = fp

    next_run = baseline + [{"id": "cr-2", "agency": "NYCHA",
                             "title": "Citywide Elevator Rehabilitation and Maintenance",
                             "date": "2026-07-07", "url": "https://a856-cityrecord.nyc.gov/RequestDetail/2",
                             "source": "NYC City Record Online"}]
    new_items, _ = m.diff_against_state("NYCHA", m.dedupe_items(next_run), state)
    assert len(new_items) == 1 and "Elevator" in new_items[0]["title"]
    print("genuinely_new_project_still_detected OK")


if __name__ == "__main__":
    test_same_run_merge()
    test_staggered_across_days_not_reported_twice()
    test_genuinely_new_project_still_detected()
    print("\nAll dedup tests passed.")
