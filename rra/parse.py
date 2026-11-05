"""Manuscript parsing: split body from reference list, extract identifiers,
and find claims (sentences carrying numeric citations like [1], [2,3], [4-6]).

v0.1 supports numeric citation styles only. Author-year styles are listed as a
known limitation rather than guessed at."""

from __future__ import annotations

import re

from .models import Claim, Reference

REF_HEADING = re.compile(r"^\s{0,3}(#+\s*)?(references|bibliography|works cited)\s*:?\s*$",
                         re.IGNORECASE | re.MULTILINE)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
ARXIV_RE = re.compile(r"arxiv(?:\.org)?[:\s/]*(?:abs/|pdf/)?(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^()\s]*(?:\([^()\s]*\)[^()\s]*)*)\)")
URL_RE = re.compile(r"https?://[^\s<>\"\]]+", re.IGNORECASE)
PMID_RE = re.compile(r"\bPMID[:\s]*(\d{4,9})\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\((19|20)(\d{2})[a-z]?\)|\b(19|20)(\d{2})\b")
REF_LINE_RE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)[.)])\s+(.*\S)\s*$")
CITE_RE = re.compile(r"\[(\d+(?:\s*[-–,]\s*\d+)*)\]")
ABBREVIATIONS = ["et al.", "e.g.", "i.e.", "Fig.", "Figs.", "vs.", "cf.", "approx.", "Eq."]


def split_manuscript(text: str) -> tuple[str, str]:
    """Return (body, references_section). Uses the LAST references heading."""
    matches = list(REF_HEADING.finditer(text))
    if not matches:
        return text, ""
    m = matches[-1]
    return text[: m.start()], text[m.end():]


def _clean_doi(doi: str) -> str:
    doi = doi.rstrip(".,;)]}")
    # A DOI may legitimately contain parentheses, e.g. 10.1016/S0140-6736(97)11096-0;
    # only strip a trailing ')' when it is unbalanced (done above if unbalanced).
    if doi.count("(") > doi.count(")"):
        doi += ")"
    return doi.lower()


def _clean_url(url: str) -> str:
    url = url.rstrip(".,;:'\u201d\u2019")
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(".,;:")
    return url


def _first_url(body: str) -> str | None:
    """The first link that isn't just a DOI or arXiv address (those are handled as identifiers)."""
    body = MD_LINK_RE.sub(r"\1 \2", body)
    for m in URL_RE.finditer(body):
        url = _clean_url(m.group(0))
        if not re.search(r"//(dx\.)?doi\.org/|//arxiv\.org/", url, re.IGNORECASE):
            return url
    return None


def _guess_title(body: str) -> str | None:
    """Best-effort title guess: the segment after the year, up to the next full stop."""
    m = re.search(r"\((?:19|20)\d{2}[a-z]?\)\.?\s*(.+?)\.(?:\s|$)", body)
    if m:
        t = m.group(1).strip().strip('"“”')
        return t if len(t) > 10 else None
    # "Author. Title. Journal" fallback: second sentence-ish segment
    parts = [p.strip() for p in re.split(r"\.\s+", body) if p.strip()]
    if len(parts) >= 2 and len(parts[1]) > 10:
        return parts[1].strip('"“”')
    return None


def _guess_first_author(body: str) -> str | None:
    m = re.match(r"\s*([A-Z][A-Za-z'\-À-ÿ]+)", body)
    return m.group(1) if m else None


def parse_references(section: str) -> list[Reference]:
    refs: list[Reference] = []
    for line in section.splitlines():
        m = REF_LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1) or m.group(2)
        body = m.group(3)
        doi = DOI_RE.search(body)
        arx = ARXIV_RE.search(body)
        pmid = PMID_RE.search(body)
        year = None
        ym = YEAR_RE.search(body)
        if ym:
            year = int((ym.group(1) or ym.group(3)) + (ym.group(2) or ym.group(4)))
        refs.append(Reference(
            key=key,
            raw=body,
            doi=_clean_doi(doi.group(1)) if doi else None,
            arxiv_id=arx.group(1) if arx else None,
            pmid=pmid.group(1) if pmid else None,
            url=_first_url(body),
            title=_guess_title(URL_RE.sub("", MD_LINK_RE.sub(r"\1", body))),
            first_author=_guess_first_author(body),
            year=year,
        ))
    return refs


def _expand_keys(spec: str) -> list[str]:
    keys: list[str] = []
    for part in re.split(r"\s*,\s*", spec):
        if re.search(r"[-–]", part):
            a, b = [int(x) for x in re.split(r"\s*[-–]\s*", part)]
            if 0 < b - a < 50:
                keys.extend(str(i) for i in range(a, b + 1))
        elif part.strip():
            keys.append(part.strip())
    return keys


def split_sentences(text: str) -> list[str]:
    protected = text
    for i, abbr in enumerate(ABBREVIATIONS):
        protected = protected.replace(abbr, abbr.replace(".", f"\u0000{i}\u0000"))
    # paragraphs first, then sentences
    sentences: list[str] = []
    for para in re.split(r"\n\s*\n", protected):
        para = " ".join(l.strip() for l in para.splitlines() if not l.strip().startswith("#"))
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\[(])", para):
            s = s.strip()
            if s:
                sentences.append(s)
    restored = []
    for s in sentences:
        for i, abbr in enumerate(ABBREVIATIONS):
            s = s.replace(abbr.replace(".", f"\u0000{i}\u0000"), abbr)
        restored.append(s)
    return restored


def extract_claims(body: str) -> list[Claim]:
    claims: list[Claim] = []
    for s in split_sentences(body):
        keys: list[str] = []
        for m in CITE_RE.finditer(s):
            for k in _expand_keys(m.group(1)):
                if k not in keys:
                    keys.append(k)
        if keys:
            claims.append(Claim(id=f"C{len(claims) + 1}", text=s, cited_keys=keys))
    return claims
