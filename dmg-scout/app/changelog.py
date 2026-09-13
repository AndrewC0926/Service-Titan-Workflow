"""Block 4C Item 3 (Master Plan v3.6 section 35): "A changelog users can
read." docs/CHANGELOG.md and the /changelog page (app/web/main.py) are
both rendered from BLOCKS below -- one structured source of truth, two
renderings, so a plain-language sentence here can never drift out of
sync with what the repo's own history actually says.

Each block's commit list is generated, not hand-typed: commits_for_block()
runs a real `git log --grep` against this repo's own history every time
this module is imported, so `scout changelog` always reflects the actual
commit log, not a snapshot that can go stale. `summary` is the one part
that cannot be generated -- a plain-language, "why this matters to a rep"
rendition of what the block actually shipped, written here after reading
every one of that block's own commit messages (and, for Blocks 1-2, which
predate this repo's per-feature commit convention and exist only as
research findings in docs/BUILD-PLAN.md, that document instead).

Backfilled through Block 4B per Block 4C Item 3's own instruction. Block
4C itself (this block) is deliberately not listed yet -- it is still in
progress in the same session that adds this module."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _repo_root() -> Path:
    """The actual git root (one level up from this checkout -- dmg-scout/
    is a subdirectory of a repo that also hosts an unrelated compliance
    project on `main`; see RUNBOOK.md's "Deploy branch" section). `git
    log` still only ever sees THIS branch's own ancestry, so nothing from
    that unrelated project can leak in regardless."""
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).resolve().parent,
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def commits_for_block(grep_pattern: str | None) -> list[dict]:
    """[{"hash": short sha, "date": YYYY-MM-DD, "subject": ...}, ...],
    oldest first -- generated from real git log, never a hand-maintained
    list. None (Blocks 1-2, which predate this convention) returns [].

    Matches grep_pattern against the SUBJECT LINE only, in Python -- NOT
    via `git log --grep`, whose `^` anchors to the start of any line
    ANYWHERE in the full commit message (subject and body), not just the
    subject. Confirmed the hard way building this: `--grep=^Block 4A`
    pulled in a Block 4B-prep commit whose BODY happened to contain the
    line "Block 4A Item 1's pen_state gating..." (an internal cross-
    reference, not that commit's own block), duplicating it under both
    Block 4A and Block 4B in the very first generated changelog."""
    import re

    if grep_pattern is None:
        return []
    out = subprocess.run(
        ["git", "log", "--format=%h|%ad|%s", "--date=short", "--reverse"],
        cwd=_repo_root(), capture_output=True, text=True, check=True,
    )
    pattern = re.compile(grep_pattern)
    commits = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        sha, date, subject = line.split("|", 2)
        if pattern.match(subject):
            commits.append({"hash": sha, "date": date, "subject": subject})
    return commits


# Every entry's `summary` is written for a rep, not an engineer: what
# changed, and why it's worth ten seconds of their attention. The
# technical detail lives in the commits themselves (linked by hash below)
# for anyone who wants it.
BLOCK_DEFS = [
    {
        "id": "1", "grep": None,
        "title": "Block 1 — how projects get their delivery method",
        "date": "2026-09-11",
        "summary": (
            "Research, not a shipped feature: checked whether Scout could reliably tell design-bid-build "
            "projects from design-build ones, and whether the mechanical engineer's name ever shows up "
            "early enough in a project's life to be worth watching for automatically. Verdict on the "
            "second question was no -- MEP is named at the team-announcement stage in only 1 of 8 real "
            "projects checked, and that one was a retrospective writeup, not a live announcement. Rather "
            "than build a watcher that would mostly return nothing, that idea was shelved. This is also "
            "where the fields Scout now shows for delivery method (design-bid-build, design-build, CMAR, "
            "P3, and so on) and who holds the pen on a project were first built."
        ),
    },
    {
        "id": "2", "grep": None,
        "title": "Block 2 — delivery method decided for real, and a second look at the MEP watcher",
        "date": "2026-09-12",
        "summary": (
            "Made the call Block 1 flagged: Scout's rules for deciding who to call on a project (app.call_target) "
            "were quietly still reading an old, unreliable delivery-method field pulled from entitlement "
            "filings -- exactly the kind of guess Scout is built to avoid. Switched every rule over to the "
            "new, honestly-ABSTAIN-by-default field, and locked that switch in place with a test that fails "
            "if anything ever reads the old field again. Checked six more real projects for the MEP-naming "
            "question from Block 1; the answer held -- still a no-go on an automated MEP watcher at this stage "
            "of a project's life."
        ),
    },
    {
        "id": "3", "grep": r"^Block 3",
        "title": "Block 3 — Scout's first real Pipeline: Opportunities, Reason Blocks, and Deadlines",
        "date": "2026-09-11",
        "summary": (
            "The first version of the screens reps actually use. Built the idea of an \"Opportunity\" (a "
            "Signal worth working) and its \"Reason Block\" -- the three whys Scout must be able to answer "
            "before it asks anyone to make a call: who do we know there, why now, and why would we win. Any "
            "one of those three can honestly say \"don't know\" (ABSTAIN) rather than a guess dressed up as an "
            "answer. Built the four-part filter that decides whether a Signal is even worth turning into an "
            "Opportunity (a real contact, a real account or building, a dated reason, and a line DMG can "
            "actually fit), plus the first Pipeline, Signals, and Deadlines pages and Scout's whole "
            "look-and-feel. Also surfaced an important, honest finding rather than hiding it: at the end of "
            "this block, not one real Signal in the system could pass all four parts of the filter at once -- "
            "flagged as a real design gap for the next block to fix, not silently patched around."
        ),
    },
    {
        "id": "4A", "grep": r"^Block 4A",
        "title": "Block 4A — Outcomes, Decision Notes, and the numbers that remember themselves",
        "date": "2026-09-12",
        "summary": (
            "Gave every Opportunity a real anchor (an Account, a Building, or a Deadline) so Pipeline rows "
            "stopped floating with nothing solid behind them. Added one-tap Outcomes (connected, no answer, "
            "won, lost, and so on) so logging what happened on a call takes one click, not a form. Added "
            "Decision Notes -- a thirty-second, voice-friendly way for anyone to record why a deal moved, "
            "who's the pen holder, which competitor is in it -- so that knowledge survives even when nobody "
            "opens Scout that day. And started the metric_snapshot table: a permanent, append-only daily "
            "record of the numbers that matter (signal counts, pipeline counts, how strong the whys are), so "
            "next quarter's report can show a real trend instead of a guess about what things looked like "
            "before anyone thought to check."
        ),
    },
    {
        "id": "4B", "grep": r"^Block 4B",
        "title": "Block 4B — Reports, the Weekly Brief, the design system, and roles",
        "date": "2026-09-12",
        "summary": (
            "One shared look for every page, print-out, and PDF, with a KPI tile, a server-drawn chart, and "
            "one honest rule: gray and \"unknown, not guessed\" for anything Scout can't back up, never a "
            "fabricated number. Built the real Reports page (funnel, why-strength, deadline exposure, data "
            "health -- all read from the permanent snapshot table Block 4A started, never recomputed live) "
            "and the Weekly Sales Intelligence Brief: one page a manager can read in two minutes, archived "
            "every week, exportable to PDF and Excel. Rewrote the call-script language for the handoff play -- "
            "when a contractor's own yard is the anchor, the ask is now the whole list of nearby buildings "
            "past service life, not just the one permit that surfaced them. Added real roles (rep, inside "
            "sales, manager, executive, operator) so an executive lands on Reports, a rep lands on Today, and "
            "a manager can see the whole team's patterns while a rep still only sees their own. Also brought "
            "this work onto real company data for the first time and set up NetSuite contact matching, so the "
            "reachable-contact and contractor-anchored Opportunities in Pipeline today are the first ones "
            "built from Scout's own customer records rather than public filings alone."
        ),
    },
]


def generate_changelog() -> list[dict]:
    """Every block above, with its commits resolved fresh from git log --
    the one function both docs/CHANGELOG.md (via `scout changelog`) and
    the /changelog page render from, so they can never disagree."""
    blocks = []
    for b in BLOCK_DEFS:
        blocks.append({**b, "commits": commits_for_block(b["grep"])})
    return blocks


def render_markdown(blocks: list[dict] | None = None) -> str:
    blocks = blocks if blocks is not None else generate_changelog()
    lines = [
        "# Changelog",
        "",
        "Generated by `scout changelog` (app/changelog.py) from this repo's own commit history --",
        "never hand-edited. Plain-language summaries are written for a rep, not an engineer; every",
        "commit hash below links to the real technical change for anyone who wants it.",
        "",
    ]
    for b in blocks:
        lines.append(f"## {b['title']}")
        lines.append(f"*{b['date']}*")
        lines.append("")
        lines.append(b["summary"])
        lines.append("")
        if b["commits"]:
            lines.append("<details><summary>Commits</summary>")
            lines.append("")
            for c in b["commits"]:
                lines.append(f"- `{c['hash']}` {c['date']} — {c['subject']}")
            lines.append("")
            lines.append("</details>")
        else:
            lines.append("*Research and verification work -- no shippable commits; see docs/BUILD-PLAN.md.*")
        lines.append("")
    return "\n".join(lines)
