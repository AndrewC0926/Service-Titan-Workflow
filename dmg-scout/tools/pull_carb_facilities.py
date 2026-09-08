"""One-off Playwright pull of CARB's Facility Search Tool export -- not a
pytest test, not a live app/pipeline fetcher, run by hand whenever the
CARB-sourced half of scaqmd_facilities needs a refresh:

    .venv/bin/python tools/pull_carb_facilities.py

Same "hand-pulled, statically stored" precedent as the AB 869 PDF corpus
under docs/hcai/ab869/raw/ -- see app/pipeline/scaqmd.py's module
docstring for why this isn't automated inside the app itself.

WHY PLAYWRIGHT: the tool's own landing page (ww2.arb.ca.gov/facility-
search-tool) renders as a JavaScript shell with no visible form in static
HTML, but the actual search form lives in a plain HTML iframe at
www.arb.ca.gov/app/emsinv/iframe/facinfo/facinfo.php -- reachable directly,
but only by first letting the wrapper page's JS load the iframe, which a
plain HTTP client can't do. That serving host's own robots.txt is clean
(Allow: /, 2s crawl-delay, confirmed 2026-09-08), so this is a single,
by-hand, one-time trigger of the tool's own "Download this data as a
Comma Separated Value text file" export -- not a scheduled or on-demand
scrape.

SCOPE: District = "SC" (South Coast AQMD only), matching the AER XLSX's
own population exactly, Database Year defaulted to whatever the tool's
own default is at pull time (2024 as of 2026-09-08) -- not pinned in code,
since re-running this script later should pick up whatever CARB's own
current default year is, the same way a human using the tool by hand
would.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

LANDING_URL = "https://ww2.arb.ca.gov/applications/facility-search-tool"
IFRAME_URL = "https://www.arb.ca.gov/app/emsinv/iframe/facinfo/facinfo.php"
DISTRICT_VALUE = "SC"  # South Coast AQMD
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "carb"


def pull(out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(accept_downloads=True)
        page.goto(LANDING_URL, wait_until="networkidle", timeout=60000)

        # The form iframe is loaded by the wrapper page's own JS and isn't
        # always present the instant networkidle fires (observed
        # intermittently, 2026-09-08) -- poll for it rather than a single
        # fixed sleep.
        form_frame = None
        for _ in range(20):
            form_frame = page.frame(url=IFRAME_URL)
            if form_frame is not None:
                break
            time.sleep(1)
        if form_frame is None:
            raise RuntimeError(f"expected iframe {IFRAME_URL!r} not found on {LANDING_URL} after 20s -- "
                              f"CARB may have changed the tool's page structure")

        form_frame.select_option("select[name='dis_']", DISTRICT_VALUE)
        submit = form_frame.query_selector("input[type=submit]")
        if submit is None:
            raise RuntimeError("no submit button found on the CARB facility search form")
        submit.click()
        time.sleep(5)  # the form's own GET navigation inside the iframe, no JS event to await

        results_frame = page.frames[-1]
        csv_link = results_frame.query_selector("a:has-text('Comma Separated')")
        if csv_link is None:
            raise RuntimeError("no 'Download this data as a Comma Separated Value text file' link "
                              "found on the CARB results page -- tool output may have changed shape")

        # The database year is whatever the tool's own default select showed --
        # read it back rather than assuming, so the saved filename is honest.
        # Anchored to the tool's own "Database year is NNNN." sentence, not
        # just any 4-digit token in the page (the record count -- e.g.
        # "5569 records returned." -- is also a bare number and appears
        # first, which a naive first-4-digit-token scan picked up instead).
        import re as _re
        year_text = results_frame.inner_text("body")
        year_match = _re.search(r"Database year is (\d{4})", year_text)
        db_year = year_match.group(1) if year_match else "unknown-year"

        out_path = out_dir / f"{db_year}-south-coast-aqmd-facilities.csv"
        with page.expect_download(timeout=30000) as dl_info:
            csv_link.click()
        dl_info.value.save_as(str(out_path))

        browser.close()
        return out_path


if __name__ == "__main__":
    saved_to = pull()
    print(f"saved to: {saved_to}")
    sys.exit(0)
