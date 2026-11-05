"""Converter tests on generated PDF, Word and HTML articles that mimic journal layouts."""

import builtins
from pathlib import Path

import pytest

from rra.convert import convert_file, split_reference_entries, to_markdown
from rra.parse import extract_claims, parse_references, split_manuscript

REFS = [
    "LeCun Y, Bengio Y, Hinton G. (2015). Deep learning. Nature 521, 436–444. doi:10.1038/nature14539",
    "Vaswani A, Shazeer N, Parmar N, et al. (2017). Attention is all you need. arXiv:1706.03762",
    "Page MJ, McKenzie JE, Bossuyt PM, et al. (2021). The PRISMA 2020 statement: an updated guideline "
    "for reporting systematic reviews. BMJ 372, n71. doi:10.1136/bmj.n71",
]


def verifier_view(md: str):
    body, refs = split_manuscript(md)
    return parse_references(refs), extract_claims(body)


def check(md: str):
    refs, claims = verifier_view(md)
    assert [r.key for r in refs] == ["1", "2", "3"]
    assert refs[0].doi == "10.1038/nature14539" and refs[1].arxiv_id == "1706.03762"
    assert refs[2].doi == "10.1136/bmj.n71"            # wrapped entry re-joined
    assert {k for c in claims for k in c.cited_keys} == {"1", "2", "3"}
    assert "Acknowledg" not in md.split("## References")[1]   # tail moved out of refs
    return refs, claims


# ---------- PDF ----------

def make_pdf(path: Path):
    pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800

    def line(t, size=10):
        nonlocal y
        c.setFont("Helvetica", size)
        c.drawString(60, y, t)
        y -= 14

    line("Introduction", 13)
    line("Deep learning models learn representations with multiple levels of abstrac-")
    line("tion [1]. The Transformer relies entirely on attention mechanisms [2].")
    line("")
    line("Reporting guidance for systematic reviews was updated in 2021 [3].")
    line("1")                                  # page number
    c.showPage(); y = 800
    line("References", 13)
    line("1. " + REFS[0])
    line("2. " + REFS[1])
    line("3. " + REFS[2][:70])
    line(REFS[2][70:])                         # wrapped reference
    line("")
    line("Acknowledgements", 13)
    line("We thank 1. nobody in particular.")
    c.save()


@pytest.mark.parametrize("extractor", ["pymupdf4llm", "pypdf"])
def test_pdf(tmp_path, monkeypatch, extractor):
    if extractor == "pypdf":
        real = builtins.__import__
        monkeypatch.setattr(builtins, "__import__", lambda n, *a, **k:
                            (_ for _ in ()).throw(ImportError()) if n == "pymupdf4llm" else real(n, *a, **k))
    else:
        pytest.importorskip("pymupdf4llm")
    pytest.importorskip("pypdf")
    p = tmp_path / "paper.pdf"
    make_pdf(p)
    conv = convert_file(p)
    assert conv.extractor == extractor
    check(conv.markdown)
    assert "abstraction [1]" in conv.markdown            # hyphenated line break re-joined


# ---------- Word: auto-numbered references, superscript citations ----------

def make_docx(path: Path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Introduction", level=1)
    p = d.add_paragraph("Deep learning models learn representations with multiple levels of abstraction")
    r = p.add_run("1"); r.font.superscript = True
    p.add_run(". The Transformer relies entirely on attention mechanisms")
    r = p.add_run("2"); r.font.superscript = True
    p.add_run(".")
    p = d.add_paragraph("Reporting guidance for systematic reviews was updated in 2021")
    r = p.add_run("3"); r.font.superscript = True
    p.add_run(".")
    d.add_heading("References", level=1)
    for ref in REFS:
        d.add_paragraph(ref, style="List Number")      # Word numbering is not in the text
    d.add_heading("Acknowledgements", level=1)
    d.add_paragraph("Thanks to the reviewers.")
    d.save(str(path))


def test_docx(tmp_path):
    p = tmp_path / "paper.docx"
    make_docx(p)
    conv = convert_file(p)
    check(conv.markdown)
    assert conv.numbered_by_converter and any("numbered 3 entries" in w for w in conv.warnings)


# ---------- HTML: <sup><a> citations, <ol> reference list, page chrome ----------

def test_html(tmp_path):
    pytest.importorskip("markdownify")
    items = "".join(f"<li>{r}</li>" for r in REFS)
    html = f"""<html><head><script>x()</script></head><body><nav>Journal menu</nav><article>
    <h2>Introduction</h2>
    <p>Deep learning models learn representations with multiple levels of abstraction<sup><a href="#r1">1</a></sup>.
    The Transformer relies entirely on attention mechanisms<sup>2</sup>.</p>
    <p>Reporting guidance for systematic reviews was updated in 2021<sup>3</sup>.</p>
    <h2>References</h2><ol>{items}</ol>
    <h2>Acknowledgements</h2><p>Thanks.</p></article><footer>© Publisher</footer></body></html>"""
    p = tmp_path / "paper.html"
    p.write_text(html, encoding="utf-8")
    conv = convert_file(p)
    check(conv.markdown)
    assert "Journal menu" not in conv.markdown and "Publisher" not in conv.markdown


# ---------- clean-up rules ----------

def test_unicode_superscripts_and_bracketed_numbers():
    conv = to_markdown("Text with a claim¹,² and another³.\n\nREFERENCES\n[1] A. x.\n[2] B. y.\n[3] C. z.\n")
    refs, claims = verifier_view(conv.markdown)
    assert [r.key for r in refs] == ["1", "2", "3"]
    assert claims[0].cited_keys == ["1", "2", "3"]


def test_unnumbered_references_split_on_author_lines():
    entries, numbered = split_reference_entries([
        "Smith, J., & Jones, K. (2020). A long title that",
        "wraps onto a second line. Journal 1, 2–3.",
        "Brown, A. (2019). Another paper. Journal 4, 5.",
    ])
    assert not numbered and len(entries) == 2 and entries[0][1].endswith("Journal 1, 2–3.")


def test_warns_when_no_references_heading():
    conv = to_markdown("Just a body [1].")
    assert any("No References heading" in w for w in conv.warnings)


def test_warns_on_author_year_style():
    conv = to_markdown("A claim (Smith, 2020).\n\nReferences\n\nSmith, J. (2020). Title. J.\n")
    assert any("Author–year" in w for w in conv.warnings)


def test_cli_does_not_overwrite(tmp_path, capsys):
    from rra.cli import main
    src = tmp_path / "a.txt"
    src.write_text("Claim [1].\n\nReferences\n1. X. (2020). Title here ok. J.\n", encoding="utf-8")
    assert main(["convert", str(src)]) == 0
    assert (tmp_path / "a.md").exists()
    assert main(["convert", str(src)]) == 2                    # refuses to overwrite
    assert main(["convert", str(src), "--force"]) == 0


def test_single_numbered_reference_not_renumbered():
    conv = to_markdown("# **Title**\n\nA claim [1].\n\nReferences\n1. Smith J. (2020). A title here. J.\n")
    assert "1. Smith J." in conv.markdown and "1. 1." not in conv.markdown
    assert conv.markdown.startswith("# Title")          # bold markers removed from headings


def test_docx_keeps_hyperlinks_fields_and_split_superscripts(tmp_path):
    docx = pytest.importorskip("docx")
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def run_el(text, sup=False, deleted=False, instr=False):
        r = OxmlElement("w:r")
        if sup:
            rpr = OxmlElement("w:rPr"); va = OxmlElement("w:vertAlign")
            va.set(qn("w:val"), "superscript"); rpr.append(va); r.append(rpr)
        t = OxmlElement("w:delText" if deleted else "w:instrText" if instr else "w:t")
        t.set(qn("xml:space"), "preserve"); t.text = text; r.append(t)
        return r

    d = docx.Document()
    p = d.add_paragraph("Claim one")
    sdt = OxmlElement("w:sdt"); content = OxmlElement("w:sdtContent")   # Mendeley/EndNote content control
    content.append(run_el("1", sup=True)); sdt.append(content); p._p.append(sdt)
    p.add_run(". Claim two")
    for piece in ["2", ",", "3"]:                                          # Word splits superscripts
        p._p.append(run_el(piece, sup=True))
    p._p.append(run_el(' ADDIN ZOTERO_ITEM CSL_CITATION {"big": "json"} ', instr=True))  # field code
    dele = OxmlElement("w:del"); dele.append(run_el(" deleted text", deleted=True)); p._p.append(dele)
    p.add_run(". Claim three")
    h = OxmlElement("w:hyperlink"); h.set(qn("w:anchor"), "r1"); h.append(run_el("[1]")); p._p.append(h)
    p.add_run(".")
    d.add_heading("References", 1)
    ref = d.add_paragraph("1. LeCun Y, Bengio Y, Hinton G. (2015). Deep learning. Nature 521, 436–444. ")
    h = OxmlElement("w:hyperlink"); h.append(run_el("https://doi.org/10.1038/nature14539")); ref._p.append(h)
    d.add_paragraph("2. Vaswani A, et al. (2017). Attention is all you need. arXiv:1706.03762")
    d.add_paragraph("3. Page MJ, et al. (2021). The PRISMA 2020 statement. BMJ 372, n71. doi:10.1136/bmj.n71")
    path = tmp_path / "fields.docx"
    d.save(str(path))

    md = convert_file(path).markdown
    assert "Claim one[1]. Claim two[2,3]. Claim three[1]." in md
    assert "ZOTERO" not in md and "deleted text" not in md
    refs, claims = verifier_view(md)
    assert refs[0].doi == "10.1038/nature14539"
    assert [c.cited_keys for c in claims] == [["1"], ["2", "3"], ["1"]]


def test_author_affiliation_superscripts_are_not_citations(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("Supply chain resilience in UK ports", style="Title")
    p = d.add_paragraph("Ada Okafor")
    r = p.add_run("1*"); r.font.superscript = True
    p.add_run(", Ben Hart")
    r = p.add_run("2"); r.font.superscript = True
    p.add_run(", and Cara Lim")
    r = p.add_run("3"); r.font.superscript = True
    p = d.add_paragraph("")
    r = p.add_run("2"); r.font.superscript = True
    p.add_run("University of Hull, UK, b.hart@example.ac.uk")
    d.add_paragraph("Abstract")
    p = d.add_paragraph("Ports are critical infrastructure")
    r = p.add_run("1"); r.font.superscript = True
    p.add_run(".")
    d.add_heading("References", 1)
    d.add_paragraph("1. Smith J. (2020). Port resilience. Maritime Policy 4(2), 1–10. doi:10.1000/x")
    path = tmp_path / "authors.docx"
    d.save(str(path))
    conv = convert_file(path)
    refs, claims = verifier_view(conv.markdown)
    assert [c.cited_keys for c in claims] == [["1"]]                       # only the real citation
    assert "Ben Hart²" in conv.markdown and "Cara Lim³" in conv.markdown
    assert any("affiliation markers" in w for w in conv.warnings)


def test_notation_superscripts_and_hidden_link_targets(tmp_path):
    docx = pytest.importorskip("docx")
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    d = docx.Document()
    d.add_paragraph("Abstract")
    p = d.add_paragraph("The cluster emits CO")
    for text in ["2", " and uses 40 kWh/m", "2", " of floor area, about 10", "6", " tonnes overall. Ports matter"]:
        r = p.add_run(text)
        r.font.superscript = text in ("2", "6")
    r = p.add_run("1"); r.font.superscript = True
    p.add_run(".")
    d.add_heading("References", 1)
    ref = d.add_paragraph("1. BSI (2011), PAS 2050, Available at: ")
    rid = ref.part.relate_to("https://knowledge.bsigroup.com/products/pas-2050", RT.HYPERLINK, is_external=True)
    h = OxmlElement("w:hyperlink"); h.set(qn("r:id"), rid)
    run = OxmlElement("w:r"); t = OxmlElement("w:t"); t.text = "PAS 2050:2011 | BSI Knowledge"
    run.append(t); h.append(run); ref._p.append(h)
    ref.add_run(". [Accessed Jul 3, 2025].")
    path = tmp_path / "notation.docx"
    d.save(str(path))
    md = convert_file(path).markdown
    assert "CO² and uses 40 kWh/m² of floor area, about 10⁶ tonnes" in md
    refs, claims = verifier_view(md)
    assert [c.cited_keys for c in claims] == [["1"]]
    assert refs[0].url == "https://knowledge.bsigroup.com/products/pas-2050"


def test_unicode_notation_left_alone():
    conv = to_markdown("Abstract\n\nIt emits CO² over 25 m² and 10⁶ t, as shown¹.\n\nReferences\n1. A. (2020). Title of it. J 1, 2-3.\n")
    assert "CO² over 25 m² and 10⁶ t, as shown [1]." in conv.markdown
