"""export.py — keyed v2 bibliography lines must export under their own key
(issue #10). Pure parsing, no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.export import (  # noqa: E402
    extract_authors_year_title,
    parse_bib_entries,
    split_keyed_entry,
    to_bibtex_entry,
)

LAVENDER = ("[Abraham, 2024] — 'Lavender': The AI machine directing Israel's bombing spree in Gaza"
            " — https://www.972mag.com/lavender-ai-israeli-army-gaza/ (2024-04-03) [news · journalism]"
            " <!-- gap_gap_israel-lavender-gospel_a5b3325cbd28.md -->")


def _fields(bib: str) -> dict:
    out = {}
    for line in bib.splitlines()[1:-1]:
        name, _, value = line.strip().partition("=")
        out[name.strip()] = value.strip().rstrip(",").strip("{}")
    return out


def test_keyed_line_exports_under_its_own_key():
    bt = to_bibtex_entry(LAVENDER, {})
    assert bt.startswith("@misc{Abraham2024,")
    f = _fields(bt)
    assert f["author"] == "Abraham"
    assert f["year"] == "2024"
    assert f["title"] == "'Lavender': The AI machine directing Israel's bombing spree in Gaza"
    assert f["url"] == "https://www.972mag.com/lavender-ai-israeli-army-gaza/"
    assert f["keywords"] == "news, journalism"
    assert "[" not in f["title"] and "—" not in f["title"]


def test_organisation_head_with_leading_plus_sign():
    entry = ("[+972 Magazine, 2024] — 'Lavender': The AI machine directing Israel's bombing spree"
             " in Gaza — https://www.972mag.com/lavender-ai-israeli-army-gaza/ (2024-04-03)"
             " [news · journalism]")
    key, year, authors, title = extract_authors_year_title(entry)
    assert (key, year, authors) == ("972Magazine", "2024", "+972 Magazine")  # one organisation, unsplit
    assert title.startswith("'Lavender'")
    assert to_bibtex_entry(entry, {}).startswith("@misc{972Magazine2024,")


def test_multi_author_head_keys_on_the_first_author():
    entry = ("[Babuta, Oswald & Janjeva, 2020] — Artificial Intelligence and UK National Security:"
             " Policy Considerations — https://static.rusi.org/ai_national_security_final_web_version.pdf"
             " ((RUSI) [think-tank report · research]")
    bt = to_bibtex_entry(entry, {})
    assert bt.startswith("@misc{Babuta2020,")
    f = _fields(bt)
    assert f["author"] == "Babuta and Oswald and Janjeva"   # BibTeX name separator
    assert f["title"] == "Artificial Intelligence and UK National Security: Policy Considerations"
    assert f["keywords"] == "think-tank report, research"


def test_undated_keyed_line_keeps_nd_year_and_sanitised_key():
    entry = ("[Anduril, n.d.] — Command & Control (Anduril Lattice product page)"
             " — https://www.anduril.com/lattice/command-and-control ((none) [corporate · vendor]")
    bt = to_bibtex_entry(entry, {})
    assert bt.startswith("@misc{Andurilnd,")
    f = _fields(bt)
    assert f["year"] == "n.d."
    assert f["title"] == "Command \\& Control (Anduril Lattice product page)"


def test_pdf_marker_and_site_segment_handling():
    entry = ("[ASPI, 2023] — [PDF] ASPI's Critical Technology Tracker: the global race for future power"
             " - AWS — http://ad-aspi.s3.ap-southeast-2.amazonaws.com/2023-03/x.pdf (2023-01-01)"
             " [think-tank report · research]")
    authors, year, title, tag = split_keyed_entry(entry)
    assert title == "ASPI's Critical Technology Tracker: the global race for future power - AWS"
    # a second — segment before the URL is kept as part of the title (it is
    # the site/outlet, which the exporter has no separate field for)
    entry2 = ("[ATLA, n.d.] — What is the transfer of defense equipment and technology?"
              " — DETRAP Portal (ATLA, Ministry of Defense) — https://detrap-portal.jp/en/basic/about/"
              " ((undated)) [government doc · official]")
    _, _, title2, tag2 = split_keyed_entry(entry2)
    assert title2 == ("What is the transfer of defense equipment and technology?"
                      " — DETRAP Portal (ATLA, Ministry of Defense)")
    assert tag2 == "government doc · official"


def test_keyed_line_without_tag_or_separator():
    entry = "[Kanda, 2005] Kanda, Fusae. \"Behind the Sensationalism.\" The Art Bulletin 87/1 (2005): 24–49."
    key, year, authors, title = extract_authors_year_title(entry)
    assert (key, year, authors) == ("Kanda", "2005", "Kanda")
    assert title.startswith("Kanda, Fusae")
    bt = to_bibtex_entry(entry, {})
    assert bt.startswith("@misc{Kanda2005,") and "keywords" not in bt


def test_unkeyed_academic_line_is_unchanged():
    entry = "Smith, J. (2024). A study of things. Journal of Stuff, 1(2), 3-4. https://doi.org/10.1234/abc"
    assert split_keyed_entry(entry) is None
    key, year, authors, title = extract_authors_year_title(entry)
    assert (key, year) == ("Smith", "2024")
    assert title == "A study of things"


def test_duplicate_keyed_heads_get_suffixes_not_anon():
    bib = "\n".join([
        "- [Abraham, 2024] — First piece — https://www.972mag.com/a/ (2024-04-03) [news · journalism]",
        "- [Abraham, 2024] — Second piece — https://www.972mag.com/b/ (2024-05-03) [news · journalism]",
        "- [Abraham, 2025] — Third piece — https://www.972mag.com/c/ (2025-03-06) [news · journalism]",
    ])
    counter = {}
    keys = [to_bibtex_entry(e, counter).split(",")[0] for e in parse_bib_entries(bib)]
    # (the existing collision scheme letters the second entry "b")
    assert keys == ["@misc{Abraham2024", "@misc{Abraham2024b", "@misc{Abraham2025"]


def test_bibtex_authors_from_head_separators():
    from scripts.export import _bibtex_authors_from_head as names
    assert names("Kanda") == "Kanda"
    assert names("Beck & Plumb") == "Beck and Plumb"
    assert names("Babuta, Oswald & Janjeva") == "Babuta and Oswald and Janjeva"
    assert names("Smith et al.") == "Smith and others"
    assert names("Lee and Park") == "Lee and Park"


def test_historical_year_takes_the_keyed_path_end_to_end():
    """[Darwin, 1859] is a valid keyed line (the inline grammar accepts any
    four-digit year); it used to fall through to Anonnd / n.d."""
    bib = "- [Darwin, 1859] — On the Origin of Species — https://example.org/origin [book · primary]\n"
    entries = parse_bib_entries(bib)
    assert len(entries) == 1
    bt = to_bibtex_entry(entries[0], {})
    assert bt.startswith("@misc{Darwin1859,")
    f = _fields(bt)
    assert f["author"] == "Darwin" and f["year"] == "1859"
    assert f["title"] == "On the Origin of Species"
    assert f["keywords"] == "book, primary"


def test_historical_keyed_line_without_url_is_kept():
    """parse_bib_entries used to require a 19xx/20xx year, a URL or n.d.; a
    keyed [Darwin, 1859] with no URL was dropped before export."""
    bib = "- [Darwin, 1859] — On the Origin of Species. London: John Murray. [book · primary]\n"
    entries = parse_bib_entries(bib)
    assert len(entries) == 1
    bt = to_bibtex_entry(entries[0], {})
    assert bt.startswith("@misc{Darwin1859,")
    f = _fields(bt)
    assert f["year"] == "1859"
    assert f["title"] == "On the Origin of Species. London: John Murray"


def test_organisation_heads_stay_one_braced_author():
    """A head with a multi-word piece is one organisation: never split into
    invented authors, braced so BibTeX does not split it on its own 'and'."""
    gates = "[Bill and Melinda Gates Foundation, 2023] — Annual letter — https://x.org/a [ngo · report]"
    bt = to_bibtex_entry(gates, {})
    assert bt.startswith("@misc{BillandMelindaGatesFoundation2023,")
    assert _fields(bt)["author"] == "Bill and Melinda Gates Foundation"
    assert "author    = {{Bill and Melinda Gates Foundation}}" in bt

    mod = "[Ministry of Defence, Japan, 2024] — White paper — https://mod.go.jp/w [government doc · official]"
    bt = to_bibtex_entry(mod, {})
    assert bt.startswith("@misc{MinistryofDefenceJapan2024,")
    assert "author    = {{Ministry of Defence, Japan}}" in bt

    plus = ("[+972 Magazine, 2024] — 'Lavender' — https://www.972mag.com/l/ (2024-04-03)"
            " [news · journalism]")
    assert "author    = {{+972 Magazine}}" in to_bibtex_entry(plus, {})


def test_surname_lists_still_split_and_et_al_still_others():
    from scripts.export import _bibtex_author_field as field
    assert field("Babuta, Oswald & Janjeva") == "Babuta and Oswald and Janjeva"
    assert field("Smith et al.") == "Smith and others"
    assert field("World Health Organization et al.") == "{World Health Organization} and others"
    # the known limit: an organisation of single words reads as surnames
    assert field("Ernst & Young") == "Ernst and Young"


def test_year_suffix_stays_in_key_not_in_year_field():
    entry = "[Smith, 2024a] — Part A — https://x.org/b [news · journalism]"
    bt = to_bibtex_entry(entry, {})
    assert bt.startswith("@misc{Smith2024a,")
    assert _fields(bt)["year"] == "2024"
