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
        monkeypatch.setattr("rra.existence._search", lambda h, r: (None, None))
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
