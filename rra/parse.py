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


# where a title stops: access notes, and a publisher name run on without a full stop
TITLE_STOP = re.compile(r"\s*,?\s*(?:Available\s+(?:at|from|online)|Retrieved\s+(?:from|on)|\[?Accessed|URL:?)\b.*$",
                        re.IGNORECASE)
PUBLISHER_TAIL = re.compile(
    r"[\s.,]+(?:Routledge|SAGE|Sage|Springer(?:\s+Nature)?|Wiley(?:-Blackwell)?|Elsevier|Palgrave(?:\s+Macmillan)?|"
    r"Pearson|McGraw[- ]Hill|Polity(?:\s+Press)?|Earthscan|Taylor\s*&\s*Francis|Edward\s+Elgar|MIT\s+Press|"
    r"(?:[A-Z][\w]+\s+){1,3}University\s+Press)\.?$")


def _clean_title(t: str) -> str:
    t = t.strip().lstrip(",;: ").strip().strip('"“”')
    t = TITLE_STOP.sub("", t)
    t = PUBLISHER_TAIL.sub("", t)
    return t.strip(" ,;:")


VOL_ISS_PAGES = re.compile(
    r"(?<![\d.])(?P<vol>\d{1,4})\s*\(\s*(?P<iss>[A-Za-z0-9\-–/]{1,9})\s*\)\s*[,:]?\s*(?:pp?\.\s*)?"
    r"(?P<p1>[A-Za-z]?\d{1,7})(?:\s*[-–—]\s*(?P<p2>[A-Za-z]?\d{1,7}))?(?![\d.]*\d)")
VOL_NO_PAGES = re.compile(
    r"\bvol(?:ume)?\.?\s*(?P<vol>\d{1,4})(?:\s*,?\s*(?:no\.?|issue)\s*(?P<iss>\d{1,4}))?"
    r"(?:\s*,?\s*pp?\.\s*(?P<p1>\d{1,7})(?:\s*[-–—]\s*(?P<p2>\d{1,7}))?)?", re.IGNORECASE)
VOL_PAGES = re.compile(
    r"(?<![\d.(])(?P<vol>\d{1,4})\s*[,:]\s*(?:pp?\.\s*)?(?P<p1>[A-Za-z]?\d{1,7})\s*[-–—]\s*(?P<p2>[A-Za-z]?\d{1,7})(?!\d)")
VOL_SPACE_PAGES = re.compile(r"(?<=,\s)(?P<vol>\d{1,4})\s+(?P<p1>\d{1,7})\s*[-–—]\s*(?P<p2>\d{1,7})(?!\d)")


def _volume_pages(body: str) -> dict:
    """Volume, issue and pages from the part of the reference after the year."""
    y = re.search(r"\(?\b(?:19|20)\d{2}[a-z]?\b\)?", body)
    tail = body[y.end():] if y else body
    tail = URL_RE.sub("", tail)
    tail = re.sub(r"\bdoi:\s*\S+", "", tail, flags=re.IGNORECASE)
    for rx in (VOL_NO_PAGES, VOL_ISS_PAGES, VOL_PAGES, VOL_SPACE_PAGES):
        m = rx.search(tail)
        if m:
            g = m.groupdict()
            return {"volume": g.get("vol"), "issue": g.get("iss"), "first_page": g.get("p1"),
                    "last_page": g.get("p2")}
    return {}


def _guess_title(body: str) -> str | None:
    """Best-effort title guess: the segment after the year, up to the next full stop."""
    m = re.search(r"\((?:19|20)\d{2}[a-z]?\)\.?\s*(.+?)\.(?:\s|$)", body)
    if m:
        t = _clean_title(m.group(1))
        return t if len(t) >= 6 else None
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
            **_volume_pages(body),
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


def _numeric(sentence: str) -> list[str]:
    keys: list[str] = []
    for m in CITE_RE.finditer(sentence):
        for k in _expand_keys(m.group(1)):
            if k not in keys:
                keys.append(k)
    return keys


def citation_style(body: str, refs: list[Reference] | None = None) -> str:
    """'numeric' if the text cites [n] and those numbers mostly exist in the reference
    list (or there is no list); 'author_year' if it doesn't but there is a reference list."""
    cited = {k for s in split_sentences(body) for k in _numeric(s)}
    keys = {r.key for r in refs or []}
    if cited and (not keys or len(cited & keys) >= len(cited) / 2):
        return "numeric"
    return "author_year" if keys else "numeric"


def extract_claims(body: str, refs: list[Reference] | None = None) -> list[Claim]:
    """Sentences that cite something. Numeric [n] citations always; author–year
    citations when the document uses them (needs the reference list to match)."""
    from .citations import build_index, find_author_year, plain_text   # avoid an import cycle
    style = citation_style(body, refs)
    index = build_index(refs) if style == "author_year" else []
    claims: list[Claim] = []
    for s in split_sentences(body):
        if style == "numeric":
            keys = _numeric(s)
            if keys:
                plain = re.sub(r"\s*\[\d+(?:\s*[-–,]\s*\d+)*\]", "", s).strip()
                claims.append(Claim(id=f"C{len(claims) + 1}", text=s, cited_keys=keys, plain=plain))
        else:
            found = find_author_year(s, index)
            if found.keys or found.unresolved:
                claims.append(Claim(id=f"C{len(claims) + 1}", text=s, cited_keys=found.keys,
                                    unresolved=found.unresolved, plain=plain_text(s, found.spans)))
    return claims


def unresolved_citations(claims: list[Claim], refs: list[Reference]) -> list[str]:
    """Numeric keys with no reference entry, then author–year citations that matched no
    single reference, in order of first appearance."""
    keys = {r.key for r in refs}
    numeric = sorted({k for c in claims for k in c.cited_keys} - keys, key=lambda k: int(k) if k.isdigit() else 0)
    labels: list[str] = []
    for c in claims:
        for u in c.unresolved:
            if u not in labels:
                labels.append(u)
    return numeric + labels
