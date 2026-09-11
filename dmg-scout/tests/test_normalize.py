from app.normalize import (
    normalize_company_name,
    normalize_county,
    normalize_name,
    normalize_state,
)


def test_strips_legal_suffixes():
    assert normalize_name("Vantage Data Centers NV11, LLC") == "vantage data centers"
    assert normalize_name("Switch, Inc.") == "switch"
    assert normalize_name("Tech Core PY A, LLC") == "tech core py a"


def test_strips_roman_numerals_and_phases():
    assert normalize_name("Gateway Industrial Phase II, L.P.") == "gateway industrial"
    assert normalize_name("Reno Technology Park Building 4") == "reno technology park"


def test_same_developer_different_spes_match():
    a = normalize_name("Vantage Data Centers NV11, LLC")
    b = normalize_name("Vantage Data Centers NV12, LLC")
    assert a == b


def test_county_normalization():
    assert normalize_county("Riverside County") == "Riverside"
    assert normalize_county("storey county") == "Storey"
    assert normalize_county(None) is None


def test_state_full_name_to_code():
    assert normalize_state("Nevada") == "NV"
    assert normalize_state("nevada") == "NV"
    assert normalize_state("California") == "CA"
    assert normalize_state("Arizona") == "AZ"


def test_state_code_passes_through_case_folded():
    assert normalize_state("NV") == "NV"
    assert normalize_state("nv") == "NV"
    assert normalize_state("Ca") == "CA"


def test_state_unrecognized_value_passes_through_unchanged():
    """Null over inference: an unrecognized value is not dropped or guessed
    at, so the state_values doctor check can still see and report it."""
    assert normalize_state("Some Territory") == "Some Territory"


def test_state_none_and_empty():
    assert normalize_state(None) is None
    assert normalize_state("") is None


# ---- normalize_company_name -------------------------------------------
# Regression set for the measured Firm-table blast radius (Phase B report):
# normalize_name's _ROMAN and _SPE_CODE passes, applied to an ordinary
# company name rather than a project/SPE name, silently collide unrelated
# real companies -- LM Construction, MCM Engineering, L&D Engineering, P2S
# Engineering, and Engineering Partners must all stay distinct under the
# company-identity function even though normalize_name collapses several
# of them to "construction"/"engineering" today.

def test_leading_roman_numeral_shaped_tokens_stay_distinct():
    names = ["LM Construction", "MCM Engineering", "L&D Engineering",
             "P2S Engineering, Inc.", "Engineering Partners"]
    norms = [normalize_company_name(n) for n in names]
    assert len(set(norms)) == len(names), dict(zip(names, norms))


def test_normalize_name_still_collides_the_same_names():
    """The bug being fixed, pinned down so a future change to normalize_name
    itself (which must NOT change -- project/permit signals depend on this
    exact behavior) trips this test instead of silently reintroducing the
    Firm-table collision under a different name."""
    assert normalize_name("LM Construction") == "construction"
    assert normalize_name("MCM Engineering") == "engineering"
    assert normalize_name("L&D Engineering Inc.") == "engineering"
    assert normalize_name("P2S Engineering, Inc.") == "engineering"


def test_company_name_phase_and_building_language_still_strips():
    """The one case where a roman-numeral-shaped token adjacent to
    phase/building/parcel/unit language SHOULD still collapse, same as
    normalize_name -- _PHASE consumes it directly, unchanged."""
    assert normalize_company_name("Gateway Industrial Phase II, L.P.") == "gateway industrial"
    assert normalize_company_name("Reno Technology Park Building III") == "reno technology park"


def test_company_name_legal_suffixes_still_strip():
    assert normalize_company_name("Switch, Inc.") == "switch"
    assert normalize_company_name("Turner Construction Company") == "turner construction"
