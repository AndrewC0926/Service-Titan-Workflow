# Changelog

## 2026-09-06

### Fixed: score/window ratchet on off-cycle fact writes

`Project.score`/`Project.window` were only ever recomputed by the nightly
`scout pipeline` run's `score` stage. Three write paths that change a
project's underlying facts (stage, MW, developer, delivery method) outside
that nightly run left the OLD score/window in place until the next cycle —
permanently, if the fact change ever needed the forward-only stage/MW rule
to move backward (it can't; see the RATCHET BUG diagnosis this session).

- `apply_review_decision()` (the `/review/{id}/{decision}` merge/reject
  queue action)
- `run_dc_news_enrichment()` (`scout fetch-dc-news-enrichment`)
- `merge_projects()` / `merge_duplicate_groups()` (`scout merge-duplicates`)

now each call a scoped `run_size_score(session, cfg, only_project_ids=...)`
for just the project(s) they touched, in the same transaction as the fact
write, matching the pattern `/add-signal` already used.

**Note for anyone watching the board:** a review-queue merge or reject can
now move a project's score and PRE_BOD/IN_BOD/POST_BOD window immediately
instead of the next morning. If a project's rank changes right after you
work the review queue, this is why — the board is now showing the correct,
current score rather than a stale one.

### Fixed: score-breakdown transparency used the wrong midpoint

`project_score_breakdown()` (the "why is my score X" detail-page feature)
computed its tons midpoint as `(low + high) / 2`. `run_size_score()` — the
function that actually writes `Project.score` — uses `TonsEstimate.midpoint`,
which is deliberately the **geometric** mean (see that property's own
docstring: arithmetic is the wrong centre for a band spanning orders of
magnitude). The two could silently disagree for any project with a wide
tons band. `project_score_breakdown()` now reads the midpoint through
`TonsEstimate` itself instead of reimplementing the formula under the same
name.

### Incident: accidental production write during read-only verification

While verifying the fix above against production, a "read-only simulation"
of `apply_review_decision(candidate #88)` was wrapped in
`session.begin()` / `session.rollback()`. That doesn't work once
`apply_review_decision` calls `run_size_score()`, which **commits
internally** — the outer rollback had nothing left to undo, and the merge
was applied for real.

**What was written** (2026-09-06, ~16:54 UTC): `projects` id 708 —
`stage` permitting→construction, `window` IN_BOD→POST_BOD, `score`
0.2423→0.0693, `last_signal_at` 2026-06-03→2026-08-17; `match_candidates`
id 88 — `status` pending→merged, `resolved_at` set; a new `project_signals`
row (id 1389, signal 602→project 708) and a new `stage_observations` row
(id 498). `developer_aliases` was unaffected (the alias `_learn_alias()`
would have written already existed).

**Revert** (same day, ~17:10 UTC): all four rows restored to their exact
pre-merge values in one transaction; read back and confirmed identical to
the values captured earlier in the same session. No other rows were
touched.

**New rule:** verification of any write path that mutates Project/Signal
facts (`apply_review_decision`, `merge_projects`, `run_dc_news_enrichment`,
and any future import that does the same) runs against a local Postgres
restored from a production dump — never against production. Enforced
mechanically, not just by policy: those three functions now refuse to run
when `SCOUT_VERIFYING_AGAINST_PROD` is set in the environment (see
`app/runguard.py`'s `refuse_if_verifying_against_prod`). See the
assumptions register ("Engineering safety rules") for the full account.
