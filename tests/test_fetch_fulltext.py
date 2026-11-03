"""fetch_fulltext — OFFLINE tests. The network (_safe_get) is monkeypatched;
no real HTTP, no DNS. Exercises extraction + the process_run wiring that fills
thin Round-1 rows and rewrites their jsonl in place.
"""

import io
import json

import pytest

from scripts import fetch_fulltext as ff


# --- extraction helpers -----------------------------------------------------

def test_html_to_text_drops_script_and_style():
    html = (b"<html><head><style>.x{color:red}</style></head>"
            b"<body><script>evil()</script><h1>Title</h1>"
            b"<p>Real body text here.</p></body></html>")
    text = ff._html_to_text(html)
    assert "Title" in text and "Real body text here." in text
    assert "evil()" not in text and "color:red" not in text


def test_looks_like_pdf_by_magic_content_type_and_suffix():
    assert ff._looks_like_pdf("https://x.com/a", "", b"%PDF-1.7 ...")
    assert ff._looks_like_pdf("https://x.com/a", "application/pdf", b"xx")
    assert ff._looks_like_pdf("https://x.com/paper.pdf", "text/html", b"xx")
    assert not ff._looks_like_pdf("https://x.com/page", "text/html", b"<html>")


def test_doi_from_url():
    assert ff._doi_from_url("https://doi.org/10.1234/abc.def") == "10.1234/abc.def"
    assert ff._doi_from_url("https://example.com/no-doi") is None


def test_pdf_to_text_roundtrip():
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    # A blank page extracts to empty string but must not raise.
    assert ff._pdf_to_text(buf.getvalue()) == ""
    assert ff._pdf_to_text(b"not a pdf") == ""


def test_pdf_to_text_survives_malformed_pages_tree(monkeypatch):
    """A PDF whose /Pages tree is broken raises from `reader.pages` itself, outside
    the per-page try — one such PDF used to kill the whole full-text pass."""
    pypdf = pytest.importorskip("pypdf")

    class _BrokenReader:
        def __init__(self, _stream):
            pass

        @property
        def pages(self):
            raise pypdf.errors.PdfReadError("broken pages tree")

    monkeypatch.setattr(pypdf, "PdfReader", _BrokenReader)
    assert ff._pdf_to_text(b"%PDF-1.4 whatever") == ""


# --- process_run wiring -----------------------------------------------------

def _write_slice(run_dir, name, rows):
    d = run_dir / "round1"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"slice_{name}.jsonl"
    with p.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


def test_every_row_attempted_longer_text_wins_raw_always_saved(monkeypatch, tmp_path):
    # Every source is now attempted (not just thin ones). The thin row's text is
    # replaced by the longer direct fetch; the fat row already has a longer Exa
    # snippet so its TEXT is kept — but its raw document is still downloaded.
    thin = {"url": "https://a.com/thin", "slice": "web", "text_chars": 0, "tier": "web"}
    fat = {"url": "https://a.com/fat", "slice": "web", "text_chars": 9000,
           "text_path": "sources/web_deadbeef.txt", "tier": "web"}
    _write_slice(tmp_path, "web", [thin, fat])

    body = "Rich full document body. " * 50  # ~1249 chars — beats thin(0), not fat(9000)
    monkeypatch.setattr(ff, "_safe_get",
                        lambda url, mb, to: (body.encode(), "text/html", url))

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    # Both rows attempted; only the thin row's text replaced (fat's snippet longer).
    assert summary["attempted"] == 2 and summary["fetched"] == 1

    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_web.jsonl").read_text().splitlines() if l.strip()]
    filled = next(r for r in rows if r["url"].endswith("/thin"))
    unchanged = next(r for r in rows if r["url"].endswith("/fat"))
    assert filled["text_chars"] >= 1200 and filled["fulltext_method"] == "html"
    assert (tmp_path / "round1" / filled["text_path"]).read_text().startswith("Rich full")
    # Fat row's TEXT is kept (Exa snippet longer), but its raw file is saved.
    assert unchanged["text_chars"] == 9000
    assert unchanged["text_path"] == "sources/web_deadbeef.txt"  # text not replaced
    # Both rows downloaded their original document to sources/*.html.
    for r in (filled, unchanged):
        assert r["raw_path"].startswith("sources/") and r["raw_path"].endswith(".html")
        assert (tmp_path / "round1" / r["raw_path"]).read_bytes() == body.encode()

    manifest = json.loads((tmp_path / "round1" / "fulltext_manifest.json").read_text())
    assert manifest["by_method"] == {"html": 1}


def test_academic_row_resolves_openalex_oa_pdf(monkeypatch, tmp_path):
    row = {"url": "https://doi.org/10.1234/xyz", "slice": "anchor", "text_chars": 0, "tier": "peer"}
    _write_slice(tmp_path, "anchor", [row])

    pdf_bytes = b"%PDF-1.7 " + (b"paper text " * 100)
    oa_json = json.dumps({"best_oa_location": {"pdf_url": "https://repo.org/x.pdf"}}).encode()

    calls = []

    def fake_get(url, mb, to):
        calls.append(url)
        if "api.openalex.org" in url:
            return oa_json, "application/json", url
        return pdf_bytes, "application/pdf", url

    monkeypatch.setattr(ff, "_safe_get", fake_get)
    # PDF extraction of our fake bytes yields nothing real, so stub the extractor
    # to prove the OA-resolution + method-tagging path independently of pypdf.
    monkeypatch.setattr(ff, "_pdf_to_text", lambda data: "Extracted paper text " * 40)

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["fetched"] == 1
    assert any("api.openalex.org" in u for u in calls)
    assert any("repo.org/x.pdf" in u for u in calls)

    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_anchor.jsonl").read_text().splitlines() if l.strip()]
    assert rows[0]["fulltext_method"] == "oa:pdf"


def test_citation_slice_row_treated_as_academic(monkeypatch, tmp_path):
    # A slice=='citation' row must take the is_academic path (OpenAlex OA-PDF
    # resolve), NOT a raw GET of the doi.org link. Prove it by asserting the
    # OA-resolver runs for this row's DOI.
    row = {"url": "https://doi.org/10.1234/cited", "slice": "citation",
           "text_chars": 0, "tier": "peer_reviewed"}
    _write_slice(tmp_path, "citation", [row])

    resolved = []

    def fake_oa(doi, timeout):
        resolved.append(doi)
        return "https://repo.org/cited.pdf"
    monkeypatch.setattr(ff, "_openalex_oa_pdf", fake_oa)

    pdf_bytes = b"%PDF-1.7 " + (b"cited text " * 100)

    def fake_get(url, mb, to):
        return pdf_bytes, "application/pdf", url
    monkeypatch.setattr(ff, "_safe_get", fake_get)
    monkeypatch.setattr(ff, "_pdf_to_text", lambda data: "Extracted cited body " * 40)

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    # The OA resolver was invoked for the citation row's DOI (academic path),
    # confirming slice=='citation' is treated as academic.
    assert resolved == ["10.1234/cited"]
    assert summary["fetched"] == 1
    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_citation.jsonl").read_text().splitlines() if l.strip()]
    assert rows[0]["fulltext_method"] == "oa:pdf"


def test_doiless_openalex_id_row_resolves_oa_pdf(monkeypatch, tmp_path):
    # FIX F8: a citation candidate with NO DOI but an openalex_id must resolve
    # its OA pdf via works/<Wid>, not fall through to a 403-ing raw GET.
    row = {"url": "https://openalex.org/W42", "slice": "citation",
           "openalex_id": "https://openalex.org/W42", "text_chars": 0, "tier": "peer"}
    _write_slice(tmp_path, "citation", [row])

    oa_json = json.dumps(
        {"best_oa_location": {"pdf_url": "https://repo.org/w42.pdf"}}).encode()
    calls = []

    def fake_get(url, mb, to):
        calls.append(url)
        if "api.openalex.org" in url:
            return oa_json, "application/json", url
        return b"%PDF-1.7 body", "application/pdf", url

    monkeypatch.setattr(ff, "_safe_get", fake_get)
    monkeypatch.setattr(ff, "_pdf_to_text", lambda data: "Resolved OA body " * 40)

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["fetched"] == 1
    # Hydrated the work by bare W-id, then fetched THAT oa pdf.
    assert any("api.openalex.org/works/W42" in u for u in calls)
    assert any("repo.org/w42.pdf" in u for u in calls)
    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_citation.jsonl").read_text().splitlines() if l.strip()]
    assert rows[0]["fulltext_method"] == "oa:pdf"


def test_doiless_row_with_no_oa_url_does_not_raw_get_openalex(monkeypatch, tmp_path):
    # G6: a DOI-less citation row whose OpenAlex work has NO OA pdf/landing/oa_url
    # must be SKIPPED — never raw-GET the openalex.org page (which 403s / returns
    # empty). The only permitted _safe_get is the works/<Wid> hydration; the
    # openalex.org/W... url must never be fetched.
    row = {"url": "https://openalex.org/W77", "slice": "citation",
           "openalex_id": "https://openalex.org/W77", "text_chars": 0, "tier": "peer"}
    _write_slice(tmp_path, "citation", [row])

    # The hydrated work has NO OA location anywhere.
    oa_json = json.dumps({"best_oa_location": {}, "open_access": {}}).encode()
    calls = []

    def fake_get(url, mb, to):
        calls.append(url)
        if "api.openalex.org" in url:
            return oa_json, "application/json", url
        # Any non-api GET here would be the forbidden raw openalex.org fetch.
        return b"forbidden", "text/html", url

    monkeypatch.setattr(ff, "_safe_get", fake_get)

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    # Row was attempted, resolved to no OA url, and skipped — nothing fetched.
    assert summary["fetched"] == 0
    assert summary["skipped"] == 1
    # The work was hydrated once; the openalex.org PAGE was never raw-GET.
    assert any("api.openalex.org/works/W77" in u for u in calls)
    assert not any(u == "https://openalex.org/W77" for u in calls)
    assert not any("openalex.org/W77" in u and "api." not in u for u in calls)
    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_citation.jsonl").read_text().splitlines() if l.strip()]
    # Row left with no text.
    assert rows[0].get("text_chars", 0) == 0
    assert "text_path" not in rows[0]


def test_openalex_id_parsed_from_url_when_field_absent():
    # A DOI-less row with only an openalex.org/W url (no openalex_id field)
    # still yields a bare W-id.
    assert ff._openalex_id_from_row(
        {}, "https://openalex.org/W99") == "W99"
    assert ff._openalex_id_from_row(
        {"openalex_id": "W7"}, "https://x.com/other") == "W7"
    assert ff._openalex_id_from_row({}, "https://x.com/no-id") is None


def test_web_slice_row_not_treated_as_academic(monkeypatch, tmp_path):
    # Contrast: a plain web row with a non-DOI url must NOT hit the OA resolver.
    _write_slice(tmp_path, "web",
                 [{"url": "https://news.example/story", "slice": "web",
                   "text_chars": 0, "tier": "news"}])

    def boom(doi, timeout):
        raise AssertionError("web rows must not resolve via OpenAlex")
    monkeypatch.setattr(ff, "_openalex_oa_pdf", boom)
    body = "Plain web article body. " * 50
    monkeypatch.setattr(ff, "_safe_get",
                        lambda url, mb, to: (body.encode(), "text/html", url))

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["fetched"] == 1


def test_fetch_failure_is_fail_open(monkeypatch, tmp_path):
    _write_slice(tmp_path, "web",
                 [{"url": "https://a.com/x", "slice": "web", "text_chars": 0, "tier": "web"}])
    monkeypatch.setattr(ff, "_safe_get", lambda url, mb, to: (None, "http-403", None))
    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["fetched"] == 0 and summary["skipped"] == 1
    assert summary["failures"] == {"http-403": 1}


def test_no_round1_dir_returns_zero(tmp_path, capsys):
    assert ff.main(["--run-dir", str(tmp_path)]) == 0


def test_no_verify_false_in_source():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "fetch_fulltext.py").read_text()
    assert "verify=False" not in src, "TLS verification must never be disabled"


# --- lone surrogates (issue #8) ---------------------------------------------

def test_strip_surrogates_replaces_only_unencodable_text():
    clean = "plain text with an emoji \U0001F600 and accents é"
    assert ff._strip_surrogates(clean) is clean          # untouched, same object
    assert ff._strip_surrogates("") == ""
    assert ff._strip_surrogates("half \ud83d emoji") == "half � emoji"


def test_pdf_to_text_replaces_a_lone_surrogate_from_pypdf(monkeypatch):
    """The real extractor, with pypdf stubbed at the module boundary: a page
    whose extract_text() carries half an emoji must come back UTF-8 encodable.
    ingest_local calls _pdf_to_text directly, with no sanitizer after it."""
    import sys
    import types

    class _Page:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _Reader:
        def __init__(self, _stream):
            self.pages = [_Page("Page one \ud83d broken emoji"), _Page("Page two fine")]

    fake_pypdf = types.ModuleType("pypdf")
    fake_pypdf.PdfReader = _Reader
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    text = ff._pdf_to_text(b"%PDF-1.4 whatever")
    assert text == "Page one \ufffd broken emoji\nPage two fine"
    text.encode("utf-8")   # must not raise


def test_lone_surrogate_in_pdf_text_does_not_sink_the_pass(monkeypatch, tmp_path):
    """Issue #8: pypdf can hand back a lone UTF-16 surrogate; write_text then
    raised UnicodeEncodeError outside the per-row try and the whole full-text
    pass died with no manifest. The row must be written (U+FFFD in place of the
    surrogate) and the rows after it must still be attempted."""
    bad = {"url": "https://a.com/bad.pdf", "slice": "web", "text_chars": 0, "tier": "web"}
    good = {"url": "https://a.com/good", "slice": "web", "text_chars": 0, "tier": "web"}
    _write_slice(tmp_path, "web", [bad, good])

    html_body = "Readable body text follows. " * 50

    def fake_get(url, mb, to):
        if url.endswith(".pdf"):
            return b"%PDF-1.4 fake", "application/pdf", url
        return html_body.encode(), "text/html", url

    monkeypatch.setattr(ff, "_safe_get", fake_get)
    monkeypatch.setattr(ff, "_pdf_to_text",
                        lambda data: ("Extracted text \ud83d with half an emoji. " * 40))

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["attempted"] == 2 and summary["fetched"] == 2

    rows = [json.loads(l) for l in
            (tmp_path / "round1" / "slice_web.jsonl").read_text().splitlines() if l.strip()]
    bad_row = next(r for r in rows if r["url"].endswith("bad.pdf"))
    written = (tmp_path / "round1" / bad_row["text_path"]).read_text(encoding="utf-8")
    assert "�" in written and "\ud83d" not in written
    assert bad_row["text_chars"] == len(written)
    assert (tmp_path / "round1" / "fulltext_manifest.json").exists()


def test_write_errors_skip_the_row_not_the_pass(monkeypatch, tmp_path):
    """Any per-row write error (full disk, bad path), not only a surrogate,
    must skip that row and leave the rest of the pass and its manifest intact."""
    from pathlib import Path

    rows = [{"url": f"https://a.com/{n}", "slice": "web", "text_chars": 0, "tier": "web"}
            for n in ("bad", "rawbad.pdf", "good")]
    _write_slice(tmp_path, "web", rows)

    def fake_get(url, mb, to):
        if url.endswith(".pdf"):
            return b"%PDF-1.4 fake", "application/pdf", url
        tag = "POISON " if url.endswith("/bad") else ""
        return (f"{tag}Readable body text follows. " * 50).encode(), "text/html", url

    real_write_text, real_write_bytes = Path.write_text, Path.write_bytes

    def write_text(self, data, *a, **k):
        if "POISON" in data:
            raise OSError(28, "No space left on device")
        return real_write_text(self, data, *a, **k)

    def write_bytes(self, data):
        if ".pdf" in self.name:
            raise OSError(13, "Permission denied")
        return real_write_bytes(self, data)

    monkeypatch.setattr(ff, "_safe_get", fake_get)
    monkeypatch.setattr(ff, "_pdf_to_text", lambda data: "PDF body text. " * 60)
    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "write_bytes", write_bytes)

    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["attempted"] == 3 and summary["fetched"] == 2
    assert summary["failures"] == {"write-error:OSError": 1}

    out = {r["url"].rsplit("/", 1)[1]: r for r in
           (json.loads(l) for l in
            (tmp_path / "round1" / "slice_web.jsonl").read_text().splitlines() if l.strip())}
    assert "text_path" not in out["bad"]
    assert "raw_path" not in out["rawbad.pdf"] and "text_path" in out["rawbad.pdf"]
    assert "text_path" in out["good"]
    assert (tmp_path / "round1" / "fulltext_manifest.json").exists()


def test_unwritable_sources_dir_skips_rows_not_the_pass(monkeypatch, tmp_path):
    """The sources dir cannot be created (a file sits at its path): every row
    is skipped as a write error and the manifest is still written."""
    _write_slice(tmp_path, "web", [
        {"url": "https://a.com/one", "slice": "web", "text_chars": 0, "tier": "web"},
        {"url": "https://a.com/two.pdf", "slice": "web", "text_chars": 0, "tier": "web"}])
    (tmp_path / "round1" / "sources").write_text("not a directory")

    def fake_get(url, mb, to):
        if url.endswith(".pdf"):
            return b"%PDF-1.4 fake", "application/pdf", url
        return ("Readable body text follows. " * 50).encode(), "text/html", url

    monkeypatch.setattr(ff, "_safe_get", fake_get)
    monkeypatch.setattr(ff, "_pdf_to_text", lambda data: "PDF body text. " * 60)
    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["attempted"] == 2 and summary["fetched"] == 0
    assert sum(summary["failures"].values()) == 2
    assert all(k.startswith("write-error:") for k in summary["failures"])
    assert (tmp_path / "round1" / "fulltext_manifest.json").exists()


def test_failed_write_keeps_the_existing_snippet_file(monkeypatch, tmp_path):
    """A write that dies part-way must not truncate the Exa snippet that
    slice_search already saved under the same filename."""
    from pathlib import Path
    from scripts.slice_search import _source_filename

    row = {"url": "https://a.com/page", "slice": "web", "tier": "web",
           "text_chars": 12}
    fname = _source_filename(row)
    row["text_path"] = f"sources/{fname}"
    _write_slice(tmp_path, "web", [row])
    existing = tmp_path / "round1" / "sources" / fname
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("Exa snippet.", encoding="utf-8")

    monkeypatch.setattr(ff, "_safe_get", lambda url, mb, to: (
        ("Readable body text follows. " * 50).encode(), "text/html", url))
    real_write_text = Path.write_text

    def half_then_fail(self, data, *a, **k):
        if "Readable body" in data:
            real_write_text(self, data[: len(data) // 2], *a, **k)
            raise OSError(28, "No space left on device")
        return real_write_text(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", half_then_fail)
    summary = ff.process_run(tmp_path, min_chars=400, max_bytes=10_000_000,
                             max_chars=20_000, timeout=5)
    assert summary["failures"] == {"write-error:OSError": 1}
    assert existing.read_text(encoding="utf-8") == "Exa snippet."
    assert not list(existing.parent.glob("*.tmp"))
