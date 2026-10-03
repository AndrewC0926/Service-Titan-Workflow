"""Look-ahead lint (DESIGN 5): every SQL literal in src/ that reads a PIT table must
apply the as_of() macro. ``PIT.latest`` applies it internally and needs no SQL at the
call site. Raw-zone/admin queries may opt out with ``# lookahead-ok: <reason>`` on
the literal's line(s).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from committee.data.lake import PIT_TABLES

SRC = Path(__file__).resolve().parents[1] / "src" / "committee"
PRAGMA = re.compile(r"#\s*lookahead-ok:\s*\S")
_TABLES = "|".join(sorted(PIT_TABLES, key=len, reverse=True))
PIT_READ = re.compile(rf"\b(?:FROM|JOIN)\s+(?:{_TABLES})\b", re.IGNORECASE)
SQL_HINT = re.compile(r"\bSELECT\b", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    snippet: str


def _literal(node: ast.expr) -> str | None:
    """Text of a string expression; f-string holes become ``{}``; ``+`` chains are joined."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _string_exprs(tree: ast.AST) -> list[tuple[ast.expr, str]]:
    out: list[tuple[ast.expr, str]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.expr):
            text = _literal(node)
            if text is not None:
                out.append((node, text))
                return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return out


def lint_source(source: str, path: str = "<string>") -> list[Violation]:
    lines = source.splitlines()
    found = []
    for node, text in _string_exprs(ast.parse(source)):
        if not (SQL_HINT.search(text) and PIT_READ.search(text)) or "as_of(" in text:
            continue
        span = lines[node.lineno - 1 : (node.end_lineno or node.lineno)]
        if any(PRAGMA.search(ln) for ln in span):
            continue
        found.append(Violation(path, node.lineno, " ".join(text.split())[:120]))
    return found


def lint_tree(root: Path = SRC) -> list[Violation]:
    out = []
    for p in sorted(root.rglob("*.py")):
        out += lint_source(p.read_text(), str(p.relative_to(root.parent)))
    return out


def test_src_has_no_lookahead_queries() -> None:
    violations = lint_tree()
    assert not violations, "PIT queries without as_of():\n" + "\n".join(
        f"  {v.path}:{v.line}: {v.snippet}" for v in violations
    )


def test_lint_catches_bad_snippets() -> None:
    bad = """
def f(pit, con):
    con.execute("SELECT close FROM prices_daily WHERE security_id = $s")
    q = f"SELECT * FROM fundamentals f JOIN {other} o ON TRUE"
    q2 = "SELECT a.* FROM x a " + "JOIN insider_txns b ON a.id = b.id"
    q3 = (
        "SELECT value "
        "FROM macro_series"
    )
"""
    v = lint_source(bad)
    assert [x.line for x in v] == [3, 4, 5, 7]


def test_lint_accepts_asof_pragma_and_non_pit() -> None:
    ok = """
def f(pit, con, t):
    pit.query("SELECT close FROM prices_daily WHERE as_of(known_time, $asof)", t)
    pit.query(f"SELECT * FROM {t} WHERE as_of(known_time, $asof)", t)
    pit.latest("filings", t, where="form = '4'")
    con.execute("SELECT * FROM read_parquet(?)")
    con.execute("SELECT count(*) FROM news")  # lookahead-ok: admin row count
    doc = "rows come FROM filings via the as-of view"
    other = "SELECT * FROM prices_daily_staging"
"""
    assert lint_source(ok) == []
