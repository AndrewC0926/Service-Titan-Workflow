"""Portfolio-transaction detection for the retrofit board: buildings sharing
a recording date within a short window AND close geographic proximity are
likely one transaction -- a buyer picking up several adjacent aging parcels
at once -- not independent leads that happen to coincide.

Calibrated 2026-08-17 against a real, known example (1600/1620/1630/1560/1580
S Azusa Ave, City of Industry -- the user flagged three of these five as one
transaction; the other two were found by this same check). Measured real
distances/date-gaps within that cluster: 1600/1620/1630 share an identical
geocode point (adjacent parcels snapping to one rooftop centroid) and the
same 2024-08-29 recording date; 1560 is 0.149mi away, same date; 1580 is
0.114mi from 1620, recorded 11 days later (2024-09-09). A DIFFERENT,
unrelated 2005-10-05 sale at four nearby Azusa Ave addresses (1418/1562/1478/
1420S) sits thousands of days outside the window and is correctly excluded.
PORTFOLIO_RADIUS_MILES=0.25 clears the real cluster's max internal distance
(0.149mi) with margin while staying tighter than adjacent-but-unrelated
parcels typically measured (0.6mi+ in the same sample);
PORTFOLIO_DATE_WINDOW_DAYS=14 clears the real 11-day gap with a few days'
margin. Both are judgment calls tuned to one real example, not fit against a
labeled set of confirmed/rejected portfolio transactions -- see
app/assumptions.py's "Portfolio-transaction detection" entry.

This is PROBABILISTIC evidence a group is one transaction, not proof: two
genuinely unrelated sales can coincidentally land within the same window and
radius, especially in dense industrial corridors. Every group is shown with
its member addresses so a rep can sanity-check it, not asserted as
confirmed common ownership.

Spot-checked 2026-08-19 against a random sample of 20 real groups (see
app/assumptions.py's "Portfolio-transaction detection" entry for the full
finding): 17 of 20 read as one PHYSICAL PROPERTY split across assessor
parcels, not one buyer acquiring several distinct buildings. A detected
group is still real, useful evidence of one co-recorded transaction --
it's just usually evidence of one site's parcel map, not a multi-property
acquisition. The UI reflects this (retrofit_board.html: "Co-recorded: N
parcels", not "Portfolio: N buildings") -- keep that framing if this
module's output is surfaced anywhere else.
"""
from __future__ import annotations

from sqlmodel import Session, select

from app.contractors import haversine_miles

PORTFOLIO_DATE_WINDOW_DAYS = 14
PORTFOLIO_RADIUS_MILES = 0.25

# A cap, added after running detection against the real production population
# (2026-08-17) turned up groups far larger than any real simultaneous
# purchase: the biggest was 44 buildings across an entire Glendale industrial
# park (Hazel St/Flower St/Paula Ave/Circle Seven Dr), every one sharing the
# EXACT same 1997-06-17 recording date. That shape -- dozens of parcels,
# identical date, not just within-window -- is far more consistent with a
# mass administrative event (a subdivision map recorded in one filing, or a
# bulk reassessment) than one buyer closing on 44 properties at once. 6 gives
# headroom above the confirmed real example (the 5-building Azusa Ave
# cluster) without presenting a neighborhood-wide recording event as a
# single lead. A judgment call, not fit against a labeled set of confirmed
# vs. rejected portfolios -- see app/assumptions.py's "Portfolio-transaction
# detection" entry. Groups exceeding this are excluded entirely (members
# revert to standalone rows), not truncated to the largest 6 -- truncating
# would silently misstate which specific buildings are actually grouped.
PORTFOLIO_MAX_GROUP_SIZE = 6

# First 7 of LA County's 10-digit APN (book+page; the trailing 3 are the
# individual parcel). Members sharing this prefix sit in the same assessor
# block -- almost always one physical property recorded as multiple
# parcels, not a genuine multi-property purchase. See detect_portfolios'
# same_block computation and app/models.py's RetrofitBuilding.
# portfolio_same_block docstring for the spot-check that established this.
APN_BLOCK_PREFIX_LEN = 7


def _apn_block(apn: str) -> str:
    return apn[:APN_BLOCK_PREFIX_LEN]


def detect_portfolios(buildings: list) -> dict[str, dict]:
    """buildings: any objects with .apn, .address, .sqft, .latitude,
    .longitude, .last_sale_date (only rows with BOTH a sale date and a
    geocode participate -- see the two coverage figures in
    app.pipeline.ownership:ownership_recency_coverage and
    app.pipeline.retrofit's geocode coverage for how much of the board that
    excludes).

    Returns {apn: {group_id, member_count, combined_sqft, members,
    same_block}} for every building that landed in a group of 2+ --
    standalone buildings (the overwhelming majority) simply don't appear in
    the returned dict. members lists every OTHER building in the group
    (never includes the key's own apn), each as {apn, address, sqft,
    last_sale_date}. same_block is True when every member shares the same
    APN book/page prefix (_apn_block) -- one physical property recorded as
    multiple parcels, not a genuine multi-property transaction; see
    APN_BLOCK_PREFIX_LEN's docstring.
    """
    candidates = [b for b in buildings if b.last_sale_date is not None
                 and b.latitude is not None and b.longitude is not None]
    candidates.sort(key=lambda b: b.last_sale_date)
    n = len(candidates)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    # Sorted by date, so once building j is more than the window past
    # building i, no later j can be in range either -- break, not continue.
    for i in range(n):
        for j in range(i + 1, n):
            gap_days = (candidates[j].last_sale_date - candidates[i].last_sale_date).days
            if gap_days > PORTFOLIO_DATE_WINDOW_DAYS:
                break
            dist = haversine_miles(candidates[i].latitude, candidates[i].longitude,
                                   candidates[j].latitude, candidates[j].longitude)
            if dist <= PORTFOLIO_RADIUS_MILES:
                union(i, j)

    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(candidates[i])

    out: dict[str, dict] = {}
    for members in groups.values():
        if len(members) < 2 or len(members) > PORTFOLIO_MAX_GROUP_SIZE:
            continue
        group_id = min(m.apn for m in members)
        combined_sqft = sum(m.sqft for m in members if m.sqft) or None
        same_block = len({_apn_block(m.apn) for m in members}) == 1
        for m in members:
            # ISO string, not a raw datetime -- this list is stored in a JSON
            # column, which can't serialize a Python datetime object directly.
            others = [{"apn": x.apn, "id": x.id, "address": x.address, "sqft": x.sqft,
                      "last_sale_date": x.last_sale_date.date().isoformat()}
                     for x in members if x.apn != m.apn]
            out[m.apn] = {
                "group_id": group_id, "member_count": len(members),
                "combined_sqft": combined_sqft, "members": others,
                "same_block": same_block,
            }
    return out


def apply_portfolio_grouping(session: Session, population: str) -> dict:
    """Runs detect_portfolios over every RetrofitBuilding row currently in
    `population` (called at the end of build_retrofit_buildings /
    find_replacement_candidates, right after that population's own DELETE-
    and-reinsert commit) and writes the grouping fields back. Resets every
    row's grouping to standalone first, same "full refresh" discipline
    app.pipeline.local250's signatory match uses, so a building that fell
    out of a group on this rebuild doesn't keep stale membership."""
    from app.models import RetrofitBuilding

    rows = session.exec(select(RetrofitBuilding).where(RetrofitBuilding.population == population)).all()
    groups = detect_portfolios(rows)
    grouped = 0
    # A production run against 53,252 replacement_candidate rows (2026-08-16)
    # hung indefinitely writing every row unconditionally in one giant
    # transaction, same do_sys_poll network-idle pattern app/pipeline/ownership
    # hit with a long-held session -- the overwhelming majority of rows are
    # standalone before AND after this rebuild, so only touching rows whose
    # grouping actually changes, and committing in batches instead of one
    # final commit, cuts the write volume and keeps any single transaction
    # small (see app.pipeline.ownership's fresh-session-per-batch fix for the
    # same class of problem). Batching alone then surfaced a WORSE bug: the
    # session's default expire_on_commit=True invalidates every already-
    # loaded row after each commit, so touching row.apn on a later row in
    # this same loop silently re-fetches it with its own individual SELECT
    # -- an N+1 that, at this scale, got the connection itself killed
    # server-side ("server closed the connection unexpectedly"). Turning
    # expiry off keeps every row already fetched into `rows` above valid
    # across every batch boundary below.
    session.expire_on_commit = False
    touched = 0
    for row in rows:
        info = groups.get(row.apn)
        if info:
            row.portfolio_group_id = info["group_id"]
            row.portfolio_member_count = info["member_count"]
            row.portfolio_combined_sqft = info["combined_sqft"]
            row.portfolio_members = info["members"]
            row.portfolio_same_block = info["same_block"]
            grouped += 1
            touched += 1
        elif row.portfolio_group_id is not None:
            row.portfolio_group_id = None
            row.portfolio_member_count = None
            row.portfolio_combined_sqft = None
            row.portfolio_members = []
            row.portfolio_same_block = None
            touched += 1
        if touched and touched % 2000 == 0:
            session.commit()
    session.commit()
    return {"candidates_considered": len(rows), "buildings_grouped": grouped,
            "distinct_groups": len({g["group_id"] for g in groups.values()})}


def portfolio_coverage(session: Session) -> dict:
    """Live counts for the assumptions register: how many buildings across
    the whole board (both populations) currently sit in a detected
    portfolio group. Computed at call time, never cached, same discipline
    as app.pipeline.resolve:delivery_method_coverage."""
    from sqlalchemy import func as sa_func

    from app.models import RetrofitBuilding

    grouped = session.exec(
        select(sa_func.count(RetrofitBuilding.id)).where(RetrofitBuilding.portfolio_group_id.is_not(None))
    ).one()
    distinct_groups = session.exec(
        select(sa_func.count(sa_func.distinct(RetrofitBuilding.portfolio_group_id)))
        .where(RetrofitBuilding.portfolio_group_id.is_not(None))
    ).one()
    groups_same_block = session.exec(
        select(sa_func.count(sa_func.distinct(RetrofitBuilding.portfolio_group_id)))
        .where(RetrofitBuilding.portfolio_group_id.is_not(None), RetrofitBuilding.portfolio_same_block.is_(True))
    ).one()
    groups_multi_block = session.exec(
        select(sa_func.count(sa_func.distinct(RetrofitBuilding.portfolio_group_id)))
        .where(RetrofitBuilding.portfolio_group_id.is_not(None), RetrofitBuilding.portfolio_same_block.is_(False))
    ).one()
    return {"buildings_grouped": grouped, "distinct_groups": distinct_groups,
            "groups_same_block": groups_same_block, "groups_multi_block": groups_multi_block}
