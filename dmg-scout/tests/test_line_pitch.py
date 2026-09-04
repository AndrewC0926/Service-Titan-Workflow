"""Line-card training pitch generation: grounding (the field the whole
feature rests on), the closed-set competitor constraint, and the
propose-never-assert skip logic (existence_verified=False, confirmed rows
never touched). Every LLM call and every HTTP fetch is mocked -- this
suite never spends real money or touches a real network, matching
tests/test_sam_gov.py and tests/test_ab869.py's own discipline.
"""
import pytest
from sqlmodel import select

import app.pipeline.line_pitch as line_pitch
from app.models import CompetitorLine, LineCompetitor, LinePitch, ProductLine, ProductLineBranch
from app.pipeline.line_pitch import (
    BANNED_PHRASES,
    _extract_claim_fragments,
    _page_contains_line_name,
    _sanitize_field,
    _truncate_to_words,
    _validate_competitor_names,
    candidate_domains,
    discover_domain_via_web_search,
    generate_one_line,
    ground_list,
    ground_required_claim,
    ground_text,
    line_row_only_pitch,
    run_line_pitch_generation,
)

FETCHED_URL = "https://example.com/product"

NO_DOMAIN_FOUND = {"domain": None, "url": None, "page_text": None,
                   "fetch_status": "no_domain_found", "searched": True, "cost_usd": 0.0}


@pytest.fixture(autouse=True)
def _no_real_web_search(monkeypatch):
    """Every test in this file gets a safe no-op web_search stub by
    default -- generate_one_line now tries discover_domain_via_web_search
    (a real Anthropic + web_search API call) whenever a fetch fails and
    line.official_domain isn't already set, which is exactly the
    candidate_domains=[] shape most tests below already use to reach
    line_row_only. Without this, those tests would attempt a real network
    call. A test that wants to exercise discovery itself overrides this
    with its own monkeypatch.setattr call, which simply takes precedence
    for that one test."""
    monkeypatch.setattr(line_pitch, "discover_domain_via_web_search",
                        lambda cfg, line: dict(NO_DOMAIN_FOUND))


def _mock_fetched(monkeypatch, page_text=None):
    """Simulate a successful page fetch so generate_one_line proceeds to
    the LLM call -- most tests exercising the full LLM/grounding/
    competitor path need this instead of the bare candidate_domains=[]
    stub, which now short-circuits straight to line_row_only_pitch with
    NO LLM call at all (see generate_one_line's 2026-09-03 rewrite)."""
    text = page_text or "AAON manufactures packaged rooftop units with capacities from 3 to 230 tons."
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: ["example.com"])
    monkeypatch.setattr(line_pitch, "fetch_product_pages", lambda d: (text, FETCHED_URL, "fetched", [FETCHED_URL]))


def _line(session, name="AAON", building_role="air_handling", existence_verified=None,
         basis_field="country_of_manufacture_basis", basis_text=None, **kw):
    defaults = dict(name=name, name_norm=name.lower(), category="rooftop_units",
                    building_role=building_role, existence_verified=existence_verified)
    defaults.update(kw)
    if basis_text is not None:
        defaults[basis_field] = basis_text
    line = ProductLine(**defaults)
    session.add(line)
    session.commit()
    session.refresh(line)
    return line


def _competitor(session, manufacturer, building_role, source_url="https://example.com/rep-card"):
    row = CompetitorLine(manufacturer=manufacturer, building_role=building_role,
                         source_url=source_url)
    session.add(row)
    session.commit()
    return row


# ---- grounding: the field the whole conclusion rests on --------------------

PAGE = "AAON manufactures packaged rooftop units with capacities from 3 to 230 tons. AAON is AHRI certified."


def test_real_grounded_claim_is_kept():
    text = "Capacities run from 3 to 230 tons and it is AHRI certified."
    kept, grounded, dropped = ground_text(text, PAGE)
    assert kept == text
    assert grounded == 2 and dropped == 0


def test_fabricated_numeric_claim_is_dropped_wholesale_not_softened():
    """The exact bug caught while building this: grounding the whole
    SENTENCE against a paraphrase fails even for a real number: the fix is
    fragment-level grounding, but a truly fabricated number must still be
    dropped, sentence and all."""
    text = "AAON builds units up to 500 tons, which isn't on the page."
    kept, grounded, dropped = ground_text(text, PAGE)
    assert kept is None
    assert grounded == 0 and dropped == 1


def test_no_page_means_every_claim_is_dropped():
    text = "AAON builds units up to 230 tons."  # true, and even on-page, but no page was given
    kept, grounded, dropped = ground_text(text, None)
    assert kept is None
    assert dropped == 1


def test_non_claim_sentences_pass_through_untouched():
    text = "AAON is a well-known brand. It ships from several US plants."
    kept, grounded, dropped = ground_text(text, None)
    assert kept == text
    assert grounded == 0 and dropped == 0


def test_partial_fail_sentence_drops_the_whole_sentence():
    """One real fragment + one fabricated fragment in the SAME sentence ->
    the whole sentence is dropped, never rewritten to keep only the real half."""
    text = "Runs at 95 dBA and up to 900 CFM."
    page = "Runs at 95 dBA and up to 400 CFM."
    kept, grounded, dropped = ground_text(text, page)
    assert kept is None
    assert dropped == 2  # both fragments counted -- the sentence-level all() check failed


def test_percent_claims_are_actually_detected():
    """Regression: the original number+unit regex had ONE trailing \\b
    shared across the whole alternation, which silently matched ZERO real
    '%'-suffixed claims (a \\b right after a symbol followed by whitespace
    is never a boundary). Caught before ever running this against a real
    line -- this pins the fix."""
    fragments = _extract_claim_fragments("20% higher SEER rating")
    assert fragments == ["20%"]
    kept, grounded, dropped = ground_text("Rated 20% more efficient.", "No efficiency numbers here.")
    assert kept is None and dropped == 1


def test_ground_list_drops_only_the_ungrounded_item():
    items = ["Fast lead times", "20% higher SEER rating", "AHRI certified"]
    kept, grounded, dropped = ground_list(items, PAGE)
    assert kept == ["Fast lead times", "AHRI certified"]
    assert grounded == 1 and dropped == 1


def test_ground_list_never_pads_back_to_original_length():
    items = ["500 tons", "900 tons"]  # both fabricated
    kept, grounded, dropped = ground_list(items, PAGE)
    assert kept == []
    assert dropped == 2


# ---- broadened claim extraction (2026-09-03): qualified/word quantities,
# certifications, and capability-trigger windows, not just bare numbers ----

AIRZONE_PAGE = "Airzone's control system can be managed with over 200 brands of AC units, and +200 compatible brands are listed in the app."


def test_airzone_over_200_compatible_brands_is_extracted_and_grounds():
    """Regression: the round-1 extractor had no pattern for a qualifier +
    number claim ('over 200') or a bare count of a generic noun
    ('brands'), so this exact real sentence went out with ZERO claim
    fragments attempted -- it was never checked against the page at all.
    This is the round-2 motivating bug, named explicitly in the brief."""
    sentence = "It supports over 200 compatible AC brands."
    fragments = _extract_claim_fragments(sentence)
    assert any("over 200" in f for f in fragments)
    kept, grounded, dropped = ground_text(sentence, AIRZONE_PAGE)
    assert kept == sentence
    assert grounded >= 1 and dropped == 0


def test_airzone_fabricated_quantity_is_dropped():
    sentence = "It supports over 900 compatible AC brands."
    kept, grounded, dropped = ground_text(sentence, AIRZONE_PAGE)
    assert kept is None
    assert dropped >= 1


def test_word_quantity_claims_are_extracted():
    fragments = _extract_claim_fragments("Trusted by hundreds of contractors nationwide.")
    assert any("hundreds" in f.lower() for f in fragments)


def test_capability_trigger_window_grounds_against_matching_text():
    page = "The controller integrates with Smart Home and BMS systems out of the box."
    sentence = "The controller integrates with Smart Home and BMS systems."
    kept, grounded, dropped = ground_text(sentence, page)
    assert kept == sentence
    assert grounded >= 1


def test_capability_trigger_window_drops_a_fabricated_integration():
    page = "The controller integrates with Smart Home and BMS systems out of the box."
    sentence = "The controller integrates with rocket launch control systems."
    kept, grounded, dropped = ground_text(sentence, page)
    assert kept is None
    assert dropped >= 1


# ---- register/lint: banned filler, wrapping quotes, exclamation marks -----

def test_sanitize_strips_wrapping_quotes():
    assert _sanitize_field('"A rooftop unit line."') == "A rooftop unit line."
    assert _sanitize_field("'A rooftop unit line.'") == "A rooftop unit line."


def test_sanitize_replaces_exclamation_marks():
    assert _sanitize_field("Great fit!") == "Great fit."


def test_sanitize_passes_through_clean_text_unchanged():
    assert _sanitize_field("A plain sentence.") == "A plain sentence."


@pytest.mark.parametrize("phrase", BANNED_PHRASES)
def test_banned_phrases_are_dropped_from_ground_text(phrase):
    text = f"This line is great -- {phrase}."
    kept, grounded, dropped = ground_text(text, None)
    assert kept is None


@pytest.mark.parametrize("phrase", BANNED_PHRASES)
def test_banned_phrases_are_dropped_from_ground_list(phrase):
    kept, grounded, dropped = ground_list([f"Sentence with {phrase} in it.", "A clean item."], None)
    assert kept == ["A clean item."]


def test_ground_text_keeps_clean_sentences_alongside_dropped_banned_ones():
    text = "It is a packaged rooftop unit. You name it, we stock it."
    kept, grounded, dropped = ground_text(text, None)
    assert kept == "It is a packaged rooftop unit."


# ---- ground_required_claim: the single mandatory differentiator -----------

def test_ground_required_claim_keeps_a_grounded_claim():
    item, grounded, dropped = ground_required_claim("AHRI certified across the line.", PAGE)
    assert item == "AHRI certified across the line."
    assert grounded == 1 and dropped == 0


def test_ground_required_claim_drops_an_ungrounded_claim():
    item, grounded, dropped = ground_required_claim("Rated for 900 tons.", PAGE)
    assert item is None
    assert dropped >= 1


def test_ground_required_claim_drops_a_non_claim_item():
    """Unlike ground_list (fine for a non-factual engineer question), this
    slot's entire purpose is to BE a checkable claim -- an item with no
    extractable claim fragment at all must be dropped, not passed through."""
    item, grounded, dropped = ground_required_claim("A well-regarded brand.", PAGE)
    assert item is None
    assert dropped == 1


def test_ground_required_claim_drops_a_banned_phrase():
    item, grounded, dropped = ground_required_claim("That's the pitch.", PAGE)
    assert item is None


def test_ground_required_claim_handles_none_and_empty():
    assert ground_required_claim(None, PAGE) == (None, 0, 0)
    assert ground_required_claim("", PAGE) == (None, 0, 0)


# ---- truncation: cut at a sentence boundary, never mid-thought ------------

def test_truncate_prefers_a_sentence_boundary_over_a_hard_word_cut():
    """The exact bug caught reviewing this module's own first production
    run against real data (2026-09-02, the Aldes line): a hard word-count
    cut left the pitch trailing off mid-sentence ('We carry them across
    Central Coast, Central'). Must cut at the last complete sentence that
    fits instead."""
    text = ("First sentence here has several words in it for padding purposes today. "
           "Second sentence also has quite a few words in it as well for padding. "
           "Third sentence trails off into something that will not fit at all here now.")
    result, truncated = _truncate_to_words(text, 20)
    assert truncated is True
    assert result.endswith(".")
    assert "trails off" not in result  # the incomplete third sentence must not appear at all


def test_truncate_falls_back_to_hard_cut_when_no_sentence_fits():
    """A single sentence longer than the whole budget -- no sentence
    boundary exists within it, so the hard cut is the only option left."""
    text = "one two three four five six seven eight nine ten eleven twelve."
    result, truncated = _truncate_to_words(text, 5)
    assert truncated is True
    assert result == "one two three four five"


def test_truncate_leaves_short_text_untouched():
    text = "A short pitch under the limit."
    result, truncated = _truncate_to_words(text, 60)
    assert result == text
    assert truncated is False


# ---- candidate_domains: never a web search, never a guessed {name}.com ----

def test_candidate_domain_found_from_own_basis_text(db_session):
    line = _line(db_session, name="AAON",
                basis_text="AAON's own corporate site (aaon.com/about) states the company operates five plants.")
    assert candidate_domains(line) == ["aaon.com"]


def test_no_candidate_domain_when_basis_text_has_none(db_session):
    line = _line(db_session, name="Marley", basis_text=None)
    assert candidate_domains(line) == []


def test_excluded_generic_domains_never_returned_even_with_name_substring(db_session):
    """A citation to a third-party directory that happens to contain the
    line's own name as a substring must never be treated as the
    manufacturer's own page."""
    line = _line(db_session, name="Wiki",
                basis_text="See wikipedia.org for more on Wiki brand history.")
    assert candidate_domains(line) == []


def test_never_falls_back_to_a_guessed_domain(db_session):
    """No basis text at all -- must return empty, never invent '{name}.com'."""
    line = _line(db_session, name="TotallyRealBrand")
    assert candidate_domains(line) == []


def test_official_domain_takes_priority_over_basis_text(db_session):
    """A previously web_search-discovered, fetch-validated domain
    (line.official_domain) is tried FIRST -- it has already survived a
    real fetch + name check, unlike anything merely extracted from basis
    text citations."""
    line = _line(db_session, name="AAON",
                basis_text="AAON's own corporate site (other-domain.com) states...",
                official_domain="aaon.com")
    assert candidate_domains(line) == ["aaon.com"]


# ---- web_search domain discovery + two-page fetch (2026-09-04) ------------

def test_page_contains_line_name_true_for_a_real_mention():
    assert _page_contains_line_name("Welcome to AAON's official site.", "AAON")


def test_page_contains_line_name_false_when_absent():
    assert not _page_contains_line_name("Welcome to our official site.", "AAON")


def test_page_contains_line_name_normalizes_punctuation():
    """Punctuation immediately around the name (not interspersed within
    it) must not defeat the match -- e.g. a trailing comma before 'Inc'."""
    assert _page_contains_line_name("Welcome to AAON, Inc's official site.", "AAON")


def test_page_contains_line_name_false_for_empty_inputs():
    assert not _page_contains_line_name(None, "AAON")
    assert not _page_contains_line_name("some text", "")


def test_discover_domain_via_web_search_accepts_a_validated_domain(db_session, monkeypatch):
    line = _line(db_session, name="AAON")
    import app.llm as llm_mod
    monkeypatch.setattr(llm_mod, "find_line_domain",
                        lambda name, category, description: {"domain": "aaon.com", "model": "x",
                                                              "web_searches": 1, "cost_usd": 0.02,
                                                              "raw_text": "DOMAIN: aaon.com"})
    monkeypatch.setattr(line_pitch, "fetch_product_pages",
                        lambda d: ("Welcome to AAON's site.", "https://aaon.com/", "fetched", ["https://aaon.com/"]))

    result = discover_domain_via_web_search(cfg=None, line=line)
    assert result["domain"] == "aaon.com"
    assert result["fetch_status"] == "fetched"
    assert result["cost_usd"] == 0.02


def test_discover_domain_via_web_search_rejects_a_page_missing_the_line_name(db_session, monkeypatch):
    line = _line(db_session, name="AAON")
    import app.llm as llm_mod
    monkeypatch.setattr(llm_mod, "find_line_domain",
                        lambda name, category, description: {"domain": "wrongsite.com", "model": "x",
                                                              "web_searches": 1, "cost_usd": 0.02,
                                                              "raw_text": "DOMAIN: wrongsite.com"})
    monkeypatch.setattr(line_pitch, "fetch_product_pages",
                        lambda d: ("This page never mentions the line.", "https://wrongsite.com/",
                                  "fetched", ["https://wrongsite.com/"]))

    result = discover_domain_via_web_search(cfg=None, line=line)
    assert result["domain"] is None


def test_discover_domain_via_web_search_rejects_an_excluded_domain(db_session, monkeypatch):
    line = _line(db_session, name="AAON")
    import app.llm as llm_mod
    monkeypatch.setattr(llm_mod, "find_line_domain",
                        lambda name, category, description: {"domain": "wikipedia.org", "model": "x",
                                                              "web_searches": 1, "cost_usd": 0.02,
                                                              "raw_text": "DOMAIN: wikipedia.org"})

    def boom(d):
        raise AssertionError("must never fetch an excluded domain")
    monkeypatch.setattr(line_pitch, "fetch_product_pages", boom)

    result = discover_domain_via_web_search(cfg=None, line=line)
    assert result["domain"] is None


def test_discover_domain_via_web_search_handles_domain_none(db_session, monkeypatch):
    line = _line(db_session, name="AAON")
    import app.llm as llm_mod
    monkeypatch.setattr(llm_mod, "find_line_domain",
                        lambda name, category, description: {"domain": None, "model": "x",
                                                              "web_searches": 1, "cost_usd": 0.01,
                                                              "raw_text": "DOMAIN: none"})
    result = discover_domain_via_web_search(cfg=None, line=line)
    assert result["domain"] is None


def test_discover_domain_via_web_search_propagates_budget_exceeded(db_session, monkeypatch):
    from app.spend import BudgetExceeded
    line = _line(db_session, name="AAON")
    import app.llm as llm_mod

    def boom(name, category, description):
        raise BudgetExceeded("cap hit")
    monkeypatch.setattr(llm_mod, "find_line_domain", boom)

    with pytest.raises(BudgetExceeded):
        discover_domain_via_web_search(cfg=None, line=line)


def test_generate_one_line_uses_web_search_when_basis_text_has_no_domain(db_session, cfg, monkeypatch):
    """The 29-line_row_only rerun's whole point: a line with nothing in
    its own basis text now gets ONE web_search attempt before falling
    back to line_row_only, and a domain it finds AND validates is written
    onto the line so a future run never searches again."""
    line = _line(db_session, name="AAON")
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])
    monkeypatch.setattr(line_pitch, "discover_domain_via_web_search",
                        lambda cfg, line: {"domain": "aaon.com", "url": "https://aaon.com/",
                                          "page_text": "AAON page text.", "fetch_status": "fetched",
                                          "searched": True, "cost_usd": 0.02})
    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: _fake_raw())

    result = generate_one_line(db_session, cfg, line)
    assert result["ok"] is True
    assert result["pitch_scope"] == "full"
    assert result["domain_source"] == "web_search"
    db_session.refresh(line)
    assert line.official_domain == "aaon.com"
    assert line.official_domain_source == "web_search"
    assert line.official_domain_url == "https://aaon.com/"


def test_generate_one_line_never_searches_again_once_official_domain_is_set(db_session, cfg, monkeypatch):
    """One web search per line, ever -- once official_domain is set (even
    if THIS run's fetch of it fails), discover_domain_via_web_search must
    not be called again."""
    line = _line(db_session, name="AAON", official_domain="aaon.com")
    monkeypatch.setattr(line_pitch, "fetch_product_pages", lambda d: (None, None, "fetch_failed", []))

    def boom(cfg, line):
        raise AssertionError("must not search again once official_domain is set")
    monkeypatch.setattr(line_pitch, "discover_domain_via_web_search", boom)

    result = generate_one_line(db_session, cfg, line)
    assert result["pitch_scope"] == "line_row_only"
    assert result["web_search_attempted"] is False


def test_generate_one_line_skips_web_search_when_basis_domain_already_fetches(db_session, cfg, monkeypatch):
    """A line WHOSE basis-text domain fetches successfully never triggers
    a web search at all -- the fallback only fires when the primary path
    fails."""
    _mock_fetched(monkeypatch)
    line = _line(db_session, name="AAON")

    def boom(cfg, line):
        raise AssertionError("must not search when the basis-text domain already fetched")
    monkeypatch.setattr(line_pitch, "discover_domain_via_web_search", boom)

    result = generate_one_line(db_session, cfg, line)
    assert result["pitch_scope"] == "full"
    assert result["web_search_attempted"] is False


# ---- _extract_product_like_link: the second, product-like page ------------

def test_extract_product_like_link_prefers_product_over_solutions():
    from selectolax.parser import HTMLParser
    from app.pipeline.line_pitch import _extract_product_like_link
    html = """<html><body>
    <a href="/solutions">Solutions</a>
    <a href="/products">Products</a>
    </body></html>"""
    tree = HTMLParser(html)
    link = _extract_product_like_link(tree, "https://example.com/")
    assert link == "https://example.com/products"


def test_extract_product_like_link_ignores_a_different_host():
    from selectolax.parser import HTMLParser
    from app.pipeline.line_pitch import _extract_product_like_link
    html = '<html><body><a href="https://other-domain.com/products">Products</a></body></html>'
    tree = HTMLParser(html)
    assert _extract_product_like_link(tree, "https://example.com/") is None


def test_extract_product_like_link_none_when_nothing_matches():
    from selectolax.parser import HTMLParser
    from app.pipeline.line_pitch import _extract_product_like_link
    html = '<html><body><a href="/about">About us</a></body></html>'
    tree = HTMLParser(html)
    assert _extract_product_like_link(tree, "https://example.com/") is None


def test_extract_product_like_link_matches_on_link_text_not_just_href():
    from selectolax.parser import HTMLParser
    from app.pipeline.line_pitch import _extract_product_like_link
    html = '<html><body><a href="/en/offerings">Our Catalog</a></body></html>'
    tree = HTMLParser(html)
    assert _extract_product_like_link(tree, "https://example.com/") == "https://example.com/en/offerings"


# ---- _is_ssl_cert_error / _fetch_home_page: www./http fallback (2026-09-04) --
# Real production case: titus-hvac.com's own cert doesn't cover the bare
# hostname. A www./http retry is ONLY correct for an SSL cert failure --
# a DNS/connection/timeout/404 failure would not be fixed by it, so those
# must stop after one attempt instead of spending two more requests.

def test_is_ssl_cert_error_detects_a_real_ssl_error():
    import ssl
    from app.pipeline.line_pitch import _is_ssl_cert_error
    exc = ssl.SSLCertVerificationError("certificate verify failed: Hostname mismatch")
    assert _is_ssl_cert_error(exc)


def test_is_ssl_cert_error_detects_a_wrapped_ssl_error():
    import ssl
    from app.pipeline.line_pitch import _is_ssl_cert_error
    inner = ssl.SSLCertVerificationError("Hostname mismatch")
    wrapper = ConnectionError("connect failed")
    wrapper.__cause__ = inner
    assert _is_ssl_cert_error(wrapper)


def test_is_ssl_cert_error_false_for_an_unrelated_error():
    from app.pipeline.line_pitch import _is_ssl_cert_error
    assert not _is_ssl_cert_error(TimeoutError("timed out"))
    assert not _is_ssl_cert_error(ConnectionError("Name or service not known"))


class _FakeResp:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    """A minimal stand-in for app.http.PoliteClient -- maps a URL to
    either an _FakeResp or an exception to raise, and records every URL
    it was asked to fetch so a test can assert exactly which fallbacks
    were (or weren't) attempted."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, BaseException):
            raise result
        return _FakeResp(result)


HTML_OK = "<html><body>Real content here.</body></html>"


def test_fetch_home_page_falls_back_to_www_on_ssl_error():
    import ssl
    client = _FakeClient({
        "https://titus-hvac.com/": ssl.SSLCertVerificationError("Hostname mismatch"),
        "https://www.titus-hvac.com/": HTML_OK,
    })
    tree, url, status = line_pitch._fetch_home_page(client, "titus-hvac.com")
    assert status == "fetched"
    assert url == "https://www.titus-hvac.com/"
    assert client.calls == ["https://titus-hvac.com/", "https://www.titus-hvac.com/"]


def test_fetch_home_page_falls_back_to_http_as_last_resort():
    import ssl
    client = _FakeClient({
        "https://titus-hvac.com/": ssl.SSLCertVerificationError("Hostname mismatch"),
        "https://www.titus-hvac.com/": ssl.SSLCertVerificationError("Hostname mismatch"),
        "http://titus-hvac.com/": HTML_OK,
    })
    tree, url, status = line_pitch._fetch_home_page(client, "titus-hvac.com")
    assert status == "fetched"
    assert url == "http://titus-hvac.com/"
    assert len(client.calls) == 3


def test_fetch_home_page_does_not_fall_back_on_a_non_ssl_error():
    client = _FakeClient({
        "https://example.com/": ConnectionError("Name or service not known"),
    })
    tree, url, status = line_pitch._fetch_home_page(client, "example.com")
    assert status == "fetch_failed"
    assert client.calls == ["https://example.com/"]  # never tried www./http


def test_fetch_home_page_skips_www_variant_when_domain_already_has_www():
    import ssl
    client = _FakeClient({
        "https://www.example.com/": ssl.SSLCertVerificationError("mismatch"),
        "http://www.example.com/": HTML_OK,
    })
    tree, url, status = line_pitch._fetch_home_page(client, "www.example.com")
    assert status == "fetched"
    assert url == "http://www.example.com/"
    assert client.calls == ["https://www.example.com/", "http://www.example.com/"]


def test_fetch_home_page_all_variants_fail():
    import ssl
    client = _FakeClient({
        "https://example.com/": ssl.SSLCertVerificationError("mismatch"),
        "https://www.example.com/": ssl.SSLCertVerificationError("mismatch"),
        "http://example.com/": ssl.SSLCertVerificationError("mismatch"),
    })
    tree, url, status = line_pitch._fetch_home_page(client, "example.com")
    assert status == "fetch_failed"
    assert tree is None and url is None


# ---- set_manual_domain: a human overriding search/basis-text discovery ----

def test_set_manual_domain_writes_fields(db_session):
    from app.pipeline.line_pitch import set_manual_domain
    line = _line(db_session, name="TCF/Twin City Fan")
    updated = set_manual_domain(db_session, "TCF/Twin City Fan", "tcf.com")
    assert updated.official_domain == "tcf.com"
    assert updated.official_domain_source == "andrew_confirmed"
    db_session.refresh(line)
    assert line.official_domain == "tcf.com"


def test_set_manual_domain_accepts_a_custom_source(db_session):
    from app.pipeline.line_pitch import set_manual_domain
    _line(db_session, name="Marley")
    updated = set_manual_domain(db_session, "Marley", "spxcooling.com", source="manual_override")
    assert updated.official_domain_source == "manual_override"


def test_set_manual_domain_raises_for_an_unknown_line(db_session):
    from app.pipeline.line_pitch import set_manual_domain
    with pytest.raises(ValueError):
        set_manual_domain(db_session, "Not A Real Line", "example.com")


def test_set_manual_domain_takes_priority_in_candidate_domains(db_session):
    from app.pipeline.line_pitch import set_manual_domain
    line = _line(db_session, name="Titus", basis_text="see some-other-domain.com")
    set_manual_domain(db_session, "Titus", "titus-hvac.com")
    db_session.refresh(line)
    assert candidate_domains(line) == ["titus-hvac.com"]


# ---- competitor-name validation: closed set, never invented ---------------

def test_validate_competitor_names_drops_anything_not_on_the_map():
    candidates = ["Trane", "Carrier"]
    offered = [
        {"competitor_name": "Trane", "why_we_lose": "x", "why_we_win": "y"},
        {"competitor_name": "Daikin", "why_we_lose": "x", "why_we_win": "y"},  # not on the map
    ]
    kept = _validate_competitor_names(offered, candidates)
    assert [c["competitor_name"] for c in kept] == ["Trane"]


def test_validate_competitor_names_is_case_insensitive():
    kept = _validate_competitor_names(
        [{"competitor_name": "trane", "why_we_lose": "x", "why_we_win": "y"}], ["Trane"])
    assert len(kept) == 1


# ---- generate_one_line: mocked LLM + mocked fetch --------------------------

def _fake_raw(competitors=None):
    return {
        "what_it_is": "A rooftop unit line.",
        "where_it_fits": "Air handling.",
        "typical_project_types": "Commercial retrofits.",
        "elevator_pitch": "It's a solid rooftop unit line for commercial retrofits.",
        "differentiators": ["Factory-built to order."],
        "engineer_questions": ["What's your tonnage requirement?"],
        "competitors": competitors or [],
    }


def test_generate_one_line_restricts_candidates_to_matching_role(db_session, cfg, monkeypatch):
    line = _line(db_session, name="AAON", building_role="air_handling")
    _competitor(db_session, "Trane", "air_handling")
    _competitor(db_session, "Greenheck", "fans_ventilation")  # different role -- must not be offered

    captured = {}

    def fake_generate(user_content):
        captured["content"] = user_content
        return _fake_raw()

    monkeypatch.setattr(line_pitch, "generate_line_pitch", fake_generate)
    _mock_fetched(monkeypatch)

    result = generate_one_line(db_session, cfg, line)
    assert result["ok"] is True
    assert "Trane" in captured["content"]
    assert "Greenheck" not in captured["content"]


def test_generate_one_line_excludes_the_line_itself_from_its_own_candidates(db_session, cfg, monkeypatch):
    """A manufacturer whose own name also sits on the competitor map for
    the same role (a real, documented case in app.competitors -- Twin City
    Fan, Panasonic, etc.) must never be offered as its own competitor."""
    line = _line(db_session, name="Panasonic", building_role="fans_ventilation")
    _competitor(db_session, "Panasonic", "fans_ventilation")
    _competitor(db_session, "Greenheck", "fans_ventilation")

    captured = {}
    monkeypatch.setattr(line_pitch, "generate_line_pitch",
                        lambda content: captured.setdefault("content", content) or _fake_raw())
    _mock_fetched(monkeypatch)

    generate_one_line(db_session, cfg, line)
    assert "Greenheck" in captured["content"]
    assert "Panasonic" not in captured["content"].split("Candidate competitors")[1]


def test_generate_one_line_no_domain_means_no_fetch_attempted(db_session, cfg, monkeypatch):
    line = _line(db_session, name="AAON")
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])

    def boom(domain):
        raise AssertionError("fetch_product_pages must not be called with no candidate domain")
    monkeypatch.setattr(line_pitch, "fetch_product_pages", boom)
    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: _fake_raw())

    result = generate_one_line(db_session, cfg, line)
    assert result["fetch_status"] == "no_domain_found"
    assert result["page_text"] is None


def test_generate_one_line_records_robots_disallowed(db_session, cfg, monkeypatch):
    line = _line(db_session, name="AAON", basis_text="see aaon.com for details")
    monkeypatch.setattr(line_pitch, "fetch_product_pages", lambda d: (None, None, "robots_disallowed", []))
    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: _fake_raw())

    result = generate_one_line(db_session, cfg, line)
    assert result["fetch_status"] == "robots_disallowed"


def test_generate_one_line_propagates_budget_exceeded(db_session, cfg, monkeypatch):
    from app.spend import BudgetExceeded
    line = _line(db_session, name="AAON")
    _mock_fetched(monkeypatch)

    def boom(content):
        raise BudgetExceeded("out of budget")
    monkeypatch.setattr(line_pitch, "generate_line_pitch", boom)

    with pytest.raises(BudgetExceeded):
        generate_one_line(db_session, cfg, line)


def test_generate_one_line_no_page_fetched_skips_the_llm_entirely(db_session, cfg, monkeypatch):
    """Per the 2026-09-03 rewrite: a line with no fetched page never
    reaches the LLM at all -- there is no page to write a grounded
    capability claim against, so none is attempted. This replaces round
    1's behavior of writing a full LLM pitch and then dropping every claim
    (the Aldes case)."""
    line = _line(db_session, name="Aldes", building_role="fans_ventilation")
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])

    def boom(content):
        raise AssertionError("the LLM must never be called with no fetched page")
    monkeypatch.setattr(line_pitch, "generate_line_pitch", boom)

    result = generate_one_line(db_session, cfg, line)
    assert result["ok"] is True
    assert result["pitch_scope"] == "line_row_only"
    assert result["model"] is None
    assert result["raw"]["elevator_pitch"] is None
    assert result["raw"]["differentiators"] == []


def test_generate_one_line_falls_back_to_line_row_only_on_llm_error(db_session, cfg, monkeypatch):
    """A page WAS fetched, but the LLM call itself raised -- falls back to
    the deterministic line_row_only template instead of returning ok=False,
    per the 2026-09-03 rewrite (one bad LLM call must not surface as a
    failed line when a safe restricted pitch is always available)."""
    line = _line(db_session, name="AAON")
    _mock_fetched(monkeypatch)

    def boom(content):
        raise ValueError("model returned malformed JSON")
    monkeypatch.setattr(line_pitch, "generate_line_pitch", boom)

    result = generate_one_line(db_session, cfg, line)
    assert result["ok"] is True
    assert result["pitch_scope"] == "line_row_only"
    assert result["fetch_status"] == "fetch_failed"
    assert "malformed JSON" in result["llm_error"]


# ---- run_line_pitch_generation: skip logic, propose-never-assert -----------

def test_run_skips_existence_verified_false(db_session, cfg, monkeypatch):
    _line(db_session, name="Real Line", existence_verified=True)
    _line(db_session, name="Fake Line", existence_verified=False)
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])
    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: _fake_raw())

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    assert stats["lines_generated"] == 1
    assert stats["lines_skipped_existence_false"] == 1
    pitches = db_session.exec(select(LinePitch)).all()
    assert len(pitches) == 1


def test_run_never_touches_an_already_confirmed_pitch(db_session, cfg, monkeypatch):
    line = _line(db_session, name="AAON")
    existing = LinePitch(product_line_id=line.id, what_it_is="Hand-confirmed text.",
                        review_status="confirmed", reviewed_by="Andrew")
    db_session.add(existing)
    db_session.commit()

    def boom(content):
        raise AssertionError("must not regenerate a confirmed pitch")
    monkeypatch.setattr(line_pitch, "generate_line_pitch", boom)
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    assert stats["lines_skipped_already_confirmed"] == 1
    pitch = db_session.exec(select(LinePitch).where(LinePitch.product_line_id == line.id)).one()
    assert pitch.what_it_is == "Hand-confirmed text."  # untouched


def test_run_regenerates_a_rejected_pitch(db_session, cfg, monkeypatch):
    """A rejected pitch is a signal to try again, not a permanent no."""
    line = _line(db_session, name="AAON")
    existing = LinePitch(product_line_id=line.id, what_it_is="Old bad text.",
                        review_status="rejected", reviewed_by="Andrew")
    db_session.add(existing)
    db_session.commit()

    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: _fake_raw())
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    assert stats["lines_generated"] == 1
    pitch = db_session.exec(select(LinePitch).where(LinePitch.product_line_id == line.id)).one()
    assert pitch.review_status == "draft"
    assert pitch.what_it_is != "Old bad text."


def test_run_never_overwrites_a_confirmed_or_rejected_competitor_row(db_session, cfg, monkeypatch):
    line = _line(db_session, name="AAON", building_role="air_handling")
    _competitor(db_session, "Trane", "air_handling")
    existing_row = LineCompetitor(product_line_id=line.id, competitor_name="Trane",
                                  why_we_win="Hand-confirmed win reason.", review_status="confirmed",
                                  reviewed_by="Andrew")
    db_session.add(existing_row)
    db_session.commit()

    _mock_fetched(monkeypatch)
    monkeypatch.setattr(line_pitch, "generate_line_pitch",
                        lambda content: _fake_raw(competitors=[
                            {"competitor_name": "Trane", "why_we_lose": "new", "why_we_win": "new"}]))

    run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    row = db_session.exec(
        select(LineCompetitor).where(LineCompetitor.product_line_id == line.id,
                                     LineCompetitor.competitor_name == "Trane")).one()
    assert row.why_we_win == "Hand-confirmed win reason."  # untouched


def test_run_writes_evidence_url_from_the_map_not_the_llm(db_session, cfg, monkeypatch):
    line = _line(db_session, name="AAON", building_role="air_handling")
    _competitor(db_session, "Trane", "air_handling", source_url="https://real-rep-card.example/trane")

    _mock_fetched(monkeypatch)
    monkeypatch.setattr(line_pitch, "generate_line_pitch",
                        lambda content: _fake_raw(competitors=[
                            {"competitor_name": "Trane", "why_we_lose": "x", "why_we_win": "y"}]))

    run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    row = db_session.exec(select(LineCompetitor).where(LineCompetitor.product_line_id == line.id)).one()
    assert row.evidence_url == "https://real-rep-card.example/trane"
    assert row.review_status == "draft"


def test_run_stops_spending_once_budget_exhausted_but_does_not_crash(db_session, cfg, monkeypatch):
    from app.spend import BudgetExceeded
    _line(db_session, name="Line A")
    _line(db_session, name="Line B")
    _line(db_session, name="Line C")

    calls = {"n": 0}
    def fake_generate(content):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise BudgetExceeded("cap hit")
        return _fake_raw()
    _mock_fetched(monkeypatch)
    monkeypatch.setattr(line_pitch, "generate_line_pitch", fake_generate)

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    assert stats["lines_generated"] == 1
    assert stats["lines_skipped_budget"] == 2


def test_run_recovers_the_session_after_a_write_failure_and_keeps_going(db_session, cfg, monkeypatch):
    """Real production failure (2026-09-04): a write-step exception left
    the session's transaction invalid, and every SUBSEQUENT line's mere
    `line.id` access then raised PendingRollbackError, crashing a 64-line
    run after only 12 lines. The write step must be wrapped the same way
    the LLM call already is, with an explicit session.rollback() so the
    NEXT line's processing (including the confirmed_line_ids skip check
    and generate_one_line) can use the session again."""
    _line(db_session, name="Line A")
    _line(db_session, name="Line B")
    _mock_fetched(monkeypatch)
    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: _fake_raw())

    calls = {"n": 0}
    real_write = line_pitch._write_pitch_and_competitors

    def flaky_write(session, line, result):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated dropped connection mid-commit")
        return real_write(session, line, result)
    monkeypatch.setattr(line_pitch, "_write_pitch_and_competitors", flaky_write)

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    assert stats["lines_errored"] == 1
    assert stats["lines_generated"] == 1
    assert "simulated dropped connection" in stats["errors"][0]["error"]


def test_run_reports_sample_pitches_and_totals(db_session, cfg, monkeypatch):
    _line(db_session, name="AAON")
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])
    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: _fake_raw())

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    assert len(stats["sample_pitches"]) == 1
    assert stats["sample_pitches"][0]["line"] == "AAON"
    assert "cost_usd" in stats


def test_run_only_line_ids_skips_every_other_eligible_line(db_session, cfg, monkeypatch):
    """A targeted rerun (scout generate-line-pitches --only-lines) must
    leave every OTHER eligible line's existing draft completely untouched
    -- not regenerated, not even attempted."""
    line_a = _line(db_session, name="AAON")
    line_b = _line(db_session, name="Airzone")
    existing_b = LinePitch(product_line_id=line_b.id, what_it_is="Untouched Airzone text.",
                           review_status="draft")
    db_session.add(existing_b)
    db_session.commit()

    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])

    def boom(content):
        raise AssertionError("must not be called for a line outside only_line_ids")
    monkeypatch.setattr(line_pitch, "generate_line_pitch", boom)

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0, only_line_ids={line_a.id})
    assert stats["lines_total"] == 1
    assert stats["lines_generated"] == 1
    pitch_b = db_session.exec(select(LinePitch).where(LinePitch.product_line_id == line_b.id)).one()
    assert pitch_b.what_it_is == "Untouched Airzone text."


def test_run_web_search_stats_are_tallied(db_session, cfg, monkeypatch):
    _line(db_session, name="AAON")
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])
    monkeypatch.setattr(line_pitch, "discover_domain_via_web_search",
                        lambda cfg, line: {"domain": "aaon.com", "url": "https://aaon.com/",
                                          "page_text": "AAON page text.", "fetch_status": "fetched",
                                          "searched": True, "cost_usd": 0.02})
    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: _fake_raw())

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    assert stats["web_search_attempted"] == 1
    assert stats["web_search_domain_found"] == 1
    assert stats["web_search_domain_not_found"] == 0
    assert stats["web_search_cost_usd"] == 0.02


def test_generate_one_line_web_search_fallback_propagates_budget_exceeded(db_session, cfg, monkeypatch):
    from app.spend import BudgetExceeded
    line = _line(db_session, name="AAON")
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])

    def boom(cfg, line):
        raise BudgetExceeded("cap hit")
    monkeypatch.setattr(line_pitch, "discover_domain_via_web_search", boom)

    with pytest.raises(BudgetExceeded):
        generate_one_line(db_session, cfg, line)


def test_run_reports_line_row_only_count(db_session, cfg, monkeypatch):
    """A line with no fetched page is counted as line_row_only in the run
    stats, per the brief's explicit 'lines on line_row_only' report field."""
    _line(db_session, name="Aldes", building_role="fans_ventilation")
    monkeypatch.setattr(line_pitch, "candidate_domains", lambda l: [])

    def boom(content):
        raise AssertionError("no LLM call for a line with no fetched page")
    monkeypatch.setattr(line_pitch, "generate_line_pitch", boom)

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    assert stats["lines_line_row_only"] == 1
    assert stats["lines_insufficient"] == 0
    assert stats["per_line_claims"][0]["pitch_scope"] == "line_row_only"


def test_run_reports_insufficient_when_every_full_claim_is_dropped(db_session, cfg, monkeypatch):
    """A 'full' pitch (a page WAS fetched) where every field's claims fail
    grounding leaves nothing to show -- reported as insufficient, not a
    bare unlabeled fragment (the Aldes bug from round 1)."""
    _line(db_session, name="AAON")
    _mock_fetched(monkeypatch, page_text="This page has no matching numbers at all.")
    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: {
        "what_it_is": "Rated for 900 tons of fabricated capacity.",
        "where_it_fits": "",
        "typical_project_types": "",
        "elevator_pitch": "",
        "differentiators": ["Rated for 900 tons."],
        "engineer_questions": [],
        "competitors": [],
    })

    stats = run_line_pitch_generation(db_session, cfg, cap_usd=5.0)
    assert stats["lines_insufficient"] == 1
    assert stats["lines_line_row_only"] == 0


# ---- line_row_only_pitch: deterministic template, no LLM call -------------

def test_line_row_only_pitch_lists_confirmed_branches(db_session):
    line = _line(db_session, name="Aldes", building_role="fans_ventilation")
    db_session.add(ProductLineBranch(product_line_id=line.id, branch="DMG Los Angeles",
                                     status="confirmed_covered", source_detail="test"))
    db_session.add(ProductLineBranch(product_line_id=line.id, branch="DMG San Diego",
                                     status="confirmed_not_covered", source_detail="test"))
    db_session.commit()

    raw = line_row_only_pitch(db_session, line)
    assert "DMG Los Angeles" in raw["where_it_fits"]
    assert "DMG San Diego" not in raw["where_it_fits"]
    assert raw["elevator_pitch"] is None
    assert raw["differentiators"] == []
    assert raw["competitors"] == []


def test_line_row_only_pitch_handles_no_confirmed_branches(db_session):
    line = _line(db_session, name="Aldes", building_role="fans_ventilation")
    raw = line_row_only_pitch(db_session, line)
    assert "Not yet confirmed" in raw["where_it_fits"]


# ---- _write_pitch_and_competitors: pitch_scope, capped differentiator -----

def test_write_pitch_sets_line_row_only_scope_and_writes_no_competitors(db_session, cfg):
    from app.pipeline.line_pitch import _write_pitch_and_competitors
    line = _line(db_session, name="Aldes", building_role="fans_ventilation")
    result = generate_one_line(db_session, cfg, line)
    write_stats = _write_pitch_and_competitors(db_session, line, result)
    assert write_stats["pitch_scope"] == "line_row_only"
    assert write_stats["competitors_written"] == 0
    pitch = db_session.exec(select(LinePitch).where(LinePitch.product_line_id == line.id)).one()
    assert pitch.pitch_scope == "line_row_only"


def test_write_pitch_caps_differentiators_and_questions_to_one(db_session, cfg, monkeypatch):
    from app.pipeline.line_pitch import _write_pitch_and_competitors
    line = _line(db_session, name="AAON")
    _mock_fetched(monkeypatch, page_text=PAGE)
    monkeypatch.setattr(line_pitch, "generate_line_pitch", lambda content: {
        "what_it_is": "A rooftop unit line.",
        "where_it_fits": "Air handling.",
        "typical_project_types": "Commercial retrofits.",
        "elevator_pitch": "A solid rooftop unit line.",
        "differentiators": ["AHRI certified across the line.", "Also fast lead times."],
        "engineer_questions": ["What's your tonnage requirement?", "What's your voltage?"],
        "competitors": [],
    })
    result = generate_one_line(db_session, cfg, line)
    write_stats = _write_pitch_and_competitors(db_session, line, result)
    pitch = db_session.exec(select(LinePitch).where(LinePitch.product_line_id == line.id)).one()
    assert len(pitch.differentiators) <= 1
    assert len(pitch.engineer_questions) <= 1
    assert write_stats["pitch_scope"] == "full"
