"""Mock scholarly-API responses for offline tests.

Response SHAPES follow the real Crossref, OpenAlex, arXiv and PubMed APIs.
Metadata (titles, authors, years, retraction status) reflects the real works.
Abstract texts are SYNTHETIC stand-ins written for testing — not the
publishers' abstracts — so tests don't depend on copyrighted text.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

ABSTRACT_LECUN = ("Deep learning allows computational models that are composed of multiple "
                  "processing layers to learn representations of data with multiple levels of "
                  "abstraction. These methods have improved the state of the art in speech "
                  "recognition, visual object recognition and object detection.")
ABSTRACT_VASWANI = ("We propose the Transformer, a network architecture based solely on attention "
                    "mechanisms, dispensing with recurrence and convolutions entirely. Experiments "
                    "on machine translation show these models are superior in quality.")
ABSTRACT_SILVER = ("We introduce a new approach to computer Go that uses value networks and policy "
                   "networks trained by supervised and reinforcement learning, combined with tree search.")
ABSTRACT_PAGE = ("The PRISMA 2020 statement updates the 2009 guideline and provides guidance for "
                 "reporting systematic reviews, including a 27-item checklist.")

CROSSREF = {
    "10.1038/nature14539": {"DOI": "10.1038/nature14539", "title": ["Deep learning"],
                            "author": [{"family": "LeCun"}, {"family": "Bengio"}, {"family": "Hinton"}],
                            "issued": {"date-parts": [[2015, 5, 27]]}, "URL": "https://doi.org/10.1038/nature14539"},
    "10.1016/s0140-6736(97)11096-0": {
        "DOI": "10.1016/S0140-6736(97)11096-0",
        "title": ["Ileal-lymphoid-nodular hyperplasia, non-specific colitis, and pervasive developmental disorder in children"],
        "author": [{"family": "Wakefield"}, {"family": "Murch"}, {"family": "Anthony"}],
        "issued": {"date-parts": [[1998, 2]]},
        "updated-by": [{"DOI": "10.1016/S0140-6736(10)60175-4", "type": "retraction"}],
        "URL": "https://doi.org/10.1016/S0140-6736(97)11096-0"},
    "10.1038/nature16961": {"DOI": "10.1038/nature16961",
                            "title": ["Mastering the game of Go with deep neural networks and tree search"],
                            "author": [{"family": "Silver"}, {"family": "Huang"}, {"family": "Maddison"}],
                            "issued": {"date-parts": [[2016, 1, 27]]}, "URL": "https://doi.org/10.1038/nature16961"},
}

OPENALEX = {
    "10.1038/nature14539": {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1038/nature14539",
                            "title": "Deep learning", "publication_year": 2015, "is_retracted": False,
                            "authorships": [{"author": {"display_name": "Yann LeCun"}}],
                            "abstract": ABSTRACT_LECUN},
    "10.1016/s0140-6736(97)11096-0": {"id": "https://openalex.org/W4", "title": CROSSREF["10.1016/s0140-6736(97)11096-0"]["title"][0],
                                      "publication_year": 1998, "is_retracted": True,
                                      "authorships": [{"author": {"display_name": "Andrew Wakefield"}}]},
    "10.1038/nature16961": {"id": "https://openalex.org/W5", "doi": "https://doi.org/10.1038/nature16961",
                            "title": "Mastering the game of Go with deep neural networks and tree search",
                            "publication_year": 2016, "is_retracted": False,
                            "authorships": [{"author": {"display_name": "David Silver"}}],
                            "abstract": ABSTRACT_SILVER},
}

PRISMA = {"id": "https://openalex.org/W6", "doi": "https://doi.org/10.1136/bmj.n71",
          "title": "The PRISMA 2020 statement: an updated guideline for reporting systematic reviews",
          "publication_year": 2021, "is_retracted": False,
          "authorships": [{"author": {"display_name": "Matthew J. Page"}}], "abstract": ABSTRACT_PAGE}

ARXIV_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/1706.03762v7</id><published>2017-06-12T17:57:34Z</published>
<title>Attention Is All You Need</title><summary>{ABSTRACT_VASWANI}</summary>
<author><name>Ashish Vaswani</name></author><author><name>Noam Shazeer</name></author>
</entry></feed>"""


# DOIs registered with agencies other than Crossref (synthetic, test-only)
DOI_ELSEWHERE = {
    "10.5281/zenodo.0000001": {"ra": "DataCite", "csl": {
        "type": "dataset", "title": "UK heat network performance dataset 2015-2023",
        "author": [{"family": "Akande", "given": "T."}], "issued": {"date-parts": [[2024, 3]]},
        "abstract": "<p>Metered heat demand for 120 UK heat networks.</p>"}},
    "10.99999/registered.nometa": {"ra": "mEDRA", "csl": None},
}

# web pages for link checks (synthetic)
WEB = {
    "https://www.gov.uk/government/publications/powering-up-britain":
        (200, "text/html; charset=utf-8", "<html><head><title>Powering up Britain - GOV.UK</title></head>"
                                          "<body>" + "x" * 1000 + "</body></html>"),
    "https://www.example.org/report.pdf": (200, "application/pdf", "%PDF-1.7 ..."),
    "https://www.example.org/old-report": (404, "text/html", "<title>Page not found</title>"),
    "https://cloudflare.example.org/report": (403, "text/html", "<title>Just a moment...</title>"),
    "https://www.example.org/moved": (301, "text/html", "https://www.gov.uk/government/publications/powering-up-britain"),
    "https://www.example.org/wrong-page": (200, "text/html", "<title>Annual accounts 2022 | Example Ltd</title>"),
}


def _inverted(text: str) -> dict:
    inv: dict[str, list[int]] = {}
    for i, w in enumerate(text.split()):
        inv.setdefault(w, []).append(i)
    return inv


def _oa(work: dict) -> dict:
    w = dict(work)
    if "abstract" in w:
        w["abstract_inverted_index"] = _inverted(w.pop("abstract"))
    return w


def make_transport(llm_handler=None, fail_hosts: set[str] | None = None) -> httpx.MockTransport:
    fail_hosts = fail_hosts or set()

    def handler(request: httpx.Request) -> httpx.Response:
        u = urlparse(str(request.url))
        q = parse_qs(u.query)
        if u.netloc in fail_hosts:
            raise httpx.ConnectError("simulated outage", request=request)
        if u.netloc == "api.crossref.org":
            if u.path == "/works":
                return httpx.Response(200, json={"message": {"items": []}})
            doi = unquote(u.path[len("/works/"):]).lower()
            if doi in CROSSREF:
                return httpx.Response(200, json={"status": "ok", "message": CROSSREF[doi]})
            return httpx.Response(404, text="Resource not found.")
        if u.netloc == "api.openalex.org":
            if u.path == "/works":
                s = (q.get("search") or [""])[0].lower()
                results = [_oa(PRISMA)] if "prisma" in s else []
                return httpx.Response(200, json={"results": results})
            doi = unquote(u.path[len("/works/doi:"):]).lower()
            if doi in OPENALEX:
                return httpx.Response(200, json=_oa(OPENALEX[doi]))
            return httpx.Response(404, json={"error": "not found"})
        if u.netloc == "export.arxiv.org":
            if (q.get("id_list") or [""])[0] == "1706.03762":
                return httpx.Response(200, text=ARXIV_FEED)
            if "attention is all you need" in (q.get("search_query") or [""])[0].lower():
                return httpx.Response(200, text=ARXIV_FEED)
            return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
        if u.netloc == "localhost:11434":
            if u.path == "/api/tags":
                return httpx.Response(200, json={"models": [
                    {"name": "llama3.1:8b", "model": "llama3.1:8b", "digest": "sha256-test",
                     "details": {"parameter_size": "8.0B", "quantization_level": "Q4_K_M"}}]})
            if u.path == "/api/chat" and llm_handler:
                return llm_handler(json.loads(request.content))
        if u.netloc == "doi.org":
            if u.path.startswith("/api/handles/"):
                doi = unquote(u.path[len("/api/handles/"):]).lower()
                if doi in DOI_ELSEWHERE or doi in CROSSREF:
                    return httpx.Response(200, json={"responseCode": 1, "handle": doi})
                return httpx.Response(404, json={"responseCode": 100})
            if u.path.startswith("/ra/"):
                doi = unquote(u.path[len("/ra/"):]).lower()
                if doi in DOI_ELSEWHERE:
                    return httpx.Response(200, json=[{"DOI": doi, "RA": DOI_ELSEWHERE[doi]["ra"]}])
                return httpx.Response(200, json=[{"DOI": doi, "status": "DOI does not exist"}])
            doi = unquote(u.path[1:]).lower()
            entry = DOI_ELSEWHERE.get(doi)
            if entry and entry["csl"] and "csl+json" in request.headers.get("accept", ""):
                return httpx.Response(200, json=entry["csl"])
            return httpx.Response(406)
        key = str(request.url)
        if key in WEB:
            status, ctype, body = WEB[key]
            if status in (301, 302):
                return httpx.Response(status, headers={"location": body})
            return httpx.Response(status, headers={"content-type": ctype}, content=body.encode())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def llm_reply(verdict: str, quote: str, rationale: str = "Scripted demo verdict") -> httpx.Response:
    text = json.dumps({"verdict": verdict, "evidence_quote": quote, "rationale": rationale})
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}, "done": True})


def scripted_llm(payload: dict) -> httpx.Response:
    """Deterministic stand-in for the model, keyed on the claim text."""
    assert payload["format"]["required"] == ["verdict", "evidence_quote", "rationale"]
    assert payload["options"]["temperature"] == 0
    prompt = payload["messages"][-1]["content"]
    claim = re.search(r"<claim>\n(.*?)\n</claim>", prompt, re.S).group(1)
    if claim.startswith("Deep learning allows"):
        return llm_reply("supported", "Deep learning allows computational models that are composed "
                                      "of multiple processing layers to learn representations of data")
    if claim.startswith("Deep learning has eliminated"):
        return llm_reply("supported", "Deep learning removes the need for feature engineering",  # fabricated quote
                         "The source says deep learning removes feature engineering.")
    if claim.startswith("The Transformer"):
        return llm_reply("supported", "based solely on attention mechanisms, dispensing with "
                                      "recurrence and convolutions entirely")
    if claim.startswith("Residual"):
        return llm_reply("not_supported", "", "Source is about computer Go, not residual connections.")
    if claim.startswith("Reporting guidance"):
        return llm_reply("supported", "updates the 2009 guideline")
    return llm_reply("insufficient_evidence", "")
