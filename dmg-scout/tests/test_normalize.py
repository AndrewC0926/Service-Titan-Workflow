from app.normalize import normalize_county, normalize_name, normalize_state


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
