"""
Offline sanity test for the DASNY and BPCA parsers.

This sandbox has no network access to dasny.org / bpca.ny.gov, so this test
feeds monitor.py's parsing logic synthetic HTML built to match the *actual*
markup structure I inspected on both live sites (verified via web_fetch
during development — see chat). It is not a substitute for running the real
script once, but it catches selector/regex breakage.
"""
import monitor
from unittest.mock import patch

DASNY_HTML = """
<html><body>
<h2><a href="/opportunities/rfps-bids/2026/test-project-one">Test Project One</a></h2>
<table>
<tr><td>Solicitation #:</td><td>3432309999-P13</td></tr>
<tr><td>Issue Date:</td><td>07/02/2026</td></tr>
<tr><td>Due Date:</td><td>07/24/2026 - 2:30 PM</td></tr>
<tr><td>Classification:</td><td>Purchasing</td></tr>
<tr><td>Type:</td><td>Bid</td></tr>
<tr><td>Status:</td><td>New</td></tr>
</table>

<h2><a href="/opportunities/rfps-bids/2026/test-project-two">Test Project Two</a></h2>
<table>
<tr><td>Solicitation #:</td><td>3751509999</td></tr>
<tr><td>Issue Date:</td><td>06/30/2026</td></tr>
<tr><td>Due Date:</td><td>08/05/2026 - 2:00 PM</td></tr>
<tr><td>Classification:</td><td>Construction Contracts</td></tr>
<tr><td>Type:</td><td>Bid</td></tr>
<tr><td>Status:</td><td>New</td></tr>
</table>
</body></html>
"""

BPCA_HTML = """
<html><body>
<div id="scope">
<h3>Current Procurement Opportunities</h3>
<a href="https://media.bpca.ny.gov/uploads/2026/04/crossing-guard-AD.pdf">Crossing Guard – AD</a>
<a href="https://media.bpca.ny.gov/uploads/2026/04/crossing-guard-RFP.pdf">Crossing Guard – RFP</a>
<a href="https://media.bpca.ny.gov/uploads/2026/04/printing-services-AD.pdf">Printing Services – AD</a>
<a href="https://media.bpca.ny.gov/uploads/2026/04/printing-services-RFP.pdf">Printing Services – RFP</a>
</div>
</body></html>
"""


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


def test_dasny_parser():
    with patch("monitor._get", return_value=FakeResponse(DASNY_HTML)):
        items, err = monitor.fetch_dasny()
    assert err is None, err
    titles = {i["title"] for i in items}
    assert titles == {"Test Project One", "Test Project Two"}, titles
    one = next(i for i in items if i["title"] == "Test Project One")
    assert one["id"] == "3432309999-P13", one["id"]
    assert one["date"] == "07/02/2026"
    print("DASNY parser OK:", items)


def test_bpca_parser():
    with patch("monitor._get", return_value=FakeResponse(BPCA_HTML)):
        items, err = monitor.fetch_bpca()
    assert err is None, err
    titles = {i["title"] for i in items}
    assert titles == {"Crossing Guard", "Printing Services"}, titles
    print("BPCA parser OK:", items)


if __name__ == "__main__":
    test_dasny_parser()
    test_bpca_parser()
    print("\nAll offline parser tests passed.")
