from app.normalize import normalize_county, normalize_name


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
