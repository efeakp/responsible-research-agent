"""Resolvers for authoritative scholarly databases.

Each resolver returns a SourceRecord, None for a definitive "does not exist"
(HTTP 404 / empty result), and raises on network failure so the caller can
mark the reference UNCHECKED rather than wrongly NOT_FOUND."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import quote

from .http import CachedClient
from .models import SourceRecord

CROSSREF = "https://api.crossref.org/works"
OPENALEX = "https://api.openalex.org/works"
ARXIV = "https://export.arxiv.org/api/query"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class LookupError_(Exception):
    """Non-definitive failure (5xx, 429, malformed response)."""


def _strip_tags(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip() or None


def _openalex_abstract(inv: Optional[dict]) -> Optional[str]:
    if not inv:
        return None
    positions = [(p, w) for w, ps in inv.items() for p in ps]
    return " ".join(w for _, w in sorted(positions)) or None


def _check(status: int, url: str) -> None:
    if status == 404:
        return
    if status >= 400:
        raise LookupError_(f"HTTP {status} from {url}")


# ---------- Crossref ----------

def _crossref_record(msg: dict) -> SourceRecord:
    titles = msg.get("title") or []
    year = None
    for k in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(k) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    retracted = any((u.get("type") or "").lower() == "retraction"
                    for u in msg.get("updated-by", []) or [])
    return SourceRecord(
        source="crossref",
        identifier=(msg.get("DOI") or "").lower(),
        title=titles[0] if titles else None,
        authors=[a.get("family") or a.get("name") or "" for a in msg.get("author", []) or []],
        year=year,
        abstract=_strip_tags(msg.get("abstract")),
        is_retracted=retracted,
        url=msg.get("URL"),
    )


def crossref_by_doi(http: CachedClient, doi: str) -> Optional[SourceRecord]:
    url = f"{CROSSREF}/{quote(doi, safe='/()')}"
    status, body = http.get(url)
    _check(status, url)
    if status == 404:
        return None
    try:
        return _crossref_record(json.loads(body)["message"])
    except (ValueError, KeyError) as e:
        raise LookupError_(f"Malformed Crossref response: {e}")


def crossref_search(http: CachedClient, citation: str, rows: int = 3) -> list[SourceRecord]:
    status, body = http.get(CROSSREF, params={"query.bibliographic": citation[:300], "rows": rows})
    _check(status, CROSSREF)
    if status == 404:
        return []
    items = json.loads(body).get("message", {}).get("items", [])
    return [_crossref_record(i) for i in items]


# ---------- OpenAlex ----------

def _openalex_record(w: dict) -> SourceRecord:
    authors = []
    for a in w.get("authorships", []) or []:
        name = (a.get("author") or {}).get("display_name") or ""
        authors.append(name.split()[-1] if name else "")
    doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
    return SourceRecord(
        source="openalex",
        identifier=doi or (w.get("id") or ""),
        title=w.get("title") or w.get("display_name"),
        authors=authors,
        year=w.get("publication_year"),
        abstract=_openalex_abstract(w.get("abstract_inverted_index")),
        is_retracted=bool(w.get("is_retracted")),
        url=w.get("id"),
    )


def openalex_by_doi(http: CachedClient, doi: str) -> Optional[SourceRecord]:
    url = f"{OPENALEX}/doi:{quote(doi, safe='/()')}"
    status, body = http.get(url)
    _check(status, url)
    if status == 404:
        return None
    return _openalex_record(json.loads(body))


def openalex_search(http: CachedClient, title: str, rows: int = 3) -> list[SourceRecord]:
    status, body = http.get(OPENALEX, params={"search": title[:300], "per_page": rows})
    _check(status, OPENALEX)
    if status == 404:
        return []
    return [_openalex_record(w) for w in json.loads(body).get("results", [])]


# ---------- arXiv ----------

_ATOM = {"a": "http://www.w3.org/2005/Atom"}


def arxiv_by_id(http: CachedClient, arxiv_id: str) -> Optional[SourceRecord]:
    status, body = http.get(ARXIV, params={"id_list": arxiv_id})
    _check(status, ARXIV)
    entries = _arxiv_entries(body)   # an invalid id yields an "Error" entry, filtered out
    if entries:
        entries[0].identifier = arxiv_id
    return entries[0] if entries else None


def _arxiv_entries(body: str) -> list[SourceRecord]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise LookupError_(f"Malformed arXiv response: {e}")
    out = []
    for entry in root.findall("a:entry", _ATOM):
        title = (entry.findtext("a:title", default="", namespaces=_ATOM) or "").strip()
        entry_id = entry.findtext("a:id", default="", namespaces=_ATOM) or ""
        if not title or title.lower() == "error" or "api/errors" in entry_id:
            continue
        published = entry.findtext("a:published", default="", namespaces=_ATOM)
        m = re.search(r"abs/(.+?)(?:v\d+)?$", entry_id)
        out.append(SourceRecord(
            source="arxiv",
            identifier=m.group(1) if m else entry_id,
            title=re.sub(r"\s+", " ", title),
            authors=[(a.findtext("a:name", default="", namespaces=_ATOM) or "").split()[-1]
                     for a in entry.findall("a:author", _ATOM)
                     if (a.findtext("a:name", default="", namespaces=_ATOM) or "").split()],
            year=int(published[:4]) if published[:4].isdigit() else None,
            abstract=re.sub(r"\s+", " ",
                            entry.findtext("a:summary", default="", namespaces=_ATOM)).strip() or None,
            url=entry_id,
        ))
    return out


def arxiv_search(http: CachedClient, title: str, rows: int = 3) -> list[SourceRecord]:
    """Title search via the arXiv API's search_query parameter (ti: field, exact phrase)."""
    phrase = re.sub(r'["\\]', " ", title).strip()[:200]
    status, body = http.get(ARXIV, params={"search_query": f'ti:"{phrase}"', "start": 0,
                                           "max_results": rows})
    _check(status, ARXIV)
    return _arxiv_entries(body)


# ---------- PubMed ----------

def pubmed_by_pmid(http: CachedClient, pmid: str) -> Optional[SourceRecord]:
    status, body = http.get(PUBMED_EFETCH, params={"db": "pubmed", "id": pmid, "retmode": "xml"})
    _check(status, PUBMED_EFETCH)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise LookupError_(f"Malformed PubMed response: {e}")
    art = root.find(".//PubmedArticle")
    if art is None:
        return None
    title = "".join(art.find(".//ArticleTitle").itertext()) if art.find(".//ArticleTitle") is not None else None
    abstract = " ".join("".join(t.itertext()) for t in art.findall(".//Abstract/AbstractText")) or None
    year_txt = art.findtext(".//JournalIssue/PubDate/Year") or art.findtext(".//ArticleDate/Year")
    pubtypes = [p.text or "" for p in art.findall(".//PublicationType")]
    return SourceRecord(
        source="pubmed",
        identifier=pmid,
        title=title,
        authors=[a.findtext("LastName") or "" for a in art.findall(".//AuthorList/Author")],
        year=int(year_txt) if year_txt and year_txt.isdigit() else None,
        abstract=abstract,
        is_retracted=any("retracted publication" in p.lower() for p in pubtypes),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    )


# ---------- doi.org: DOIs registered with agencies other than Crossref ----------
# Crossref and OpenAlex don't know DOIs from DataCite (Zenodo, Figshare, many datasets and
# reports), mEDRA, JaLC and others. doi.org knows every registered DOI, whichever agency.

DOI_HANDLES = "https://doi.org/api/handles/"
DOI_RA = "https://doi.org/ra/"
CSL_JSON = "application/vnd.citationstyles.csl+json"


def doi_registered(http: CachedClient, doi: str) -> bool:
    """True if the DOI is registered with any agency. Raises LookupError_ if doi.org's
    answer is unclear, so an outage is never reported as 'does not exist'."""
    url = DOI_HANDLES + quote(doi, safe="/()")
    status, body = http.get(url)
    try:
        code = json.loads(body).get("responseCode") if body else None
    except ValueError:
        code = None
    if code == 1 and status == 200:
        return True
    if code == 100 or (status == 404 and code is None):
        return False
    raise LookupError_(f"Unclear answer from doi.org (HTTP {status}, code {code})")


def doi_agency(http: CachedClient, doi: str) -> Optional[str]:
    """The registration agency (DataCite, Crossref, mEDRA…), or None if unknown."""
    status, body = http.get(DOI_RA + quote(doi, safe="/()"))
    if status != 200:
        return None
    try:
        items = json.loads(body)
    except ValueError:
        return None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0].get("RA")
    return None


def doi_metadata(http: CachedClient, doi: str, agency: Optional[str]) -> Optional[SourceRecord]:
    """Citation metadata via doi.org content negotiation (CSL JSON). None if the agency
    doesn't offer it."""
    status, body = http.get(f"https://doi.org/{quote(doi, safe='/()')}", headers={"Accept": CSL_JSON})
    if status != 200:
        return None
    try:
        d = json.loads(body)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    title = d.get("title")
    if isinstance(title, list):
        title = title[0] if title else None
    year = None
    parts = (d.get("issued") or {}).get("date-parts") or [[None]]
    if parts and parts[0] and parts[0][0]:
        try:
            year = int(parts[0][0])
        except (TypeError, ValueError):
            year = None
    return SourceRecord(
        source=(agency or "doi.org").lower(),
        identifier=doi,
        title=_strip_tags(title) if isinstance(title, str) else None,
        authors=[a.get("family") or a.get("literal") or "" for a in d.get("author") or []
                 if isinstance(a, dict)],
        year=year,
        abstract=_strip_tags(d.get("abstract")) if isinstance(d.get("abstract"), str) else None,
        url=f"https://doi.org/{doi}",
    )
