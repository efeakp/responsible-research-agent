"""Author–year citations: (Smith, 2020), (Smith & Jones, 2019; Brown et al., 2021a),
(Intergovernmental Panel on Climate Change [IPCC], 2023), and narrative forms such as
Smith et al. (2020) or Clery and Gough (2022).

Each citation is matched to a reference by first author (or organisation, or its
bracketed acronym), year and year suffix, and the second author when two are named.
Nothing is guessed: a citation that matches no reference, or more than one, is reported
as unresolved with the reason.

Narrative citations are counted only when they match a reference. Text like
"in Hull (2020)" is too easily confused with prose to report as a missing citation.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from .models import Reference

YEAR = r"(?:19|20)\d{2}[a-z]?"
YEAR_OR_ND = rf"(?:{YEAR}|n\.\s?d\.)"
YEAR_RE = re.compile(rf"\b({YEAR})\b")
PAREN = re.compile(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)")      # one level of nesting: (IPCC [x], 2023)
PREFIX = re.compile(r"^(?:see\s+also|see|e\.g\.,?|i\.e\.,?|cf\.|for\s+example,?|as\s+cited\s+in|"
                    r"adapted\s+from|quoted\s+in|reviewed\s+in|following)\s+", re.IGNORECASE)
LOCATOR = r"(?:\s*,\s*(?:pp?\.|para\.?|paras\.|ch\.|chap\.|section|sec\.|fig\.|table)\s*[\w–\-]+)?"
ITEM = re.compile(rf"^(?P<names>\D.*?)[,\s]\s*(?P<years>{YEAR_OR_ND}(?:\s*(?:,|&|and)\s*{YEAR_OR_ND})*){LOCATOR}\s*$",
                  re.IGNORECASE)
MALFORMED = re.compile(r"^(?P<names>[A-Z\[][^,;]{0,120}),\s*(?P<year>\d{1,3}|\d{5,}|(?:19|20)\d{2}\d+)\s*$")
YEARS_ONLY = re.compile(rf"^\s*(?P<years>{YEAR}(?:\s*[,;]\s*{YEAR})*){LOCATOR}\s*$", re.IGNORECASE)
NOT_NAMES = {"in", "from", "since", "by", "until", "before", "after", "between", "during", "around", "circa",
             "c.", "about", "approximately", "approx.", "up", "to", "and", "or", "the", "of", "for", "on", "at",
             "as", "compared", "versus", "vs.", "figure", "fig.", "table", "section", "eq.", "n", "n=", "year",
             "years", "fy", "q1", "q2", "q3", "q4", "january", "february", "march", "april", "may", "june",
             "july", "august", "september", "october", "november", "december", "spring", "summer", "autumn",
             "winter", "est.", "established", "founded", "updated", "revised", "published", "accessed"}
PARTICLES = {"van", "von", "de", "der", "den", "di", "da", "du", "del", "della", "la", "le", "ten", "ter", "st."}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class RefEntry:
    key: str
    segment: str                 # normalised author part (before the year)
    raw: str
    year: Optional[int]
    suffix: str                  # "a" in 2020a, else ""


@dataclass
class Cite:
    label: str                   # as written, e.g. "Smith & Jones, 2019"
    names: list[str]
    et_al: bool
    year: Optional[int]          # None for n.d.
    suffix: str


@dataclass
class Found:
    keys: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    spans: list[tuple[int, int]] = field(default_factory=list)   # citation text to drop for checking


def build_index(refs: list[Reference]) -> list[RefEntry]:
    out = []
    for r in refs:
        m = re.search(rf"\b({YEAR})\b|\bn\.\s?d\.", r.raw)
        segment = r.raw[:m.start()] if m else r.raw[:120]
        year_txt = m.group(1) if m and m.group(1) else None
        out.append(RefEntry(key=r.key, segment=norm(segment), raw=r.raw,
                            year=int(year_txt[:4]) if year_txt else None,
                            suffix=year_txt[4:] if year_txt and len(year_txt) > 4 else ""))
    return out


def split_names(names: str) -> tuple[list[str], bool]:
    et_al = bool(re.search(r"\bet\s+al\b\.?", names, re.IGNORECASE))
    names = re.sub(r",?\s*\bet\s+al\b\.?", "", names, flags=re.IGNORECASE).strip(" ,")
    parts = [p.strip(" ,") for p in re.split(r"\s*(?:,|&|\band\b)\s*", names) if p.strip(" ,")]
    return parts, et_al


def _looks_like_names(names: str) -> bool:
    words = names.split()
    if not words or len(words) > 16 or len(names) > 160:
        return False
    first = words[0].lower()
    if first in NOT_NAMES or re.fullmatch(r"[\d.,%]+", first):
        return False
    return words[0][0].isupper() or words[0][0] == "[" or first in PARTICLES


def _years(text: str) -> list[tuple[Optional[int], str]]:
    out = []
    for y in re.findall(rf"{YEAR}|n\.\s?d\.", text, flags=re.IGNORECASE):
        out.append((None, "") if y.lower().startswith("n") else (int(y[:4]), y[4:]))
    return out


def _first_matches(name: str, e: RefEntry) -> bool:
    n = norm(name)
    if not n:
        return False
    if re.match(rf"{re.escape(n)}\b", e.segment):
        return True
    # organisation acronym given in brackets in the reference: "... Change [IPCC] (2023)"
    return bool(re.search(r"[\[(]\s*" + re.escape(name.strip("[]() ")) + r"\s*[\])]", e.raw, re.IGNORECASE))


def match(cite: Cite, index: list[RefEntry]) -> list[str]:
    hits = [e for e in index
            if e.year == cite.year and (not cite.suffix or e.suffix == cite.suffix)
            and cite.names and _first_matches(cite.names[0], e)]
    if len(hits) > 1 and len(cite.names) >= 2 and not cite.et_al:
        second = norm(cite.names[1])
        narrowed = [e for e in hits if re.search(rf"\b{re.escape(second)}\b", e.segment)]
        hits = narrowed or hits
    return [e.key for e in hits]


def _resolve(cite: Cite, index: list[RefEntry], found: Found, report_missing: bool) -> bool:
    keys = match(cite, index)
    if len(keys) == 1:
        if keys[0] not in found.keys:
            found.keys.append(keys[0])
        return True
    if not report_missing:
        return False
    if not keys:
        found.unresolved.append(f"{cite.label}: no matching reference")
    else:
        found.unresolved.append(f"{cite.label}: matches more than one reference ([{'], ['.join(keys)}])")
    return True


def find_author_year(sentence: str, index: list[RefEntry]) -> Found:
    found = Found()
    for m in PAREN.finditer(sentence):
        inner = m.group(1).strip()
        # narrative: Name (2020) / Name et al. (2020, p. 4)
        ym = YEARS_ONLY.match(inner)
        if ym:
            years = _years(ym.group("years"))
            before = sentence[:m.start()].rstrip()
            words = before.split()
            for n in range(1, min(12, len(words)) + 1):
                tail = " ".join(words[-n:]).strip(" ,;:")
                if not _looks_like_names(tail):
                    continue
                names, et_al = split_names(tail)
                ok = [_resolve(Cite(f"{tail} ({y}{s})", names, et_al, y, s), index, found, False)
                      for y, s in years]
                if all(ok):
                    found.spans.append((m.start(), m.end()))
                    break
            continue
        # parenthetical: (Smith, 2020; Jones & Lee, 2019a, p. 4)
        items = [i.strip() for i in inner.split(";") if i.strip()]
        any_citation = False
        pending_malformed = []
        for item in items:
            item = PREFIX.sub("", item).strip()
            im = ITEM.match(item)
            if im and _looks_like_names(im.group("names")):
                any_citation = True
                names, et_al = split_names(im.group("names"))
                for y, s in _years(im.group("years")):
                    label = f"{im.group('names').strip()}, {y if y else 'n.d.'}{s}"
                    _resolve(Cite(label, names, et_al, y, s), index, found, True)
            elif MALFORMED.match(item) and _looks_like_names(MALFORMED.match(item).group("names")):
                pending_malformed.append(item)
        if pending_malformed and (any_citation or len(items) == len(pending_malformed)):
            any_citation = True
            for item in pending_malformed:
                found.unresolved.append(f"{item}: the year looks wrong")
        if any_citation:
            found.spans.append((m.start(), m.end()))
    return found


def plain_text(sentence: str, spans: list[tuple[int, int]]) -> str:
    """The sentence with citation brackets removed, for the support check."""
    out, last = [], 0
    for a, b in sorted(spans):
        out.append(sentence[last:a])
        last = b
    out.append(sentence[last:])
    text = "".join(out)
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()
