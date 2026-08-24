"""CSLB contractor <-> retrofit-candidate geographic join. See
app/models.py's Contractor/RetrofitGeocode docstrings for sourcing and
app/pipeline/cslb.py's module docstring for the CSLB compliance check.

Distance is real haversine miles between geocoded points (app.geocode),
not county adjacency or ZIP matching -- both sides carry real lat/long
because the volumes here (tens of thousands on each side) make a live
per-request N x M join impractical, so every query below bounding-box
prefilters in SQL (indexed lat/long range) before the exact haversine
check in Python, and the contractor-ranking direction is precomputed by
match_contractors rather than computed on page load -- see Contractor's
nearby_replacement_candidates field docstring.

"Licensed" here means exactly what CSLB's own PrimaryStatus/SecondaryStatus
fields say, shown as-is -- this module does not decide who counts as
licensed-enough to list; a suspended contractor still shows up if they are
geographically nearest, with their real status attached, not silently
filtered out by a judgment call this project was told not to make.
"""
from __future__ import annotations

import math

from sqlalchemy import func, text
from sqlmodel import Session, select

from rapidfuzz import fuzz

from app.config import Config
from app.models import Contractor, RetrofitBuilding, utcnow
from app.pipeline.local250 import (
    MIN_NAME_LEN,
    NAME_ONLY_THRESHOLD,
    NAME_WITH_CITY_THRESHOLD,
    clean_contractor_name,
)

# The two CSLB classifications that are actually mechanical (HVAC /
# refrigeration) -- see app.pipeline.cslb.CLASSIFICATIONS for the full
# C-20/C-38/B storage scope. "Nearest licensed MECHANICAL contractors" on
# a retrofit building page narrows to these two; the broader /contractors
# ranked view is not narrowed, since a B (general building) contractor is
# a real bidder on an equipment-replacement job too.
MECHANICAL_CLASSIFICATIONS = frozenset({"C20", "C38"})

MILES_PER_DEGREE_LAT = 69.0

# Same cap app.pipeline.retrofit:rank_buildings' magnitude_factor uses on
# service_life_years_past, and for the same reason: a handful of rows carry
# a 150-300yr figure (almost certainly an assessor YearBuilt data error,
# e.g. "1806"), and without a cap those few would dominate a contractor's
# whole aggregate urgency score by themselves. Kept as the same constant so
# a future change to rank_buildings' cap doesn't silently drift out of sync
# with this one.
URGENCY_YEARS_PAST_CAP = 100.0


def _urgency_weight(building: RetrofitBuilding) -> float:
    """This building's contribution to a nearby contractor's aggregate
    nearby_urgency_score -- service_life_years_past, clipped to
    [0, URGENCY_YEARS_PAST_CAP]. The same gradient rank_buildings ranks
    individual buildings on (see RetrofitBuilding.service_life_years_past's
    docstring), not a second, driftable definition of "urgent" invented at
    the contractor-aggregation layer. None (never computed) contributes 0,
    same as a building at parity with "just barely due"."""
    return max(0.0, min(URGENCY_YEARS_PAST_CAP, building.service_life_years_past or 0.0))


def _tons_mid(building: RetrofitBuilding) -> float:
    """This building's contribution to a nearby contractor's aggregate
    nearby_estimated_tons -- the midpoint of estimated_tons_low/high, 0 if
    either bound is missing. A tie-breaker only (see Contractor's docstring
    for why this must never be allowed to outrank urgency), so an
    under-estimate here costs nothing beyond ordering two equally-urgent
    contractors against each other."""
    if building.estimated_tons_low is None or building.estimated_tons_high is None:
        return 0.0
    return (building.estimated_tons_low + building.estimated_tons_high) / 2.0


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_miles = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r_miles * math.asin(min(1.0, math.sqrt(a)))


def _bounding_box(lat: float, lon: float, radius_miles: float) -> tuple[float, float, float, float]:
    """(lat_min, lat_max, lon_min, lon_max) -- a cheap SQL prefilter, always
    a superset of the true radius (a square containing the circle), never a
    subset -- the exact haversine check afterward is what enforces the real
    boundary, this just keeps the DB from scanning every geocoded row."""
    lat_delta = radius_miles / MILES_PER_DEGREE_LAT
    lon_delta = radius_miles / (MILES_PER_DEGREE_LAT * max(math.cos(math.radians(lat)), 0.01))
    return lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta


def default_radius_miles(cfg: Config) -> float:
    """15mi -- the per-building "nearest mechanical contractors" list (a
    single building's detail page, app.web.main:retrofit_building_detail).
    Deliberately wide: a real dispatch list should show what's realistically
    reachable, and this is the radius contractors themselves see on their
    own CSLB service area, not a ranking metric anyone needs to discriminate
    within. NOT the radius match_contractors ranks on -- see
    ranking_radius_miles below for why those two questions need different
    answers."""
    return cfg.get("contractors.default_radius_miles", 15)


def ranking_radius_miles(cfg: Config) -> float:
    """3mi -- match_contractors' own default when no radius is given
    explicitly, distinct from default_radius_miles above. Measured
    2026-08-15: at 15mi (the per-building default), a contractor's nearby
    building set overlaps almost entirely with every other contractor's in
    the same dense metro pocket -- sum-based aggregation over nearly-
    identical inputs converges to nearly the same total regardless of what
    per-building weight is used, which is why the FIRST attempt at urgency-
    weighted ranking still spread its top 10 by only 1.4%, same as raw
    count. Tightening to 3mi (still ~135-380 buildings per top contractor,
    not a thin/noisy sample) restored real spread: 18-29% across several
    radii tested, vs 1.4-10.8% at 5mi+. This is the fix "consider a tighter
    radius" in the ranking problem actually needed -- reweighting alone,
    at the old radius, could not have discriminated no matter what it
    weighted by."""
    return cfg.get("contractors.ranking_radius_miles", 3)


def nearby_replacement_candidates(session: Session, contractor: Contractor,
                                  radius_miles: float) -> list[RetrofitBuilding]:
    """Replacement-candidate RetrofitBuilding rows within radius_miles of
    this contractor, ranked by service-life urgency (rank_score, descending
    -- see RetrofitBuilding.rank_score's own docstring for what that number
    is). Empty if the contractor has never been geocoded -- never an
    error, and never silently treated as zero real candidates found."""
    if contractor.latitude is None or contractor.longitude is None:
        return []
    lat_min, lat_max, lon_min, lon_max = _bounding_box(contractor.latitude, contractor.longitude, radius_miles)
    boxed = session.exec(
        select(RetrofitBuilding).where(
            RetrofitBuilding.population == "replacement_candidate",
            RetrofitBuilding.latitude.is_not(None),
            RetrofitBuilding.latitude.between(lat_min, lat_max),
            RetrofitBuilding.longitude.between(lon_min, lon_max),
        )
    ).all()
    in_radius = [
        b for b in boxed
        if haversine_miles(contractor.latitude, contractor.longitude, b.latitude, b.longitude) <= radius_miles
    ]
    in_radius.sort(key=lambda b: (b.rank_score is not None, b.rank_score or 0.0), reverse=True)
    return in_radius


# One UPDATE ... FROM per batch: a bounding-box prefilter (BETWEEN, the
# cheap superset square -- see _bounding_box's docstring) joined against the
# exact haversine formula, aggregated per contractor, entirely inside
# Postgres. GREATEST/RADIANS/ASIN/LEAST/POWER/SQRT mirror _bounding_box and
# haversine_miles above field-for-field -- if either of those changes, this
# SQL must change with it. urgency/tons_mid mirror _urgency_weight/_tons_mid
# below field-for-field, for the same reason -- see those functions'
# docstrings for what each term means and why urgency (not raw count) is
# the ranking field.
_MATCH_CONTRACTORS_BATCH_SQL = text("""
    WITH candidates AS (
        SELECT id, latitude, longitude,
               LEAST(GREATEST(COALESCE(service_life_years_past, 0), 0), 100) AS urgency,
               COALESCE((estimated_tons_low + estimated_tons_high) / 2.0, 0) AS tons_mid
        FROM retrofit_buildings
        WHERE population = 'replacement_candidate'
          AND latitude IS NOT NULL
          AND longitude IS NOT NULL
    ),
    agg AS (
        SELECT c.id AS contractor_id,
               COUNT(b.id) AS cnt,
               COALESCE(SUM(b.urgency), 0) AS urgency_score,
               COALESCE(SUM(b.tons_mid), 0) AS tons_score
        FROM contractors c
        LEFT JOIN candidates b
          ON b.latitude  BETWEEN c.latitude  - (:radius / 69.0)
                              AND c.latitude  + (:radius / 69.0)
         AND b.longitude BETWEEN c.longitude - (:radius / (69.0 * GREATEST(COS(RADIANS(c.latitude)), 0.01)))
                              AND c.longitude + (:radius / (69.0 * GREATEST(COS(RADIANS(c.latitude)), 0.01)))
         AND 2 * 3958.8 * ASIN(LEAST(1.0, SQRT(
                POWER(SIN(RADIANS(b.latitude - c.latitude) / 2), 2)
                + COS(RADIANS(c.latitude)) * COS(RADIANS(b.latitude))
                  * POWER(SIN(RADIANS(b.longitude - c.longitude) / 2), 2)
             ))) <= :radius
        WHERE c.latitude IS NOT NULL
          AND c.longitude IS NOT NULL
          AND c.id BETWEEN :lo AND :hi
        GROUP BY c.id
    )
    UPDATE contractors AS c
    SET nearby_replacement_candidates = agg.cnt,
        nearby_urgency_score = agg.urgency_score,
        nearby_estimated_tons = agg.tons_score,
        nearby_radius_miles = :radius,
        nearby_computed_at = :now
    FROM agg
    WHERE c.id = agg.contractor_id
""")


def _match_contractors_sql(session: Session, radius_miles: float, now, *, batch_size: int) -> dict:
    """Postgres-only fast path -- see _MATCH_CONTRACTORS_BATCH_SQL. Batched
    by contractor id range (not per-contractor) so a run against the full
    ~47k-contractor table is a handful of round trips, each its own
    committed transaction, rather than one all-or-nothing transaction
    holding locks for the entire run."""
    skipped = session.exec(
        select(func.count()).select_from(Contractor).where(Contractor.latitude.is_(None))
    ).one()
    geocoded_ids = session.exec(
        select(Contractor.id).where(Contractor.latitude.is_not(None)).order_by(Contractor.id)
    ).all()

    matched = 0
    for i in range(0, len(geocoded_ids), batch_size):
        batch = geocoded_ids[i:i + batch_size]
        result = session.execute(_MATCH_CONTRACTORS_BATCH_SQL,
                                 {"radius": radius_miles, "lo": batch[0], "hi": batch[-1], "now": now})
        matched += result.rowcount
        session.commit()

    return {"contractors_matched": matched, "contractors_skipped_ungeocoded": skipped}


def _match_contractors_python(session: Session, radius_miles: float, now) -> dict:
    """Row-by-row fallback for dialects the batch SQL above doesn't target
    (SQLite, under test -- see conftest.py). Never runs against production,
    which is Postgres-only; kept only so the behavior this module promises
    stays covered by the test suite without a live Postgres instance."""
    stats = {"contractors_matched": 0, "contractors_skipped_ungeocoded": 0}
    for contractor in session.exec(select(Contractor)).all():
        if contractor.latitude is None:
            stats["contractors_skipped_ungeocoded"] += 1
            continue
        nearby = nearby_replacement_candidates(session, contractor, radius_miles)
        contractor.nearby_replacement_candidates = len(nearby)
        contractor.nearby_urgency_score = sum(_urgency_weight(b) for b in nearby)
        contractor.nearby_estimated_tons = sum(_tons_mid(b) for b in nearby)
        contractor.nearby_radius_miles = radius_miles
        contractor.nearby_computed_at = now
        session.add(contractor)
        stats["contractors_matched"] += 1
    session.commit()
    return stats


def match_contractors(session: Session, cfg: Config, *, radius_miles: float | None = None,
                      batch_size: int = 5000) -> dict:
    """Precomputes Contractor.nearby_replacement_candidates for every
    geocoded contractor -- see that field's docstring for why this is
    cached rather than computed per page view. Idempotent: re-running with
    the same radius just refreshes the count; a changed radius recomputes
    every contractor against the new value.

    On Postgres (production) this is _MATCH_CONTRACTORS_BATCH_SQL run in
    id-range batches, not a per-contractor Python loop: the earlier
    implementation issued one ORM query per contractor -- ~42k round trips
    to a remote Postgres instance, measured at ~2 hours for the full table.
    The SQL version does the bounding-box prefilter, haversine distance and
    per-contractor aggregation inside Postgres itself, batched only to keep
    any single transaction from holding locks/undo for the whole run."""
    radius_miles = radius_miles if radius_miles is not None else ranking_radius_miles(cfg)
    now = utcnow()

    if session.get_bind().dialect.name != "postgresql":
        return _match_contractors_python(session, radius_miles, now)
    return _match_contractors_sql(session, radius_miles, now, batch_size=batch_size)


def _is_mechanical_classification(classifications: str | None) -> bool:
    raw = (classifications or "").upper()
    tokens = {t.replace("-", "") for t in raw.replace(",", " ").split()}
    return bool(tokens & MECHANICAL_CLASSIFICATIONS)


def nearest_mechanical_contractors(session: Session, building: RetrofitBuilding, *,
                                   radius_miles: float, limit: int = 10) -> list[dict]:
    """Nearest C-20/C-38 (mechanical) contractors to this building, within
    radius_miles, nearest first. Returns [{contractor, distance_miles}, ...]
    -- distance computed fresh here (not cached, unlike the contractor ->
    count direction), since a single building page is a bounded, cheap
    query, not an all-contractors sweep."""
    if building.latitude is None or building.longitude is None:
        return []
    lat_min, lat_max, lon_min, lon_max = _bounding_box(building.latitude, building.longitude, radius_miles)
    boxed = session.exec(
        select(Contractor).where(
            Contractor.latitude.is_not(None),
            Contractor.latitude.between(lat_min, lat_max),
            Contractor.longitude.between(lon_min, lon_max),
        )
    ).all()

    results = []
    for c in boxed:
        if not _is_mechanical_classification(c.classifications):
            continue
        dist = haversine_miles(building.latitude, building.longitude, c.latitude, c.longitude)
        if dist <= radius_miles:
            results.append({"contractor": c, "distance_miles": round(dist, 1)})
    results.sort(key=lambda r: r["distance_miles"])
    return results[:limit]


# A SQL port of the double loop below (same bounding-box-plus-haversine
# shape as _MATCH_CONTRACTORS_BATCH_SQL) was tried and abandoned
# 2026-08-18. match_contractors' move to SQL wins because it eliminates
# ~42k per-contractor ROUND TRIPS -- an I/O problem. This function already
# issues exactly one round trip (the candidate fetch below); the double
# loop that follows is pure in-process compute, so pushing it into SQL
# only trades Python's O(1)-per-building running-best comparison for a
# real per-building sort. At LA's density a 15mi box is only weakly
# selective -- confirmed via EXPLAIN ANALYZE against a real 193-building/
# 3,505-candidate production sample, each building's own box matches
# ~900-1,100 of the 3,505 mechanical candidates, not a handful -- so three
# SQL formulations were all substantially slower than the Python loop's
# measured 0.535s: a window function over two unnest() arrays (~14.8s, no
# index available over a literal array at all), the same window function
# joined against the real, indexed `contractors` table instead (~10.1s,
# now index-assisted but still materializing and sorting ~175k candidate
# rows before picking rank 1), and a LATERAL join with ORDER BY/LIMIT 1
# per building -- the correct "nearest per group" idiom, avoiding the big
# sort -- still ~6.0s, dominated by evaluating the haversine expression
# and an `id = ANY(3,505-element array)` filter across ~1,000 candidates
# per building at Postgres's per-row executor overhead. None of that
# overhead exists in Python's tight loop. A real spatial index (PostGIS
# GiST/KNN) would change this calculus; this codebase deliberately doesn't
# carry that dependency -- see haversine_miles' own module docstring.
def nearest_mechanical_contractor_bulk(session: Session, buildings: list[RetrofitBuilding], *,
                                       radius_miles: float) -> dict[int, dict]:
    """The single nearest mechanical contractor per building -- for the
    retrofit board's mobile card view (app/web/templates/retrofit_board.html),
    where every rendered row needs a tel: link, not the detail page's top-10
    list. Calling nearest_mechanical_contractors() once per row would be up
    to `limit` (200) separate bounding-box queries; this does ONE query
    instead, over a bounding box covering every building's location at once,
    column-only (measured 2026-08-20: fetching full Contractor ORM rows over
    a 200-building/~28k-contractor worst case took ~3s; column-only cut that
    to ~0.85s -- see app.web.main:retrofit_board for the full-page budget
    this needs to fit inside). Returns {building_id: {contractor_name,
    contractor_phone, distance_miles}}; a building with no match (none
    within radius, or the building itself isn't geocoded) is simply absent
    from the dict, not a null-valued entry."""
    geocoded = [b for b in buildings if b.latitude is not None and b.longitude is not None]
    if not geocoded:
        return {}
    lats = [b.latitude for b in geocoded]
    lons = [b.longitude for b in geocoded]
    lat_pad = radius_miles / MILES_PER_DEGREE_LAT
    lon_pad = radius_miles / (MILES_PER_DEGREE_LAT * max(math.cos(math.radians(sum(lats) / len(lats))), 0.01))
    lat_min, lat_max = min(lats) - lat_pad, max(lats) + lat_pad
    lon_min, lon_max = min(lons) - lon_pad, max(lons) + lon_pad

    candidates = session.exec(
        select(Contractor.business_name, Contractor.business_phone, Contractor.latitude,
              Contractor.longitude, Contractor.classifications)
        .where(
            Contractor.latitude.is_not(None),
            Contractor.latitude.between(lat_min, lat_max),
            Contractor.longitude.between(lon_min, lon_max),
        )
    ).all()
    mechanical = [c for c in candidates if _is_mechanical_classification(c.classifications)]

    out: dict[int, dict] = {}
    for b in geocoded:
        best_name, best_phone, best_dist = None, None, None
        for name, phone, clat, clon, _cls in mechanical:
            d = haversine_miles(b.latitude, b.longitude, clat, clon)
            if d <= radius_miles and (best_dist is None or d < best_dist):
                best_name, best_phone, best_dist = name, phone, d
        if best_name is not None:
            out[b.id] = {"contractor_name": best_name, "contractor_phone": best_phone,
                        "distance_miles": round(best_dist, 1)}
    return out


# ---- account roster <-> CSLB join --------------------------------------
#
# The join most likely to actually fire for a rep's roster: Jason's list is
# contractors, and CSLB carries ~48,870 of them in territory (see
# app.pipeline.cslb), against only 23 mech_contractor/gc rows on Scout's own
# firm roster (app.accounts.accounts_matching_firm) -- see `firm-type-counts`.
#
# Reuses app.pipeline.local250's name-cleaning and ambiguity discipline
# verbatim (clean_contractor_name/MIN_NAME_LEN/NAME_ONLY_THRESHOLD/
# NAME_WITH_CITY_THRESHOLD, promoted out of that module for this second
# caller) rather than a new implementation: it is the SAME problem --
# fuzzy-match a freeform business name against this same CSLB roster -- and
# that module already earned its threshold/tie-handling the hard way (see
# its own docstring on "Building Aire Inc." silently reducing to empty
# under app.normalize.normalize_name). A tie (more than one CSLB license
# clearing the threshold at the same top score) is reported as ambiguous
# and left unresolved, never guessed -- CSLB's own roster genuinely
# contains multiple, unrelated licenses under near-identical names (a
# common trade name reused by different owners in different counties), and
# picking one would be exactly the kind of fabricated precision this
# pipeline is built to refuse elsewhere.


def build_cslb_match_candidates(session: Session, county: str | None = None) -> list[tuple[Contractor, str]]:
    """Every (contractor, cleaned_name) pair worth fuzzy-matching against --
    built ONCE per report run, not once per account (a live per-account
    scan of all ~48,870 in-territory contractors is the same N x M shape
    match_local250 already solves this way). A contractor with both
    business_name and full_business_name appears twice, each cleaned name
    matched independently, same as match_local250's own candidate list.

    county, when given, prefilters to Contractor.county == county before
    cleaning -- a single-account page load (app.accounts.build_account_page)
    cannot afford to clean+fuzzy-match all ~48,870 in-territory contractors
    on every request (measured ~5.6s to build the full candidate list), so
    it narrows to the account's own county first. This is a real recall
    trade-off, not a free optimization: a contractor whose CSLB business
    (mailing) address sits in a NEIGHBORING county from the account's own
    would be missed here even though the batched, unfiltered
    account-join-report CLI (which amortizes the full build across every
    account in one run) would still find it. Leave county=None for that
    batched, exhaustive case."""
    q = select(Contractor)
    if county:
        q = q.where(Contractor.county == county)
    out: list[tuple[Contractor, str]] = []
    for c in session.exec(q).all():
        for candidate_name in (c.business_name, c.full_business_name):
            if not candidate_name:
                continue
            norm = clean_contractor_name(candidate_name)
            if len(norm) >= MIN_NAME_LEN:
                out.append((c, norm))
    return out


def match_account_to_cslb(account_name: str, account_city: str | None,
                          candidates: list[tuple[Contractor, str]]) -> dict:
    """Fuzzy-match one imported account's name against the CSLB candidate
    list from build_cslb_match_candidates. account_city, when it agrees
    with the candidate's own CSLB business (mailing) address city, is real
    corroboration -- unlike a project's job-site address, a contractor's
    own office/mailing city has every reason to match the same account's
    city in a rep's CRM, so agreement earns the lower NAME_WITH_CITY_THRESHOLD
    the same way match_local250 already uses it.

    Returns {'contractor': Contractor | None, 'ambiguous': bool,
    'candidates': list[Contractor]}: exactly one candidate clearing the
    threshold at the top score is a match (candidates has that one row);
    zero is no match (candidates is empty); more than one tied at the top
    score is 'ambiguous' (contractor is None, candidates carries every tied
    row so a caller can show them rather than picking one) -- never picked
    at random, and never silently resolved by taking the first row back
    from the database."""
    norm = clean_contractor_name(account_name)
    if len(norm) < MIN_NAME_LEN:
        return {"contractor": None, "ambiguous": False, "candidates": []}
    acct_city = (account_city or "").strip().lower()

    best_score = 0.0
    best_by_id: dict[int, Contractor] = {}
    for c, norm_candidate in candidates:
        score = fuzz.token_sort_ratio(norm, norm_candidate)
        same_city = bool(acct_city and c.city and acct_city == c.city.strip().lower())
        threshold = NAME_WITH_CITY_THRESHOLD if same_city else NAME_ONLY_THRESHOLD
        if score < threshold:
            continue
        if score > best_score:
            best_score, best_by_id = score, {c.id: c}
        elif score == best_score:
            best_by_id[c.id] = c

    best = list(best_by_id.values())
    if len(best) == 1:
        return {"contractor": best[0], "ambiguous": False, "candidates": best}
    if len(best) > 1:
        return {"contractor": None, "ambiguous": True, "candidates": best}
    return {"contractor": None, "ambiguous": False, "candidates": []}


def overdue_buildings_near_contractor(session: Session, contractor: Contractor,
                                      radius_miles: float) -> int:
    """Count of retrofit candidates within radius_miles whose own
    service_life_status is specifically 'overdue' -- NOT the broader
    replacement_candidate population nearby_replacement_candidates returns
    (that population is ~95% overdue but also carries a due/approaching/
    not_due tail; see RetrofitBuilding.service_life_status). Filters the
    same live query rather than adding a second bounding-box/haversine
    path, since this is a per-contractor lookup (one row on an account
    report), not a bulk precompute."""
    return sum(1 for b in nearby_replacement_candidates(session, contractor, radius_miles)
              if b.service_life_status == "overdue")


def overdue_buildings_near_contractor_detail(session: Session, contractor: Contractor,
                                             radius_miles: float) -> list[dict]:
    """Every overdue building within radius_miles, BY DISTANCE -- deliberately
    a different sort than nearby_replacement_candidates' own urgency-first
    order (right for /contractors' cross-contractor ranking, wrong here:
    "what's closest to drive to" is the question a rep taking a meeting
    actually has, not "what's most overdue somewhere in a 15mi circle").
    Returns the FULL sorted list, nearest first -- like
    nearby_replacement_candidates itself, callers slice for a top-N display
    and use len() for the true count (see /contractor/{id}'s own
    nearby[:10]/nearby_total pattern) rather than this function silently
    deciding how many are worth showing. Empty (never an error) if the
    contractor has no geocode -- same discipline nearby_replacement_candidates
    itself uses."""
    if contractor.latitude is None or contractor.longitude is None:
        return []
    overdue = [b for b in nearby_replacement_candidates(session, contractor, radius_miles)
              if b.service_life_status == "overdue"]
    with_distance = [
        {"building": b, "distance_miles": round(
            haversine_miles(contractor.latitude, contractor.longitude, b.latitude, b.longitude), 1)}
        for b in overdue
    ]
    with_distance.sort(key=lambda row: row["distance_miles"])
    return with_distance
