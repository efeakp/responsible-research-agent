"""Author–year citation matching. Synthetic sentences; references use public metadata."""

from rra.parse import citation_style, extract_claims, parse_references, unresolved_citations

REFS = parse_references("""
1. Boiral, O., Henri, J. & Talbot, D. (2012) Modeling the impacts of corporate commitment on climate change. Business Strategy and the Environment, 21 (8), 495–516.
2. Braun, V. & Clarke, V. (2006) Using thematic analysis in psychology. Qualitative Research in Psychology, 3 (2), 77–101.
3. Intergovernmental Panel on Climate Change [IPCC] (2023) Climate change 2022 – impacts, adaptation and vulnerability. Cambridge University Press.
4. Smith, A. (2020a) First paper. J 1, 1–2.
5. Smith, A. (2020b) Second paper. J 2, 3–4.
6. Van Stijn, A., Eberhardt, L., Jansen, B. W. & Meijer, A. (2022) Environmental design guidelines. Journal of Cleaner Production, 357 131375.
7. World Resources Institute and World Business Council for Sustainable Development, (2011) Corporate value chain (scope 3) accounting and reporting standard.
8. Marttinen, K. & Kähkönen, A. (2022) Fostering firms' ability to cascade sustainability. IJOPM, 42 (8), 1146–1172.
9. Pre-Sustainability (2021) Life cycle-based sustainability standards and guidelines.
""")


def claims(text):
    return extract_claims(text, REFS)


def test_style_detection():
    assert citation_style("A claim (Boiral et al., 2012).", REFS) == "author_year"
    assert citation_style("A claim [1]. Another [2].", REFS) == "numeric"


def test_parenthetical_forms():
    c = claims("Firms matter (Boiral et al., 2012). Analysis followed a method (Braun & Clarke, 2006, p. 79). "
               "Warming is severe (Intergovernmental Panel on Climate Change [IPCC], 2023). Also (IPCC, 2023). "
               "Design matters (see Van Stijn et al., 2022). Standards exist (World Resources Institute and "
               "World Business Council for Sustainable Development, 2011; Pre-sustainability, 2021). "
               "Power shapes cascading (Marttinen & Kahkonen, 2022).")
    assert [x.cited_keys for x in c] == [["1"], ["2"], ["3"], ["3"], ["6"], ["7", "9"], ["8"]]
    assert all(not x.unresolved for x in c)


def test_narrative_forms():
    c = claims("Braun and Clarke (2006) describe the method. Boiral et al. (2012) studied firms. "
               "In Hull (2020) nothing is cited.")
    assert [x.cited_keys for x in c] == [["2"], ["1"]]
    assert c[0].plain == "Braun and Clarke describe the method."


def test_year_suffix_and_ambiguity():
    c = claims("One (Smith, 2020a). Both (Smith, 2020a, 2020b). Unclear (Smith, 2020).")
    assert c[0].cited_keys == ["4"] and c[1].cited_keys == ["4", "5"]
    assert c[2].cited_keys == [] and "more than one reference ([4], [5])" in c[2].unresolved[0]


def test_unmatched_and_malformed_citations_are_reported():
    c = claims("Data sharing helps (Boiral et al., 2012; Vedat et al., 2024). Standards (BSI, 201; Pre-sustainability, 2021).")
    assert c[0].cited_keys == ["1"] and c[0].unresolved == ["Vedat et al., 2024: no matching reference"]
    assert c[1].cited_keys == ["9"] and c[1].unresolved == ["BSI, 201: the year looks wrong"]
    assert unresolved_citations(c, REFS) == ["Vedat et al., 2024: no matching reference",
                                             "BSI, 201: the year looks wrong"]


def test_non_citation_brackets_are_ignored():
    c = claims("Emissions fell (from 2015 to 2020). Output rose (in 2021). Sample size was large (n = 2020). "
               "Results are in a table (Table 2, 2020 data).")
    assert c == []


def test_plain_text_drops_citations_for_the_support_check():
    c = claims("Scope 3 is over 70% of emissions (Boiral et al., 2012, p. 500; Braun & Clarke, 2006).")
    assert c[0].plain == "Scope 3 is over 70% of emissions."
    from rra.support import number_issues
    assert number_issues(c[0].plain, "scope 3 is over 70% of emissions") == []
