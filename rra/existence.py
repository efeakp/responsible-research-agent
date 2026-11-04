"""Stage 1 verifier: does each reference exist, and does it say what the
manuscript says it is (title, first author, year)? Is it retracted?"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

import httpx

from .audit import AuditLog
from .http import CachedClient, OfflineMiss
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
        if _norm(ref.first_author) not in surnames:
            issues.append(f"First author “{ref.first_author}” not among record authors "
                          f"({', '.join(rec.authors[:4])}{'…' if len(rec.authors) > 4 else ''})")
    return issues, sim


def _lookup(http: CachedClient, ref: Reference) -> tuple[Optional[SourceRecord], str]:
    """Returns (record, how_found). Raises on non-definitive failures."""
    if ref.doi:
        cr = sources.crossref_by_doi(http, ref.doi)
        oa = sources.openalex_by_doi(http, ref.doi)
        return _merge(cr, oa), "doi"
    if ref.arxiv_id:
        return sources.arxiv_by_id(http, ref.arxiv_id), "arxiv"
    if ref.pmid:
        return sources.pubmed_by_pmid(http, ref.pmid), "pmid"
    return None, "none"


def _search(http: CachedClient, ref: Reference) -> tuple[Optional[SourceRecord], Optional[float]]:
    query = ref.title or ref.raw
    best, best_sim = None, 0.0
    candidates = sources.openalex_search(http, query) + sources.crossref_search(http, ref.raw)
    if ref.title and not any((title_similarity(ref.title, c.title) or 0) >= SEARCH_ACCEPT
                             for c in candidates):
        # preprints are often missing from the general indexes' top hits; ask arXiv directly
        candidates += sources.arxiv_search(http, ref.title)
    for rec in candidates:
        sim = title_similarity(ref.title or ref.raw, rec.title) or 0.0
        if sim > best_sim:
            best, best_sim = rec, sim
    return best, (round(best_sim, 3) if best else None)


def verify_reference(http: CachedClient, audit: AuditLog, ref: Reference) -> ExistenceResult:
    try:
        rec, how = _lookup(http, ref)
        if how == "none":
            rec, sim = _search(http, ref)
            if rec is None or (sim or 0) < SEARCH_ACCEPT:
                res = ExistenceResult(reference=ref, verdict=ExistenceVerdict.NOT_FOUND,
                                      record=None, title_similarity=sim,
                                      issues=["No identifier given and no confident title match "
                                              f"(best similarity {sim or 0:.2f})"])
            else:
                issues, sim = compare(ref, rec)
                issues.insert(0, "No DOI/arXiv/PMID in reference; matched by title search — add the identifier")
                verdict = (ExistenceVerdict.RETRACTED if rec.is_retracted
                           else ExistenceVerdict.FOUND_BY_SEARCH)
                res = ExistenceResult(reference=ref, verdict=verdict, record=rec,
                                      title_similarity=sim, issues=issues)
        elif rec is None:
            res = ExistenceResult(reference=ref, verdict=ExistenceVerdict.NOT_FOUND,
                                  issues=[f"Identifier ({how}) does not resolve in any queried database"])
        else:
            issues, sim = compare(ref, rec)
            if rec.is_retracted:
                verdict = ExistenceVerdict.RETRACTED
                issues.insert(0, "Work is flagged as RETRACTED")
            elif any(i.startswith(("Title mismatch", "Year mismatch", "First author")) for i in issues):
                verdict = ExistenceVerdict.METADATA_MISMATCH
            else:
                verdict = ExistenceVerdict.VERIFIED
            res = ExistenceResult(reference=ref, verdict=verdict, record=rec,
                                  title_similarity=sim, issues=issues)
    except (httpx.HTTPError, sources.LookupError_, OfflineMiss) as e:
        res = ExistenceResult(reference=ref, verdict=ExistenceVerdict.UNCHECKED,
                              issues=[f"Lookup failed ({type(e).__name__}): {str(e)[:200]} — "
                                      "not treated as missing"])
    audit.log("existence_verdict", key=ref.key, verdict=res.verdict.value,
              source=res.record.source if res.record else None,
              title_similarity=res.title_similarity, issues=res.issues)
    return res
