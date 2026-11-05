"""Stage 1 verifier: does each reference exist, and does it say what the
manuscript says it is (title, first author, year)? Is it retracted?"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

import httpx

from .audit import AuditLog
from .http import BlockedURL, CachedClient, OfflineMiss
from .models import ExistenceResult, ExistenceVerdict, Reference, SourceRecord
from . import sources

TITLE_MATCH = 0.85      # similarity needed to call titles a match
SEARCH_ACCEPT = 0.92    # stricter bar when we found the work by search, not by identifier
YEAR_TOLERANCE = 1      # online-first vs print year often differ by one


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def title_similarity(a: Optional[str], b: Optional[str]) -> Optional[float]:
    if not a or not b:
        return None
    na, nb = _norm(a), _norm(b)
    ratio = SequenceMatcher(None, na, nb).ratio()
    # tolerate subtitles: "Title" vs "Title: a subtitle"
    if na and nb and (na.startswith(nb) or nb.startswith(na)) and min(len(na), len(nb)) > 20:
        ratio = max(ratio, 0.95)
    return round(ratio, 3)


def _merge(primary: Optional[SourceRecord], secondary: Optional[SourceRecord]) -> Optional[SourceRecord]:
    """Crossref is the metadata authority; OpenAlex fills abstract and retraction gaps."""
    if primary is None:
        return secondary
    if secondary is None:
        return primary
    merged = primary.model_copy()
    if not merged.abstract and secondary.abstract:
        merged.abstract = secondary.abstract
        merged.source = f"{primary.source}+{secondary.source}"
    merged.is_retracted = primary.is_retracted or secondary.is_retracted
    return merged


def compare(ref: Reference, rec: SourceRecord) -> tuple[list[str], Optional[float]]:
    issues: list[str] = []
    sim = title_similarity(ref.title, rec.title)
    if ref.title is None:
        issues.append("Could not parse a title from the reference line; title not compared")
    elif sim is not None and sim < TITLE_MATCH:
        issues.append(f"Title mismatch (similarity {sim:.2f}): record says “{rec.title}”")
    if ref.year and rec.year and abs(ref.year - rec.year) > YEAR_TOLERANCE:
        issues.append(f"Year mismatch: reference says {ref.year}, record says {rec.year}")
    if ref.first_author and rec.authors:
        surnames = {_norm(a) for a in rec.authors if a}
        # the author part of the reference: everything before the year
        segment = _norm(re.split(r"\(?\b(?:19|20)\d{2}[a-z]?\b", ref.raw, maxsplit=1)[0])
        # corporate authors ("Intergovernmental Panel on Climate Change") are matched by any
        # record author appearing as a whole word; a misspelt surname ("Cirotha") still fails
        named = any(re.search(rf"\b{re.escape(a)}\b", segment) for a in surnames if a)
        if _norm(ref.first_author) not in surnames and not named:
            issues.append(f"First author “{ref.first_author}” not among record authors "
                          f"({', '.join(rec.authors[:4])}{'…' if len(rec.authors) > 4 else ''})")
    return issues, sim


LOOKUP_ERRORS = (httpx.HTTPError, sources.LookupError_, OfflineMiss)


def _attempt(fn, errors: list[str], name: str):
    """Run one database call; on failure record which database failed and carry on."""
    try:
        return fn()
    except LOOKUP_ERRORS as e:
        errors.append(f"{name} ({str(e)[:120]})")
        return None


def _search(http: CachedClient, ref: Reference) -> tuple[Optional[SourceRecord], Optional[float], list[str]]:
    """Title search across OpenAlex, Crossref and arXiv. One database failing (e.g. a rate
    limit) doesn't stop the others. Returns (best match, similarity, failed databases)."""
    query = ref.title or ref.raw
    errors: list[str] = []
    candidates: list[SourceRecord] = []
    candidates += _attempt(lambda: sources.openalex_search(http, query), errors, "OpenAlex") or []
    candidates += _attempt(lambda: sources.crossref_search(http, ref.raw), errors, "Crossref") or []
    if ref.title and not any((title_similarity(ref.title, c.title) or 0) >= SEARCH_ACCEPT for c in candidates):
        # preprints are often missing from the general indexes' top hits; ask arXiv directly
        candidates += _attempt(lambda: sources.arxiv_search(http, ref.title), errors, "arXiv") or []
    best, best_sim = None, 0.0
    for rec in candidates:
        sim = title_similarity(ref.title or ref.raw, rec.title) or 0.0
        if sim > best_sim:
            best, best_sim = rec, sim
    return best, (round(best_sim, 3) if best else None), errors


# A reference formatted like a journal or conference paper (volume and pages, or a
# journal/proceedings name). If no database has it and there is no DOI or link, that is
# the pattern of a fabricated reference, so it stays red. Reports, standards and
# legislation rarely match this, so they get "can't verify" instead.
ARTICLE_SHAPE = re.compile(
    r"\b\d{1,4}\s*\(\s*\d{1,4}\s*\)\s*[,:]?\s*[A-Za-z]?\d+"      # 12(3), 45  /  4(2):e101
    r"|\b\d{1,4}\s*[,:]\s*(?:pp?\.\s*)?\d{1,6}\s*[-–]\s*\d{1,6}\b"   # 521, 436–444
    r"|\b(?:vol\.|volume)\s*\d|\bpp?\.\s*\d"
    r"|\b(?:journal|proceedings|conference|transactions|symposium)\b",
    re.IGNORECASE)


def looks_like_article(raw: str) -> bool:
    return bool(ARTICLE_SHAPE.search(re.sub(r"https?://\S+", "", raw)))


def _judge(ref: Reference, rec: SourceRecord) -> ExistenceResult:
    issues, sim = compare(ref, rec)
    if rec.is_retracted:
        verdict = ExistenceVerdict.RETRACTED
        issues.insert(0, "Work is flagged as RETRACTED")
    elif any(i.startswith(("Title mismatch", "Year mismatch", "First author")) for i in issues):
        verdict = ExistenceVerdict.METADATA_MISMATCH
    else:
        verdict = ExistenceVerdict.VERIFIED
    return ExistenceResult(reference=ref, verdict=verdict, record=rec, title_similarity=sim, issues=issues)


def _doi_outside_crossref(http: CachedClient, ref: Reference) -> ExistenceResult:
    """Crossref and OpenAlex don't know this DOI. Ask doi.org before calling it missing."""
    if not sources.doi_registered(http, ref.doi):
        return ExistenceResult(reference=ref, verdict=ExistenceVerdict.NOT_FOUND, issues=[
            "This DOI isn't registered with any DOI agency (checked Crossref, OpenAlex and doi.org)"])
    try:
        agency = sources.doi_agency(http, ref.doi)
        rec = sources.doi_metadata(http, ref.doi, agency)
    except (httpx.HTTPError, sources.LookupError_, OfflineMiss):
        agency, rec = None, None
    where = f"registered with {agency}" if agency else "registered"
    if rec is not None and (rec.title or rec.authors):
        res = _judge(ref, rec)
        res.issues.insert(0, f"DOI is {where}, not Crossref, so it isn't in Crossref or OpenAlex; "
                             "metadata came from doi.org")
        return res
    return ExistenceResult(reference=ref, verdict=ExistenceVerdict.EXISTS_UNINDEXED, issues=[
        f"The DOI is real ({where}), but no title or authors could be retrieved to compare. "
        "Open it and check it's the cited work"])


def _segments(title: str) -> list[str]:
    """'Powering up Britain - GOV.UK' -> ['Powering up Britain - GOV.UK', 'Powering up Britain', 'GOV.UK']"""
    parts = [p.strip() for p in re.split(r"\s[|\-–—:·]\s", title) if p.strip()]
    return [title] + (parts if len(parts) > 1 else [])


def _check_link(http: CachedClient, ref: Reference) -> ExistenceResult:
    try:
        p = http.probe(ref.url)
    except BlockedURL as e:
        return ExistenceResult(reference=ref, verdict=ExistenceVerdict.UNCHECKED, issues=[
            f"The link wasn't opened: {e}. Check it by hand"])
    status = p["status"]
    archive = f"https://web.archive.org/web/*/{ref.url}"
    if status in (404, 410):
        return ExistenceResult(reference=ref, verdict=ExistenceVerdict.NOT_FOUND, issues=[
            f"The link is broken (HTTP {status}) and no scholarly database has this work",
            f"It may have moved. Search for the title, or look for an archived copy: {archive}"])
    if status >= 400 or status == 0:
        return ExistenceResult(reference=ref, verdict=ExistenceVerdict.UNCHECKED, issues=[
            f"The website refused an automated check (HTTP {status}). This is common and doesn't "
            "mean the link is broken. Open it yourself"])
    kind = ("a PDF" if "pdf" in p["content_type"] else
            "a web page" if "html" in p["content_type"] else p["content_type"] or "a document")
    sim = None
    if ref.title and p.get("title"):
        sim = max(title_similarity(ref.title, seg) or 0.0 for seg in _segments(p["title"]))
    issues = [f"The link works ({kind}). The source isn't in Crossref, OpenAlex or arXiv, "
              "which is normal for reports, policy documents and web pages"]
    if p.get("title"):
        issues.append(f"Page title: “{p['title']}”" + (f" (title match {sim:.0%})" if sim is not None else ""))
    if sim is not None and sim < TITLE_MATCH:
        issues.append("The page title doesn't match the reference. Check the link points to the cited document")
    if p["final_url"] != ref.url:
        issues.append(f"Redirected to {p['final_url']}")
    rec = SourceRecord(source="web", identifier=ref.url, title=p.get("title"), url=p["final_url"])
    return ExistenceResult(reference=ref, verdict=ExistenceVerdict.EXISTS_UNINDEXED, record=rec,
                           title_similarity=round(sim, 3) if sim is not None else None, issues=issues)


def _verify(http: CachedClient, ref: Reference) -> ExistenceResult:
    if ref.doi:
        errors: list[str] = []
        cr = _attempt(lambda: sources.crossref_by_doi(http, ref.doi), errors, "Crossref")
        oa = _attempt(lambda: sources.openalex_by_doi(http, ref.doi), errors, "OpenAlex")
        rec = _merge(cr, oa)
        if rec:
            return _judge(ref, rec)
        if len(errors) == 2:     # neither answered: we can't say anything
            raise sources.LookupError_("Crossref and OpenAlex both failed: " + "; ".join(errors))
        return _doi_outside_crossref(http, ref)
    if ref.arxiv_id:
        rec = sources.arxiv_by_id(http, ref.arxiv_id)
        return _judge(ref, rec) if rec else ExistenceResult(
            reference=ref, verdict=ExistenceVerdict.NOT_FOUND, issues=["This arXiv ID doesn't exist"])
    if ref.pmid:
        rec = sources.pubmed_by_pmid(http, ref.pmid)
        return _judge(ref, rec) if rec else ExistenceResult(
            reference=ref, verdict=ExistenceVerdict.NOT_FOUND, issues=["This PubMed ID doesn't exist"])

    rec, sim, errors = _search(http, ref)
    if rec is not None and (sim or 0) >= SEARCH_ACCEPT:
        res = _judge(ref, rec)
        if res.verdict == ExistenceVerdict.VERIFIED:
            res.verdict = ExistenceVerdict.FOUND_BY_SEARCH
        res.issues.insert(0, "No DOI, arXiv ID or PMID in the reference; matched by title search. "
                             "Add the identifier")
        return res
    link = _check_link(http, ref) if ref.url else None
    if errors:
        # Some databases couldn't be searched, so "not found" or "can't verify" would be unfounded.
        if link is not None and link.verdict == ExistenceVerdict.EXISTS_UNINDEXED:
            link.issues.append("Not every database could be searched: " + "; ".join(errors))
            return link
        return ExistenceResult(reference=ref, verdict=ExistenceVerdict.UNCHECKED, issues=[
            "Couldn't search " + "; ".join(errors) + ". Not treated as missing. Run the check again later"])
    if link is not None:
        return link
    if looks_like_article(ref.raw):
        return ExistenceResult(reference=ref, verdict=ExistenceVerdict.NOT_FOUND, title_similarity=sim, issues=[
            "Formatted like a journal or conference paper, but no database has it, and it has no DOI "
            "or link. This is the pattern of a fabricated reference. Check it by hand"])
    return ExistenceResult(reference=ref, verdict=ExistenceVerdict.UNVERIFIABLE, title_similarity=sim, issues=[
        "No DOI or link, and not found in Crossref, OpenAlex or arXiv. This is common for reports, "
        "standards and legislation. Add a link or DOI, or check it by hand"])


def verify_reference(http: CachedClient, audit: AuditLog, ref: Reference) -> ExistenceResult:
    try:
        res = _verify(http, ref)
    except (httpx.HTTPError, sources.LookupError_, OfflineMiss) as e:
        res = ExistenceResult(reference=ref, verdict=ExistenceVerdict.UNCHECKED,
                              issues=[f"Lookup failed ({type(e).__name__}): {str(e)[:200]}. "
                                      "Not treated as missing"])
    audit.log("existence_verdict", key=ref.key, verdict=res.verdict.value,
              source=res.record.source if res.record else None,
              title_similarity=res.title_similarity, issues=res.issues)
    return res
