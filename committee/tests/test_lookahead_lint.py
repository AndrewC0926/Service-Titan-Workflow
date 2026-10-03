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
# A PIT table by name, or a dynamic table name (an f-string hole ``{}``), which
# could be any PIT table and so must be filtered too.
PIT_READ = re.compile(rf"\b(?:FROM|JOIN)\s+(?:(?:{_TABLES})\b|\{{\}})", re.IGNORECASE)
SQL_HINT = re.compile(r"\bSELECT\b", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    snippet: str


def _literal(node: ast.expr) -> str | None:
    """Text of a string expression; f-string holes become ``{}``; ``+`` chains,
    ``"sep".join([...])`` and ``"...".format(...)`` are resolved."""
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
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        recv = _literal(node.func.value)
        if recv is not None and node.func.attr == "format":
            return recv
        if recv is not None and node.func.attr == "join" and len(node.args) == 1:
            arg = node.args[0]
            if isinstance(arg, ast.List | ast.Tuple):
                parts = [_literal(e) for e in arg.elts]
                if all(x is not None for x in parts):
                    return recv.join(x for x in parts if x is not None)
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


@dataclass
class _Built:
    """SQL text assembled across statements (``q = "..."; q += "..."``)."""

    lineno: int
    end_lineno: int
    text: str
    pieces: list[ast.expr]


def _built_strings(tree: ast.AST) -> tuple[list[_Built], set[int]]:
    """Strings built up in a variable over several statements, per scope.

    Returns the assembled strings (judged as a whole) and the ids of the literal
    pieces that went into them (not judged on their own).
    """
    built: list[_Built] = []
    scopes = [n for n in ast.walk(tree) if isinstance(n, ast.Module | ast.FunctionDef)]
    for scope in scopes:
        acc: dict[str, _Built] = {}
        body = scope.body
        stmts: list[ast.stmt] = []

        def collect(nodes: list[ast.stmt]) -> None:
            for n in nodes:
                if isinstance(n, ast.FunctionDef | ast.ClassDef | ast.AsyncFunctionDef):
                    continue
                stmts.append(n)
                for field_name in ("body", "orelse", "finalbody"):
                    collect(getattr(n, field_name, []) or [])

        collect(body)
        for st in stmts:
            end = st.end_lineno or st.lineno
            if isinstance(st, ast.Assign) and len(st.targets) == 1:
                tgt = st.targets[0]
                if not isinstance(tgt, ast.Name):
                    continue
                v = st.value
                if (  # q = q + "..."
                    isinstance(v, ast.BinOp)
                    and isinstance(v.op, ast.Add)
                    and isinstance(v.left, ast.Name)
                    and v.left.id in acc
                ):
                    tail = _literal(v.right)
                    b = acc[v.left.id]
                    b.text += tail if tail is not None else "{}"
                    b.end_lineno = end
                    b.pieces.append(v.right)
                    acc[tgt.id] = b
                    continue
                text = _literal(v)
                if text is not None:
                    acc[tgt.id] = _Built(st.lineno, end, text, [v])
                    built.append(acc[tgt.id])
                else:
                    acc.pop(tgt.id, None)
            elif (
                isinstance(st, ast.AugAssign)
                and isinstance(st.op, ast.Add)
                and isinstance(st.target, ast.Name)
                and st.target.id in acc
            ):
                tail = _literal(st.value)
                b = acc[st.target.id]
                b.text += tail if tail is not None else "{}"
                b.end_lineno = end
                b.pieces.append(st.value)
    multi = [b for b in built if len(b.pieces) > 1]
    return multi, {id(x) for b in multi for x in b.pieces}


_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _unfiltered(text: str) -> bool:
    """True when the SQL reads more PIT tables than it applies as_of() to."""
    sql = _SQL_COMMENT.sub(" ", text)
    if not SQL_HINT.search(sql):
        return False
    reads = len(PIT_READ.findall(sql))
    return reads > 0 and sql.count("as_of(") < reads


def lint_source(source: str, path: str = "<string>") -> list[Violation]:
    lines = source.splitlines()
    tree = ast.parse(source)
    found: dict[int, Violation] = {}
    multi, pieces = _built_strings(tree)
    spans: list[tuple[int, int, str]] = [
        (n.lineno, n.end_lineno or n.lineno, t)
        for n, t in _string_exprs(tree)
        if id(n) not in pieces
    ]
    spans += [(b.lineno, b.end_lineno, b.text) for b in multi]
    for start, end, text in spans:
        if not _unfiltered(text):
            continue
        if any(PRAGMA.search(ln) for ln in lines[start - 1 : end]):
            continue
        if start not in found:
            found[start] = Violation(path, start, " ".join(text.split())[:120])
    return [found[k] for k in sorted(found)]


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


def test_lint_catches_sql_built_across_statements() -> None:
    """REVIEW R-11: SQL assembled over several statements, joins and comments."""
    bad = """
def f(pit, t, cols):
    q = "SELECT close "
    q += "FROM prices_daily WHERE security_id = $s"
    pit.query(q, t)


def g(pit, t):
    sql = "SELECT * "
    sql = sql + f"FROM fundamentals WHERE metric = '{t}'"
    return sql


def h(pit, t):
    pit.query(" ".join(["SELECT value", "FROM macro_series"]), t)


def i(pit, t):
    pit.query("SELECT * FROM news -- as_of(known_time, $asof) is in a comment", t)


def j(pit, t):
    pit.query(
        "SELECT * FROM signals s JOIN insider_txns i ON s.security_id = i.security_id "
        "WHERE as_of(s.known_time, $asof)",
        t,
    )


def k(pit, t):
    pit.query("SELECT {} FROM filings".format("form"), t)


def m(pit, table, t):
    pit.query(f"SELECT * FROM {table} WHERE TRUE", t)  # dynamic table, no as_of
"""
    lines = [x.line for x in lint_source(bad)]
    assert lines == [3, 9, 15, 19, 24, 31, 35], lines


def test_lint_accepts_sql_built_correctly() -> None:
    ok = """
def f(pit, t):
    q = "SELECT close FROM prices_daily "
    q += "WHERE as_of(known_time, $asof)"
    pit.query(q, t)


def g(pit, t):
    pit.query(
        "SELECT * FROM signals s JOIN insider_txns i ON s.security_id = i.security_id "
        "WHERE as_of(s.known_time, $asof) AND as_of(i.known_time, $asof)",
        t,
    )
"""
    assert lint_source(ok) == []


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
