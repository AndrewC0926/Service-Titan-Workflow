"""Every <table class="tbl"> in this app uses table-layout:fixed (see .tbl
in app/web/static/app.css), which sizes columns ONLY from the <colgroup>,
never from cell content. A <colgroup> that declares fewer <col> than the
table actually has leaves the extra trailing column(s) with no reserved
box at all -- their content then renders with ~zero allocated width and
visually overlaps the neighboring column instead of wrapping or
scrolling, invisible on a wide desktop window and only obvious on a
narrow viewport.

This happened for real: board.html shipped 11 <col> for 12 <th> (a Rank
column was added without a matching <col>), and "Last sig." silently
overlapped "Who to call" -- reported, and not caught by any test, twice.
This is the static-analysis guard so the next added/removed column can't
reintroduce it silently. A live-render viewport check lives alongside
this as a manual/CI browser check where one exists; this test is the
cheap, fast, no-browser-needed floor everyone gets for free.

A <table> with NO <colgroup> at all is a different, deliberately-used,
safe pattern (the browser falls back to sizing every column from the
first row's cells) -- not what this guards against, and not flagged."""
import re
from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).parent.parent / "app" / "web" / "templates"


def _tables_with_colgroups(html: str) -> list[tuple[int, int | None]]:
    """(declared_col_count, thead_th_count_or_None) per <table>...</table>
    block that has its own <colgroup> -- tables without one are skipped,
    see module docstring."""
    out = []
    for table in re.findall(r"<table\b[^>]*>.*?</table>", html, re.DOTALL):
        cg = re.search(r"<colgroup>(.*?)</colgroup>", table, re.DOTALL)
        if cg is None:
            continue
        n_cols = len(re.findall(r"<col\b", cg.group(1)))
        thead = re.search(r"<thead>(.*?)</thead>", table, re.DOTALL)
        n_th = len(re.findall(r"<th\b", thead.group(1))) if thead else None
        out.append((n_cols, n_th))
    return out


def _template_files_with_colgroups() -> list[Path]:
    return sorted(p for p in TEMPLATES_DIR.glob("*.html") if "<colgroup>" in p.read_text())


@pytest.mark.parametrize("path", _template_files_with_colgroups(), ids=lambda p: p.name)
def test_colgroup_col_count_matches_thead_th_count(path):
    for i, (n_cols, n_th) in enumerate(_tables_with_colgroups(path.read_text())):
        if n_th is None:
            continue  # a colgroup with no <thead> at all -- nothing to compare
        assert n_cols == n_th, (
            f"{path.name}, table #{i + 1}: <colgroup> declares {n_cols} <col> but <thead> has "
            f"{n_th} <th> -- table-layout:fixed will leave the uncovered trailing column(s) "
            f"with no reserved box, and their content will overlap the next column instead of "
            f"wrapping. Add/remove <col> elements until the two counts match exactly."
        )


def test_this_test_actually_found_the_real_files():
    """Guards the test itself, not the app -- if template reorganization
    ever moves these files and the glob stops finding them, this fails
    loudly instead of the parametrized test above silently collecting
    zero cases and reporting green."""
    names = {p.name for p in _template_files_with_colgroups()}
    assert {"board.html", "retrofit_board.html", "firms.html"} <= names
