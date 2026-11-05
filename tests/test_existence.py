"""Reference verdicts for sources outside Crossref, OpenAlex, arXiv and PubMed."""

import httpx
import pytest

from rra.audit import AuditLog
from rra.existence import verify_reference
from rra.http import CachedClient
from rra.models import Claim, ExistenceVerdict as V
from rra.parse import parse_references
from rra.support import HeuristicChecker

from fixtures import make_transport


@pytest.fixture
def check(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    http = CachedClient(tmp_path / "cache", audit, transport=make_transport())

    def run(line: str):
        ref = parse_references(f"1. {line}")[0]
        return verify_reference(http, audit, ref)
    run.http, run.audit, run.tmp = http, audit, tmp_path
    return run


def test_datacite_doi_is_verified_not_missing(check):
    r = check("Akande T. (2024). UK heat network performance dataset 2015-2023. Zenodo. "
              "https://doi.org/10.5281/zenodo.0000001")
    assert r.verdict == V.VERIFIED
    assert r.record.source == "datacite" and r.record.abstract.startswith("Metered heat")
    assert "registered with DataCite" in r.issues[0]


def test_registered_doi_without_metadata_exists_unindexed(check):
    r = check("Rossi M. (2021). Italian report on district heating. doi:10.99999/registered.nometa")
    assert r.verdict == V.EXISTS_UNINDEXED and "mEDRA" in r.issues[0]


def test_unregistered_doi_is_not_found(check):
    r = check("Smith J. (2021). A paper. J 4, 1-12. doi:10.9999/fake.2021.001")
    assert r.verdict == V.NOT_FOUND and "doi.org" in r.issues[0]


def test_live_link_exists_unindexed_and_ignores_site_suffix(check):
    r = check("DESNZ (2023). Powering up Britain. Available at: "
              "https://www.gov.uk/government/publications/powering-up-britain (Accessed 3 May 2024).")
    assert r.verdict == V.EXISTS_UNINDEXED
    assert r.title_similarity == 1.0
    assert not any("doesn't match" in i for i in r.issues)


def test_redirect_is_followed_and_reported(check):
    r = check("DESNZ (2023). Powering up Britain. https://www.example.org/moved")
    assert r.verdict == V.EXISTS_UNINDEXED and any("Redirected to https://www.gov.uk" in i for i in r.issues)


def test_link_to_wrong_page_is_flagged(check):
    r = check("DESNZ (2023). Powering up Britain. https://www.example.org/wrong-page")
    assert r.verdict == V.EXISTS_UNINDEXED and any("doesn't match" in i for i in r.issues)


def test_pdf_link(check):
    r = check("Ofgem (2022). Decarbonisation action plan. https://www.example.org/report.pdf")
    assert r.verdict == V.EXISTS_UNINDEXED and "a PDF" in r.issues[0]


def test_dead_link_is_not_found_with_archive_hint(check):
    r = check("Council (2019). Local energy plan. https://www.example.org/old-report")
    assert r.verdict == V.NOT_FOUND and any("web.archive.org" in i for i in r.issues)


def test_bot_blocked_link_is_unchecked_not_missing(check):
    r = check("Agency (2020). Strategy. https://cloudflare.example.org/report")
    assert r.verdict == V.UNCHECKED and "403" in r.issues[0]


def test_local_and_private_links_are_never_fetched(tmp_path, monkeypatch):
    import rra.http
    def refuse(request):
        raise AssertionError(f"fetched {request.url}")
    audit = AuditLog(tmp_path / "a.jsonl")
    http = CachedClient(tmp_path / "c", audit, transport=httpx.MockTransport(refuse))
    for url in ["http://127.0.0.1:11434/api/tags", "http://localhost:8000/api/runs"]:
        ref = parse_references(f"1. Ollama (2024). Local service. {url}")[0]
        ref.title = None
        # search APIs are also refused above, so stub them for this test
        monkeypatch.setattr("rra.existence._search", lambda h, r: (None, None, []))
        assert verify_reference(http, audit, ref).verdict == V.UNCHECKED
    monkeypatch.setattr(rra.http, "_resolve", lambda host: ["10.1.2.3"])
    ref = parse_references("1. Intranet (2024). Internal page. https://intranet.example.com/x")[0]
    r = verify_reference(http, audit, ref)
    assert r.verdict == V.UNCHECKED and "private" in r.issues[0]


def test_grey_literature_without_link_is_unverifiable(check):
    r = check("Climate Change Committee (2023). Progress in reducing emissions: 2023 Report to "
              "Parliament. London: CCC.")
    assert r.verdict == V.UNVERIFIABLE


def test_article_shaped_reference_with_nothing_found_stays_red(check):
    r = check("Jones P, Lee K. (2020). Neural citation checking at scale. Journal of Imaginary "
              "Results 12(3), 45–67.")
    assert r.verdict == V.NOT_FOUND and "fabricated" in r.issues[0]


def test_arxiv_url_is_read_as_arxiv_id(check):
    r = check("Vaswani A, et al. (2017). Attention is all you need. https://arxiv.org/abs/1706.03762v5")
    assert r.reference.arxiv_id == "1706.03762" and r.verdict == V.VERIFIED


def test_link_check_is_cached_for_offline_replay(check, tmp_path):
    check("DESNZ (2023). Powering up Britain. https://www.gov.uk/government/publications/powering-up-britain")
    offline = CachedClient(check.tmp / "cache", check.audit, offline=True)
    ref = parse_references("1. DESNZ (2023). Powering up Britain. "
                           "https://www.gov.uk/government/publications/powering-up-britain")[0]
    assert verify_reference(offline, check.audit, ref).verdict == V.EXISTS_UNINDEXED


def test_claim_against_web_source_says_check_by_hand(check):
    r = check("DESNZ (2023). Powering up Britain. https://www.gov.uk/government/publications/powering-up-britain")
    s = HeuristicChecker().check(Claim(id="C1", text="The UK will do X [1].", cited_keys=["1"]), "1", r.record)
    assert s.verdict.value == "insufficient_evidence" and "Open the link" in s.rationale


# ---------- rate limits ----------

def _client(tmp_path, handler, monkeypatch=None):
    audit = AuditLog(tmp_path / "audit.jsonl")
    return CachedClient(tmp_path / "cache", audit, transport=httpx.MockTransport(handler)), audit


def test_429_is_retried_with_backoff(tmp_path, monkeypatch):
    import rra.http as h
    waits, calls = [], []
    monkeypatch.setattr(h, "_sleep", waits.append)

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json={"results": []})
    http, audit = _client(tmp_path, handler)
    status, _ = http.get("https://api.openalex.org/works", params={"search": "x"})
    assert status == 200 and waits == [2.0, 2.0] and len(calls) == 3
    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8").count("http_retry") == 2


def test_daily_limit_is_not_waited_for(tmp_path, monkeypatch):
    import rra.http as h
    waits = []
    monkeypatch.setattr(h, "_sleep", waits.append)
    http, _ = _client(tmp_path, lambda req: httpx.Response(429, headers={"x-ratelimit-reset": "36000"}))
    status, _ = http.get("https://api.openalex.org/works", params={"search": "x"})
    assert status == 429 and waits == []


def test_openalex_failure_falls_back_to_crossref(tmp_path):
    item = {"DOI": "10.1111/x.1", "title": ["Using thematic analysis in psychology"],
            "author": [{"family": "Braun"}, {"family": "Clarke"}], "issued": {"date-parts": [[2006]]}}

    def handler(req):
        if req.url.host == "api.openalex.org":
            return httpx.Response(429, headers={"x-ratelimit-reset": "36000"})
        if req.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": [item]}})
        return httpx.Response(404)
    http, audit = _client(tmp_path, handler)
    ref = parse_references("1. Braun, V. & Clarke, V. (2006) Using thematic analysis in psychology. "
                           "Qualitative Research in Psychology, 3 (2), 77–101.")[0]
    r = verify_reference(http, audit, ref)
    assert r.verdict == V.FOUND_BY_SEARCH and r.record.source == "crossref"


def test_all_databases_failing_is_unchecked_not_unverifiable(tmp_path):
    http, audit = _client(tmp_path, lambda req: httpx.Response(503))
    import rra.http as h
    h.MAX_RETRIES, saved = 0, h.MAX_RETRIES
    try:
        ref = parse_references("1. Climate Change Committee (2023). Progress report. London: CCC.")[0]
        r = verify_reference(http, audit, ref)
    finally:
        h.MAX_RETRIES = saved
    assert r.verdict == V.UNCHECKED and "OpenAlex" in r.issues[0] and "Crossref" in r.issues[0]


def test_openalex_key_is_sent_but_never_logged_or_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "secret-key-123")
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"results": []})
    http, _ = _client(tmp_path, handler)
    http.get("https://api.openalex.org/works", params={"search": "x"})
    assert "api_key=secret-key-123" in seen[0]
    assert "secret-key-123" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    for f in (tmp_path / "cache").iterdir():
        assert "secret-key-123" not in f.read_text(encoding="utf-8")


# ---------- author matching ----------

def test_corporate_author_is_not_a_mismatch():
    from rra.existence import compare
    from rra.models import SourceRecord
    ref = parse_references("1. Intergovernmental Panel on Climate Change [IPCC] (2023) Climate change 2022 – "
                           "impacts, adaptation and vulnerability. Cambridge University Press.")[0]
    rec = SourceRecord(source="openalex", identifier="x", title="Climate Change 2022 – Impacts, Adaptation and "
                       "Vulnerability", authors=["Change"], year=2023)
    issues, _ = compare(ref, rec)
    assert not any(i.startswith("First author") for i in issues)


def test_misspelt_authors_on_title_match_show_as_mismatch(tmp_path):
    item = {"DOI": "10.1000/lca.1", "title": ["LCA database creation: Current challenges and the way forward"],
            "author": [{"family": "Ciroth"}, {"family": "Di Noi"}, {"family": "Burhan"}, {"family": "Srocka"}],
            "issued": {"date-parts": [[2019]]}}

    def handler(req):
        if req.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": [item]}})
        return httpx.Response(200, json={"results": []})
    http, audit = _client(tmp_path, handler)
    ref = parse_references("1. Cirotha, A., Di Noia, C., Burhana, S. S. & Srockaa, M. (2019) LCA database "
                           "creation: Current challenges and the way forward. Indonesian Journal of Life "
                           "Cycle Assessment and Sustainability, 3 (2), 41–51.")[0]
    r = verify_reference(http, audit, ref)
    assert r.verdict == V.METADATA_MISMATCH
    assert any("Cirotha" in i for i in r.issues) and r.record.identifier == "10.1000/lca.1"


def test_openalex_key_is_redacted_from_network_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "secret-key-123")

    def handler(req):
        raise httpx.ConnectError(f"connection failed for {req.url}", request=req)
    http, audit = _client(tmp_path, handler)
    ref = parse_references("1. Braun, V. & Clarke, V. (2006) Using thematic analysis in psychology. "
                           "Qualitative Research in Psychology, 3 (2), 77–101.")[0]
    r = verify_reference(http, audit, ref)
    assert r.verdict == V.UNCHECKED
    assert "secret-key-123" not in " ".join(r.issues)
    assert "secret-key-123" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


# ---------- volume, issue, pages; titles ----------

def _crossref_only(tmp_path, item):
    def handler(req):
        if req.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": [item]}})
        return httpx.Response(200, json={"results": []})
    return _client(tmp_path, handler)


def test_volume_given_as_issue_is_flagged(tmp_path):
    item = {"DOI": "10.1021/es901643a", "title": ["Categorization of Scope 3 Emissions for Streamlined "
            "Enterprise Carbon Footprinting"], "author": [{"family": "Huang"}, {"family": "Weber"},
            {"family": "Matthews"}], "issued": {"date-parts": [[2009]]}, "volume": "43", "issue": "22",
            "page": "8509-8515"}
    http, audit = _crossref_only(tmp_path, item)
    ref = parse_references("1. Huang, Y. A., Weber, C. L. & Matthews, H. S. (2009) Categorization of scope 3 "
                           "emissions for streamlined enterprise carbon foot printing. Environ. Sci. Technol. "
                           "22, 8509–8515.")[0]
    r = verify_reference(http, audit, ref)
    assert r.verdict == V.METADATA_MISMATCH
    assert any("looks like the issue number" in i for i in r.issues)


def test_matching_volume_and_pages_stay_found(tmp_path):
    item = {"DOI": "10.1002/bse.723", "title": ["Modeling the Impacts of Corporate Commitment on Climate Change"],
            "author": [{"family": "Boiral"}, {"family": "Henri"}, {"family": "Talbot"}],
            "issued": {"date-parts": [[2012]]}, "volume": "21", "issue": "8", "page": "495-516"}
    http, audit = _crossref_only(tmp_path, item)
    ref = parse_references("1. Boiral, O., Henri, J. & Talbot, D. (2012) Modeling the impacts of corporate "
                           "commitment on climate change. Business Strategy and the Environment, 21 (8), 495–516.")[0]
    assert verify_reference(http, audit, ref).verdict == V.FOUND_BY_SEARCH


def test_book_subtitle_from_crossref_allows_match(tmp_path):
    item = {"DOI": "10.4324/9781315134543", "title": ["The Research Act"],
            "subtitle": ["A Theoretical Introduction to Sociological Methods"],
            "author": [{"family": "Denzin"}], "issued": {"date-parts": [[2017]]}}
    http, audit = _crossref_only(tmp_path, item)
    ref = parse_references("1. Denzin, N. K. (2017) The research act: A theoretical introduction to "
                           "sociological methods Routledge.")[0]
    r = verify_reference(http, audit, ref)
    assert r.verdict == V.FOUND_BY_SEARCH and r.record.identifier == "10.4324/9781315134543"


def test_short_title_inside_page_title_is_a_match(tmp_path):
    def handler(req):
        if req.url.host == "knowledge.bsigroup.com":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  content=b"<title>PAS 2050:2011 | 30 Sep 2011 | BSI Knowledge</title>")
        return httpx.Response(200, json={"results": [], "message": {"items": []}})
    http, audit = _client(tmp_path, handler)
    ref = parse_references("1. BSI (2011), PAS 2050, Available at: PAS 2050:2011 | 30 Sep 2011 | BSI Knowledge "
                           "https://knowledge.bsigroup.com/products/pas-2050. [Accessed Jul 3, 2025].")[0]
    r = verify_reference(http, audit, ref)
    assert r.verdict == V.EXISTS_UNINDEXED and not any("doesn't match" in i for i in r.issues)
