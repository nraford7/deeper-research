#!/usr/bin/env python3
"""
export.py — emit BibTeX, claims JSONL, and Research Bible HTML.

Inputs:
  - A directory containing section markdown files with [Author, Year] cites
  - A master bibliography file

Outputs:
  - bibliography.bib  : BibTeX entries (one per bib row, key = AuthorYear)
  - claims.jsonl      : one row per inline citation with file + surrounding sentence
  - Research Bible HTML: jimemo when compatible, otherwise a built-in page

Usage:
  python3 export.py --sections research/topic/sections/ \
      --bibliography research/topic/sections/bibliography.md \
      --output-dir research/topic/export/
"""

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

# Support both documented direct execution (`python3 scripts/export.py`) and
# package imports (`python3 -m scripts.export`, tests).
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.research_bible_html import (
    assembled_html_name,
    build_document,
    export_html,
    resolve_bible_path,
)
from scripts.helper_runtime import require_managed_mutation, standalone_mutation_guard
from scripts.run_fs import RootedFS
from scripts.run_layout import LayoutKind, RunLayout, safe_relpath
from scripts.run_transactions import broker_request


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\)\]]+")
YEAR_RE = re.compile(r"\b(19|20)\d{2}[a-z]?\b")
# INLINE_CITE_RE is kept BYTE-FOR-BYTE identical to verify_citations.py's copy
# (two named alternatives — `domain` for bare lowercase web domains, `author`
# for academic author heads — plus a shared year group that accepts n.d.). If
# these diverge, n.d. / digit-domain web cites never reach claims.jsonl.
_AUTHOR_GROUP = r"(?:[A-Za-z][A-Za-z\.\-' ]{0,80}?)"
_AUTHOR_ALT = rf"{_AUTHOR_GROUP}(?:\s+(?:et\s+al\.?|&\s+{_AUTHOR_GROUP}|and\s+{_AUTHOR_GROUP}))?"
_DOMAIN_ALT = r"[a-z0-9-]+(?:\.[a-z0-9-]+)+"
_YEAR_ALT = r"\d{4}[a-z]?|n\.d\."
INLINE_CITE_RE = re.compile(
    rf"[\[\(]\s*(?:(?P<domain>{_DOMAIN_ALT})|(?P<author>{_AUTHOR_ALT})),?\s*(?P<year>{_YEAR_ALT})\s*[\]\)]"
)
# KB citations — kept BYTE-FOR-BYTE identical to verify_citations.py's
# KB_CITE_RE (same alignment rule as INLINE_CITE_RE above). If these diverge,
# KB cites never reach claims.jsonl.
KB_CITE_RE = re.compile(r"\[\s*kb:(?P<slug>[a-z0-9][a-z0-9-]*)\s*,\s*(?P<kbyear>\d{4}|n\.d\.)\s*\]")


def _kb_cite_dict(m):
    return {"author": f"kb:{m.group('slug')}", "year": m.group("kbyear"), "kind": "kb"}


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
BIB_BULLET_RE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)")


def parse_bib_entries(text: str):
    """Parse bibliography list entries. Skip prose lines (frontmatter,
    section dividers, dedup notes). A real entry is a bullet/numbered list
    item containing a 4-digit year."""
    entries = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if not BIB_BULLET_RE.match(line):
            continue
        body = BIB_BULLET_RE.sub("", line, count=1).strip()
        if len(body) < 30:
            continue
        # A real academic entry has a 4-digit year. A WEB entry may be undated
        # (n.d.) — accept it when it carries a URL or an explicit n.d. marker so
        # year-less web sources are not silently dropped. A keyed head
        # ([Darwin, 1859] — …) is an entry whatever its year.
        if not YEAR_RE.search(body):
            if not (URL_RE.search(body) or "n.d." in body.lower()
                    or _KEYED_HEAD_RE.match(body)):
                continue
        entries.append(body)
    return entries


def bibtex_escape(s: str) -> str:
    return s.replace("{", "\\{").replace("}", "\\}").replace("%", "\\%").replace("$", "\\$").replace("&", "\\&")


_LEADING_DOMAIN_RE = re.compile(r"^\s*([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b")
_ND_RE = re.compile(r"\bn\.d\.", re.IGNORECASE)

# Keyed v2 bibliography lines lead with their citation key:
#   [Abraham, 2024] — 'Lavender': The AI machine … — https://… (2024-04-03) [news · journalism]
#   [Anduril, n.d.] — Command & Control — https://… ((none) [corporate · vendor]
# The head IS the author/year signal; the trailing [tier · kind] tag is the
# run's own classification and the optional <!-- … --> comment is provenance.
# Any four-digit year, like the inline citation grammar (a keyed [Darwin, 1859]
# is a valid bibliography line), not just 19xx/20xx.
_KEYED_HEAD_RE = re.compile(
    r"^\s*\[(?P<authors>[^\[\]]+?),\s*(?P<year>\d{4}[a-z]?|n\.d\.)\s*\]\s*(?:—\s*)?")
_TRAILING_TAG_RE = re.compile(r"\s*\[(?P<tag>[^\[\]]*·[^\[\]]*)\]\s*$")
_HTML_COMMENT_RE = re.compile(r"\s*<!--.*?-->\s*$", re.S)
_AUTHOR_SPLIT_RE = re.compile(r"\s*(?:,|&|;|\band\b|\bet\s+al\b)\s*")
_KEYED_SEP = " — "


def split_keyed_entry(entry: str):
    """Parse a keyed v2 line into (authors, year, title, tag) or None when the
    line does not start with a ``[Key, YYYY]`` / ``[Key, n.d.]`` head.

    The title is the ``—``-separated text between the head and the URL, minus a
    short leading marker such as ``[PDF]``; ``tag`` is the trailing
    ``tier · kind`` text without its brackets (None when absent)."""
    m = _KEYED_HEAD_RE.match(entry)
    if not m:
        return None
    rest = _HTML_COMMENT_RE.sub("", entry[m.end():])
    tag = None
    tm = _TRAILING_TAG_RE.search(rest)
    if tm:
        tag = tm.group("tag").strip()
        rest = rest[:tm.start()]
    title_segs = []
    for seg in (s.strip() for s in rest.split(_KEYED_SEP)):
        if URL_RE.match(seg):
            break
        if seg:
            title_segs.append(seg)
    title = _KEYED_SEP.join(title_segs)
    url_m = URL_RE.search(title)
    if url_m:                     # URL glued to the title with no separator
        title = title[:url_m.start()]
    title = re.sub(r"^\[[^\]]{1,12}\]\s*", "", title)   # [PDF], [JA], …
    title = title.strip(" .,;")
    if len(title) > 1 and title[0] == title[-1] and title[0] in "\"'":
        title = title[1:-1].strip()   # a fully quoted title, not a leading 'scare quote'
    authors = m.group("authors").strip()
    return authors, m.group("year"), (title or authors)[:300], tag


_ET_AL_RE = re.compile(r"\bet\s+al\b\.?")
# Lowercase surname particles, as in the citation grammar's [van der Berg, 2020].
_SURNAME_PARTICLES = frozenset(
    "van von der den de del della di da du dos das le la al bin ibn ter ten".split())


def _is_surname(piece: str) -> bool:
    """One surname, allowing lowercase particles before it (``van der Berg``)."""
    words = piece.split()
    return len([w for w in words[:-1] if w not in _SURNAME_PARTICLES]) == 0


def _split_head(authors: str):
    """(names, others, organisation) for a keyed head. ``names`` are the
    pieces between separators; ``others`` is True for ``et al.``. A head is an
    organisation when any piece is more than one surname: a surname list
    (``Babuta, Oswald & Janjeva``, ``van der Berg & Smith``) is single
    surnames, with lowercase particles allowed, while ``Bill and Melinda Gates
    Foundation`` or ``Ministry of Defence, Japan`` is not, and splitting those
    would invent authors. An organisation whose pieces are all single
    words (``Ernst & Young``) still reads as a surname list; the raw head stays
    in the ``note`` field."""
    text = authors.strip()
    others = bool(_ET_AL_RE.search(text))
    text = _ET_AL_RE.sub("", text).strip(" ,;")
    names = [n.strip() for n in _AUTHOR_SPLIT_RE.split(text) if n.strip()]
    organisation = not all(_is_surname(n) for n in names)
    if organisation:
        names = [text]
    return names, others, organisation


def _bibtex_authors_from_head(authors: str) -> str:
    """Plain author names from a keyed head, joined with ``and``, which is the
    only separator BibTeX reads (``Babuta, Oswald & Janjeva`` → ``Babuta and
    Oswald and Janjeva``; ``Smith et al.`` → ``Smith and others``). An
    organisation comes back whole (see _split_head)."""
    names, others, _org = _split_head(authors)
    if others:
        names = names + ["others"]
    return " and ".join(names) if names else authors.strip()


def _bibtex_author_field(authors: str) -> str:
    """Escaped BibTeX author value for a keyed head. An organisation is
    wrapped in braces so BibTeX reads it as one name and never splits it on
    the ``and`` inside it (``{Bill and Melinda Gates Foundation}``)."""
    names, others, org = _split_head(authors)
    if not names:
        return bibtex_escape(authors.strip())
    parts = ["{" + bibtex_escape(n) + "}" if org else bibtex_escape(n) for n in names]
    if others:
        parts.append("others")
    return " and ".join(parts)


def _author_key_from_head(authors: str) -> str:
    """BibTeX key stem from a keyed head, letters and digits only: the first
    author of a surname list (``Babuta, Oswald & Janjeva`` → ``Babuta``), or
    the whole organisation (``+972 Magazine`` → ``972Magazine``; ``Bill and
    Melinda Gates Foundation`` → ``BillandMelindaGatesFoundation``)."""
    names, _others, _org = _split_head(authors)
    first = names[0] if names else authors
    return re.sub(r"[^A-Za-z0-9]", "", first) or "Anon"


def extract_authors_year_title(entry: str):
    """Split a bibliography line into (author_key, year, authors, title).

    Academic lines have a 4-digit year. Undated WEB lines (no year) are handled
    separately: the leading token is a bare domain, the year is `n.d.`, and the
    title is the text between the `n.d.` marker (or the domain) and the URL — so
    the author/title fields are clean instead of a truncated dot-split.
    Keyed v2 lines (``[Key, YYYY] — title — url …``) take their author and
    year from the head, see split_keyed_entry."""
    keyed = split_keyed_entry(entry)
    if keyed:
        authors, year, title, _tag = keyed
        return (_author_key_from_head(authors), year,
                _bibtex_authors_from_head(authors), title)

    year_m = YEAR_RE.search(entry)
    if year_m:
        year = year_m.group(0)
        pre = entry[:year_m.start()].rstrip(" (.,")
        author_first = re.match(r"([A-Z][A-Za-z\-']+)", pre)
        if author_first:
            author_key = author_first.group(1)
        else:
            # A dated WEB entry leads with a bare lowercase domain — key off it.
            dom_m = _LEADING_DOMAIN_RE.match(entry)
            author_key = re.sub(r"[^A-Za-z0-9]", "", dom_m.group(1)) if dom_m else "Anon"
        post = entry[year_m.end():].strip(" .,)")
        title_m = re.match(r"[^.\"]+", post)
        title = title_m.group(0).strip(" \"'.,") if title_m else post[:200]
        return author_key, year, pre.strip(), title

    # Undated web entry: pull the leading domain as author + BibTeX key stem.
    dom_m = _LEADING_DOMAIN_RE.match(entry)
    if dom_m:
        domain = dom_m.group(1)
        # BibTeX keys must be alphanumeric-ish; strip dots/hyphens from the stem.
        author_key = re.sub(r"[^A-Za-z0-9]", "", domain) or "Anon"
        authors = domain
    else:
        author_key = "Anon"
        authors = ""
    # Title = text after the n.d. marker (or the domain), up to the first URL.
    tail = entry
    nd_m = _ND_RE.search(entry)
    if nd_m:
        tail = entry[nd_m.end():]
    elif dom_m:
        tail = entry[dom_m.end():]
    tail = URL_RE.sub("", tail)
    title = tail.strip(" ().,\"'").strip()
    title = re.split(r"(?<=[.!?])\s", title, maxsplit=1)[0].strip(" .\"'")
    if not title:
        title = authors or "Untitled"
    return author_key, "n.d.", authors, title[:300]


def to_bibtex_entry(entry: str, author_year_counter: dict) -> str:
    author_key, year, authors, title = extract_authors_year_title(entry)
    doi_m = DOI_RE.search(entry)
    url_m = URL_RE.search(entry)
    # BibTeX cite keys must not contain punctuation like the '.' in "n.d." —
    # sanitize the key stem (the `year` FIELD below keeps its literal value).
    year_key = re.sub(r"[^A-Za-z0-9]", "", year) or "nd"
    base = f"{author_key}{year_key}"
    seen = author_year_counter.get(base, 0)
    author_year_counter[base] = seen + 1
    if seen == 0:
        key = base
    elif seen < 26:
        key = f"{base}{chr(ord('a') + seen)}"
    else:
        key = f"{base}_{seen}"
    keyed = split_keyed_entry(entry)
    author_field = _bibtex_author_field(keyed[0]) if keyed else bibtex_escape(authors)
    # The key keeps a disambiguating letter (Smith2024a); the year field is
    # the year itself.
    year_field = re.sub(r"^(\d{4})[a-z]$", r"\1", year)
    fields = [f"  author    = {{{author_field}}}",
              f"  year      = {{{year_field}}}",
              f"  title     = {{{bibtex_escape(title)}}}"]
    if doi_m:
        fields.append(f"  doi       = {{{doi_m.group(0)}}}")
    if url_m:
        fields.append(f"  url       = {{{url_m.group(0)}}}")
    if keyed and keyed[3]:
        # the run's own [tier · kind] tag, as comma-separated keywords
        parts = [t.strip() for t in keyed[3].split("·") if t.strip()]
        fields.append(f"  keywords  = {{{bibtex_escape(', '.join(parts))}}}")
    fields.append(f"  note      = {{{bibtex_escape(entry[:400])}}}")
    return "@misc{" + key + ",\n" + ",\n".join(fields) + "\n}"


def _url_host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _cite_dict(m, sentence: str):
    """Turn one INLINE_CITE_RE match into a claim citation dict, or None if the
    match is not a valid citation (e.g. an academic-head `n.d.`, which is
    web-only — mirrors extract_inline_cites in verify_citations.py).

    A `domain`-matched head is a WEB citation and carries `kind="web"` plus a
    `url` chosen by HOST match (exact host or a subdomain of the cited domain),
    NOT a naive substring — `foo.com` must not match `notfoo.com`. If no URL in
    the sentence has a matching host, the bare domain is used as the https://
    fallback (we do NOT borrow an unrelated URL). An `author`-matched head is an
    ACADEMIC citation; an academic head with `n.d.` is rejected (web-only)."""
    domain = m.group("domain")
    author = m.group("author")
    year = m.group("year")
    if domain:
        domain = domain.lower()
        url = None
        for raw in URL_RE.findall(sentence):
            u = raw.rstrip(".,;)")
            host = _url_host(u)
            if host == domain or host.endswith("." + domain):
                url = u
                break
        if url is None:
            url = f"https://{domain}"
        return {"author": domain, "year": year, "kind": "web", "url": url}
    if year == "n.d.":
        # `n.d.` is web-only; an author-head n.d. is not a valid citation.
        return None
    return {"author": (author or "").strip(), "year": year, "kind": "academic"}


def extract_claims(sections_dir: Path, *, run_root: Path | None = None):
    for f in sorted(sections_dir.rglob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        sentences = SENTENCE_SPLIT_RE.split(text)
        for sent in sentences:
            cites = [c for c in (_cite_dict(m, sent) for m in INLINE_CITE_RE.finditer(sent))
                     if c is not None]
            cites.extend(_kb_cite_dict(m) for m in KB_CITE_RE.finditer(sent))
            if not cites:
                continue
            yield {
                "file": (f.relative_to(run_root).as_posix() if run_root else str(f)),
                "sentence": sent.strip()[:600],
                "citations": cites,
            }


def managed_export(*, layout: RunLayout, fs: RootedFS, typed_args: dict):
    """Publish the canonical v2 machine exports and HTML companion."""
    require_managed_mutation(layout, "export")
    unknown = set(typed_args) - {"bible", "no_html"}
    if unknown:
        raise ValueError(f"unknown export options: {sorted(unknown)}")
    if layout.kind is not LayoutKind.V2:
        raise ValueError("managed export requires a v2 run")
    bibliography = layout.bibliography_md
    if not bibliography.is_file():
        raise FileNotFoundError(f"missing master bibliography: {bibliography}")
    entries = parse_bib_entries(bibliography.read_text(encoding="utf-8", errors="replace"))
    counter: dict = {}
    bibtex = "\n\n".join(to_bibtex_entry(entry, counter) for entry in entries)
    fs.atomic_write_text("Sources/bibliography.bib", bibtex, create_parents=True)
    claim_lines = [json.dumps(row) for row in extract_claims(layout.sections, run_root=layout.run_root)]
    fs.atomic_write_text(
        "Sources/claims.jsonl",
        "\n".join(claim_lines) + ("\n" if claim_lines else ""),
        create_parents=True,
    )
    result = {
        "bibliography": "Sources/bibliography.bib",
        "claims": "Sources/claims.jsonl",
        "claim_count": len(claim_lines),
    }
    if not typed_args.get("no_html", False):
        bible_value = typed_args.get("bible")
        bible = None
        if bible_value:
            candidate = Path(bible_value)
            if candidate.is_absolute():
                try:
                    candidate = candidate.relative_to(layout.run_root)
                except ValueError as exc:
                    raise ValueError("managed Bible path must remain inside the run") from exc
            bible = layout.run_root / safe_relpath(candidate)
        if bible is None:
            discovered = layout.discover_bible()
            bible = layout.run_root / discovered.markdown if discovered.markdown else None
        document = build_document(layout.sections, bibliography, bible)
        html_name = bible.with_suffix(".html").name if bible else assembled_html_name(layout.sections)
        with tempfile.TemporaryDirectory(prefix="research-html-") as temporary:
            rendered = export_html(document, Path(temporary) / html_name)
            fs.atomic_write_bytes(html_name, rendered.path.read_bytes(), create_parents=True)
        result.update(html=html_name, renderer=rendered.renderer)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", help="Managed v2 run (uses canonical layout paths)")
    ap.add_argument("--broker-endpoint", help="Manager broker endpoint for a v2 run")
    ap.add_argument("--lease-token", help="Manager lease token for a v2 run")
    ap.add_argument("--sections", help="Directory of section markdown files")
    ap.add_argument("--bibliography", help="Master bibliography file")
    ap.add_argument("--output-dir")
    ap.add_argument("--bible", help="Finished Markdown Bible to use for page metadata and basename")
    ap.add_argument("--no-html", action="store_true", help="Skip the automatic HTML companion")
    args = ap.parse_args(argv)

    if args.run_dir:
        if args.sections or args.bibliography or args.output_dir:
            ap.error("--run-dir conflicts with --sections/--bibliography/--output-dir")
        if not (args.broker_endpoint and args.lease_token):
            ap.error("--run-dir requires --broker-endpoint and --lease-token")
        layout = RunLayout.open(args.run_dir)
        result = broker_request(
            args.broker_endpoint,
            args.lease_token,
            "export",
            options={"bible": args.bible, "no_html": args.no_html},
        )
        print(f"BibTeX: {layout.run_root / result['bibliography']} ({len(parse_bib_entries(layout.bibliography_md.read_text()))} entries)")
        print(f"Claims: {layout.run_root / result['claims']} ({result['claim_count']} rows)")
        if result.get("html"):
            print(f"HTML: {layout.run_root / result['html']} ({result['renderer']})")
        return 0
    if not (args.sections and args.bibliography and args.output_dir):
        ap.error("standalone export requires --sections, --bibliography, and --output-dir")

    out_dir = Path(args.output_dir)
    guard = standalone_mutation_guard(out_dir, operation="export research Bible")
    guard.__enter__()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        bib_entries = parse_bib_entries(Path(args.bibliography).read_text(encoding="utf-8", errors="replace"))
        counter = {}
        bibtex_lines = [to_bibtex_entry(e, counter) for e in bib_entries]
        bib_path = out_dir / "bibliography.bib"
        bib_path.write_text("\n\n".join(bibtex_lines), encoding="utf-8")
        print(f"BibTeX: {bib_path} ({len(bib_entries)} entries)")

        claims_path = out_dir / "claims.jsonl"
        n = 0
        with claims_path.open("w", encoding="utf-8") as f:
            for row in extract_claims(Path(args.sections)):
                f.write(json.dumps(row) + "\n")
                n += 1
        print(f"Claims: {claims_path} ({n} rows)")

        if not args.no_html:
            try:
                bible_path = resolve_bible_path(out_dir, Path(args.bible) if args.bible else None)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            document = build_document(Path(args.sections), Path(args.bibliography), bible_path)
            html_name = bible_path.with_suffix(".html").name if bible_path else assembled_html_name(Path(args.sections))
            result = export_html(document, out_dir / html_name)
            if result.renderer == "jimemo":
                print(f"HTML: {result.path} (jimemo)")
            elif result.fallback_reason == "jimemo unavailable":
                print(f"HTML: {result.path} (built-in; jimemo unavailable)")
            else:
                print(f"HTML: {result.path} (built-in; {result.fallback_reason})")
    finally:
        guard.__exit__(*sys.exc_info())
    return 0


if __name__ == "__main__":
    main()
