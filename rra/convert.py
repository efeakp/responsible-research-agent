"""Convert an article (PDF, Word, HTML or text) into Markdown the verifier can read:
numbered citations like [1] in the body, then a "## References" heading with one
numbered entry per line.

Extraction is done by existing libraries; the value here is the clean-up that
journal layouts need: re-joining hyphenated words and wrapped lines, dropping page
numbers, finding the reference list, splitting it into one entry per reference,
and numbering entries when the source used automatic numbering that didn't survive
extraction.

Everything uncertain is reported as a warning rather than guessed silently.
Always skim the output before verifying it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

REF_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:\d+\.?\s+)?"
    r"(references(?: and notes)?|bibliography|literature cited|works cited|reference list)"
    r"\s*(?:\*\*|__)?\s*:?\s*$",
    re.IGNORECASE)
# headings that usually come after the reference list
AFTER_REFS = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*)?\s*(appendix|appendices|supplementary|supporting information|"
    r"acknowledg(?:e)?ments?|author contributions|funding|conflicts? of interest|figure legends|tables?)\b",
    re.IGNORECASE)
NUMBERED_ENTRY = re.compile(r"^\s*(?:\[(\d{1,4})\]|(\d{1,4})[.)])\s+(\S.*)$")
AUTHOR_START = re.compile(r"^[A-Z][A-Za-zÀ-ÿ'’\-]+(?:,|\s+[A-Z]{1,3}[.,\s]|\s+et al)")
PAGE_NUMBER = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*(?:of\s*\d{1,4})?\s*$", re.IGNORECASE)
SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
SUP_RUN = re.compile(r"(?<=[A-Za-z\)\].,;:])([⁰¹²³⁴⁵⁶⁷⁸⁹]+(?:[,–\-‒][⁰¹²³⁴⁵⁶⁷⁸⁹]+)*)")


# A superscript straight after one of these is notation, not a citation: chemical formulas
# (CO², CH⁴ typed as superscript), units (m², kWh/m²) and powers of ten (10⁶).
_ELEMENT = r"(?:Cl|Br|Na|Ca|Mg|Fe|Si|Al|Hg|Pb|Zn|Cu|C|H|O|N|S|F|P|K)"
NOTATION_BEFORE = re.compile(
    r"(?:(?<![A-Za-z])" + _ELEMENT + r"(?:\d*" + _ELEMENT + r"){0,3}\d*"     # CO, CH, SO, HFC, N2O
    r"|(?:^|[\s/·(\d])(?:m|cm|mm|km|ft|kg|g|t|l|kWh|MWh|W|kW|J|MJ|GJ|Pa|s|h|yr)"  # units
    r"|(?:^|[\s×x(])10)$")


def is_notation(before: str) -> bool:
    return bool(NOTATION_BEFORE.search(before))


class ConversionUnavailable(RuntimeError):
    """The file type isn't supported, or the library it needs isn't installed."""


@dataclass
class Conversion:
    markdown: str
    extractor: str
    references: int = 0
    numbered_by_converter: bool = False
    warnings: list[str] = field(default_factory=list)


# ---------- extraction ----------

def _pdf(path: Path) -> tuple[str, str]:
    try:
        import pymupdf4llm  # better layout handling (AGPL licence)
        return pymupdf4llm.to_markdown(str(path), show_progress=False), "pymupdf4llm"
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ConversionUnavailable("PDF support needs pypdf: pip install -e \".[convert]\"")
    reader = PdfReader(str(path))
    pages = [(p.extract_text() or "") for p in reader.pages]
    return "\n\n".join(pages), "pypdf"


def _docx_runs(paragraph) -> list[tuple[str, bool]]:
    """Every run's text in document order, including runs inside hyperlinks, fields and
    content controls (where reference managers put citations, and where clickable DOIs
    live). Skips tracked deletions and text boxes. Returns (text, is_superscript)."""
    from docx.oxml.ns import qn
    from docx.text.run import Run
    skip = {qn("w:del"), qn("w:moveFrom"), qn("w:txbxContent")}
    root = paragraph._p
    pieces: list[tuple[str, bool]] = []
    # Links whose visible text isn't the address ("BSI Knowledge"): remember the target URL
    # so it can be written out after the text, where the link check will find it.
    link_after: dict[int, str] = {}
    for h in root.iter(qn("w:hyperlink")):
        rid = h.get(qn("r:id"))
        runs = list(h.iter(qn("w:r")))
        if not rid or not runs:
            continue
        try:
            url = paragraph.part.rels[rid].target_ref
        except (KeyError, AttributeError):
            continue
        shown = "".join(Run(r, paragraph).text for r in runs)
        if url.lower().startswith(("http://", "https://")) and url.rstrip("/") not in shown:
            link_after[id(runs[-1])] = url
    for r in root.iter(qn("w:r")):
        node, skipped = r.getparent(), False
        while node is not None and node is not root:
            if node.tag in skip:
                skipped = True
                break
            node = node.getparent()
        if skipped:
            continue
        run = Run(r, paragraph)
        if run.text:
            pieces.append((run.text, bool(run.font.superscript)))
        if id(r) in link_after:
            pieces.append((f" {link_after[id(r)]} ", False))
    return pieces


def _docx(path: Path) -> tuple[str, str]:
    try:
        import docx
    except ImportError:
        raise ConversionUnavailable("Word support needs python-docx: pip install -e \".[convert]\"")
    d = docx.Document(str(path))
    out = []
    for p in d.paragraphs:
        pieces = _docx_runs(p)
        # merge neighbouring superscript runs: Word often splits "1,2" into "1" "," "2"
        merged: list[list] = []
        for text, sup in pieces:
            if merged and merged[-1][1] and sup:
                merged[-1][0] += text
            else:
                merged.append([text, sup])
        parts = []
        for text, sup in merged:
            if sup and re.fullmatch(r"[\d,\-–\s]+", text) and re.search(r"\d", text):
                if is_notation("".join(parts)):
                    text = text.translate(_TO_SUP)                # CO², m², 10⁶: keep as superscript
                else:
                    digits = re.sub(r"\s+", "", text).strip(",")
                    text = "[" + digits + "]"                     # superscript citation numbers become [n]
            parts.append(text)
        text = "".join(parts).strip()
        if not text:
            out.append("")
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith("heading") or style == "title":
            level = int(re.sub(r"\D", "", style) or 1) if style != "title" else 1
            out.append(f"{'#' * min(level + 1, 6)} {text}")
        else:
            out.append(text)
        out.append("")
    return "\n".join(out), "python-docx"


def _html(path: Path) -> tuple[str, str]:
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify
    except ImportError:
        raise ConversionUnavailable("HTML support needs markdownify and beautifulsoup4: pip install -e \".[convert]\"")
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    for sup in soup.find_all("sup"):          # <sup>3</sup> or <sup><a>3</a></sup> citations
        t = sup.get_text(strip=True)
        if re.fullmatch(r"[\d,\-–\s]+", t):
            sup.replace_with(f"[{t}]")
    body = soup.find("article") or soup.find("main") or soup.body or soup
    return markdownify(str(body), heading_style="ATX", strip=["a", "img"]), "markdownify"


def extract(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf(path)
    if suffix == ".docx":
        return _docx(path)
    if suffix in (".html", ".htm", ".xhtml"):
        return _html(path)
    if suffix in (".md", ".markdown", ".txt"):
        return path.read_text(encoding="utf-8", errors="replace"), "text"
    raise ConversionUnavailable(f"Unsupported file type {suffix!r}. Use .pdf, .docx, .html or .txt.")


# ---------- clean-up ----------

def _clean_lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("­", "").replace("ﬁ", "fi").replace("ﬂ", "fl")
    text = re.sub(r"(\w)-\n(\s*)([a-z])", r"\1\3", text)          # de-hyphenate line breaks
    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        if PAGE_NUMBER.match(line):
            continue
        lines.append(line)
    return lines


def _superscripts_to_brackets(text: str) -> str:
    def repl(m: re.Match) -> str:
        if is_notation(m.string[:m.start()]):
            return m.group(0)
        return f" [{m.group(1).translate(SUPERSCRIPTS).replace('-', '–')}]"
    return SUP_RUN.sub(repl, text)


def _normalise_citations(body: str) -> str:
    body = _superscripts_to_brackets(body)
    # markdown-escaped brackets from some extractors: \[1\]
    body = re.sub(r"\\\[(\d[\d,\s\-–]*)\\\]", r"[\1]", body)
    # pymupdf4llm / markdownify sometimes render citations as links: [1](#ref1)
    body = re.sub(r"\[(\d[\d,\s\-–]*)\]\([^)]*\)", r"[\1]", body)
    return body


HEADING_BOLD = re.compile(r"^(#{1,6})\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*$")


# The first line of the paper proper. Everything above it is the title and author block,
# where superscript numbers mark affiliations, not citations.
FRONT_MATTER_END = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:\d+(?:\.\d+)*[.)]?\s+)?"
    r"(abstract|introduction|keywords?|key\s+words|summary|executive\s+summary|background|highlights)\b",
    re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TO_SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def _demote_affiliation_markers(lines: list[str]) -> tuple[list[str], int]:
    """Turn [n] back into superscript digits in the title/author block (before Abstract or
    Introduction) and on any line with an email address, where they are affiliation
    markers. Returns (lines, number demoted)."""
    end = next((i for i, l in enumerate(lines) if FRONT_MATTER_END.match(l)), 0)
    count = 0

    def demote(m: re.Match) -> str:
        nonlocal count
        count += 1
        return m.group(1).replace(" ", "").translate(_TO_SUP)

    out = []
    for i, line in enumerate(lines):
        if i < end or EMAIL_RE.search(line):
            line = re.sub(r"\s?\[(\d+(?:\s*,\s*\d+)*)\]", demote, line)
        out.append(line)
    return out, count


def _join_paragraphs(lines: list[str]) -> str:
    """Re-flow hard-wrapped lines into paragraphs; keep headings and blank lines."""
    out, buf = [], []
    for line in lines:
        s = HEADING_BOLD.sub(r"\1 \2", line.strip())
        if not s or s.startswith("#") or s.startswith("|") or s.startswith("```"):
            if buf:
                out.append(" ".join(buf))
                buf = []
            out.append(s)
        else:
            buf.append(s)
    if buf:
        out.append(" ".join(buf))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def split_reference_entries(lines: list[str]) -> tuple[list[tuple[str | None, str]], bool]:
    """Return ([(number or None, entry text)], already_numbered)."""
    lines = [re.sub(r"^\s*[-*•]\s+", "", l).replace("**", "").strip() for l in lines]
    numbered = sum(1 for l in lines if NUMBERED_ENTRY.match(l))
    first = next((l for l in lines if l), "")
    entries: list[list] = []
    if numbered >= 2 or (numbered == 1 and NUMBERED_ENTRY.match(first)):
        for l in lines:
            m = NUMBERED_ENTRY.match(l)
            if m:
                entries.append([m.group(1) or m.group(2), m.group(3)])
            elif l and entries:
                entries[-1][1] += " " + l
        return [(k, re.sub(r"\s+", " ", t).strip()) for k, t in entries], True
    # unnumbered: blank lines separate entries if the source has them
    blocks = [b for b in "\n".join(lines).split("\n\n") if b.strip()]
    if len(blocks) >= 2 and all(len(b.split("\n")) <= 6 for b in blocks):
        return [(None, re.sub(r"\s+", " ", b).strip()) for b in blocks], False
    # otherwise: a new entry starts on an author-like line after a line ending in "."
    current: list[str] = []
    out: list[tuple[str | None, str]] = []
    for l in (l for l in lines if l):
        if current and AUTHOR_START.match(l) and current[-1].rstrip().endswith((".", ")")):
            out.append((None, " ".join(current)))
            current = []
        current.append(l)
    if current:
        out.append((None, " ".join(current)))
    return [(None, re.sub(r"\s+", " ", t).strip()) for _, t in out], False


def to_markdown(raw: str, extractor: str = "text") -> Conversion:
    lines = _clean_lines(raw)
    conv = Conversion(markdown="", extractor=extractor)

    starts = [i for i, l in enumerate(lines) if REF_HEADING.match(l)]
    if not starts:
        conv.warnings.append("No References heading found. Add '## References' above the reference list, "
                             "then one numbered entry per line.")
        conv.markdown = _join_paragraphs([_normalise_citations(l) for l in lines]) + "\n"
        return conv
    start = starts[-1]
    end = next((j for j in range(start + 1, len(lines)) if AFTER_REFS.match(lines[j])), len(lines))
    body_lines, ref_lines, tail = lines[:start], lines[start + 1:end], lines[end:]

    entries, already = split_reference_entries(ref_lines)
    conv.references = len(entries)
    conv.numbered_by_converter = not already
    if not already:
        conv.warnings.append(f"Reference list wasn't numbered in the extracted text; numbered {len(entries)} "
                             "entries in order. Check this matches the article's numbering.")
    else:
        keys = [int(k) for k, _ in entries]
        if keys and keys != list(range(keys[0], keys[0] + len(keys))):
            conv.warnings.append("Reference numbers aren't a simple 1, 2, 3… sequence; check for entries "
                                 "that were split or merged.")
    numbered = [f"{k if already else i + 1}. {t}" for i, (k, t) in enumerate(entries)]

    body_lines, demoted = _demote_affiliation_markers([_normalise_citations(l) for l in body_lines])
    if demoted:
        conv.warnings.append(f"{demoted} superscript number(s) in the title and author block were treated as "
                             "affiliation markers, not citations.")
    body = _join_paragraphs(body_lines)
    from .parse import extract_claims, parse_references
    found_refs = parse_references("\n".join(numbered))
    if not extract_claims(body, found_refs):
        conv.warnings.append("No citations were found in the text, either numbered like [1] or author–year "
                             "like (Smith, 2020) matching the reference list, so only the reference list "
                             "can be checked. Superscript numbers in PDFs are often lost in extraction; if "
                             "the article uses them, try its Word or HTML version.")
    # sections after the reference list (appendix, acknowledgements…) go before it,
    # so the verifier doesn't mistake them for references
    if any(l.strip() for l in tail):
        body += "\n\n" + _join_paragraphs([_normalise_citations(l) for l in tail])
    conv.markdown = body + "\n\n## References\n\n" + "\n".join(numbered) + "\n"
    return conv


def convert_file(path: Path) -> Conversion:
    raw, extractor = extract(Path(path))
    return to_markdown(raw, extractor)


def inspect_markdown(md: str) -> dict:
    """What the verifier will see in this Markdown: used by the CLI, the API and the preview."""
    from .parse import citation_style, extract_claims, parse_references, split_manuscript, unresolved_citations
    body, ref_section = split_manuscript(md)
    refs = parse_references(ref_section)
    claims = extract_claims(body, refs)
    keys = {r.key for r in refs}
    cited = {k for c in claims for k in c.cited_keys}
    return {
        "references": len(refs),
        "with_identifier": sum(1 for r in refs if r.doi or r.arxiv_id or r.pmid),
        "claims": len(claims),
        "citation_style": citation_style(body, refs) if claims else "none",
        "missing_citations": unresolved_citations(claims, refs),
        "uncited_references": sorted(keys - cited, key=int),
        "has_reference_heading": bool(ref_section.strip()) or bool(refs),
    }
