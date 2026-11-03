#!/usr/bin/env python3
"""fetch_fulltext.py — fill in full text for Round-1 sources Exa left thin.

Round 1 (``slice_search.py``) asks Exa to extract each result's page/PDF text,
but some sources come back empty or truncated — most notably the academic anchor
works (metadata only, no Exa text) and PDF-backed white papers / reports Exa did
not fully render. This pass reads the real document directly:

  * academic / DOI rows  → resolve an OPEN-ACCESS pdf via OpenAlex, then fetch it
  * plain web rows       → fetch the URL as-is
  * PDF bytes            → extracted with pypdf
  * HTML bytes           → tags stripped to readable text

Extracted text is saved under ``round1/sources/<file>.txt`` (same convention as
slice_search) and each source's jsonl row is updated in place with ``text_path``
+ ``text_chars`` so Round-2 synthesis reads the full document, not a snippet.

SAFETY: every network fetch goes through the SSRF-hardened resolve-and-vet + IP
-pinned TLS machinery in ``verify_citations`` — reused, not reinvented. Redirects
are followed manually and RE-VETTED at every hop. Per-source fail-open: one bad
fetch is skipped with a notice, never aborts the run. ``$0`` — never ledgered.
No WebFetch (its summarizer hallucinates) — we read the raw bytes ourselves.

Exit 0 always (fail-open by design). Usage:

    python3 scripts/fetch_fulltext.py --run-dir research/[slug]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scripts import verify_citations as vc
from scripts.helper_runtime import require_managed_mutation, resolve_helper_layout
from scripts.run_layout import LayoutKind
from scripts.run_path_schema import PATH_SCHEMAS
from scripts.slice_search import _source_filename

# A row whose stored text is shorter than this is treated as "thin" and gets a
# direct-fetch attempt. Anchor rows (text_chars 0) always qualify.
DEFAULT_MIN_CHARS = int(os.environ.get("DR_FULLTEXT_MIN_CHARS", "400"))
# Hard cap on bytes pulled off the wire per source (defends against huge files).
DEFAULT_MAX_BYTES = int(os.environ.get("DR_FULLTEXT_MAX_BYTES", str(12_000_000)))
# Cap on characters kept from any one document (bounds synthesis context).
DEFAULT_MAX_CHARS = int(os.environ.get("DR_FULLTEXT_MAX_CHARS", "20000"))
MAX_REDIRECTS = 5
OPENALEX = "https://api.openalex.org"
CONTACT = os.environ.get("CONTACT_EMAIL", "anonymous@example.com")
OA_KEY = os.environ.get("OPENALEX_KEY")  # OpenAlex premium key; metered credit pool
_OA_KEY_SUFFIX = f"&api_key={OA_KEY}" if OA_KEY else ""


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

class _TextHTMLParser(HTMLParser):
    """Collect visible text, dropping <script>/<style>/<head> content."""

    _SKIP = {"script", "style", "head", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self._chunks)).strip()


def _html_to_text(data: bytes) -> str:
    try:
        raw = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    parser = _TextHTMLParser()
    try:
        parser.feed(raw)
    except Exception:  # noqa: BLE001 — malformed markup, keep what we got
        pass
    return parser.text()


_SURROGATE_RE = re.compile("[\\ud800-\\udfff]")


def _strip_surrogates(text: str) -> str:
    """Replace lone UTF-16 surrogates (half an emoji from a broken PDF font
    map) with U+FFFD. ``write_text(encoding="utf-8")`` raises
    UnicodeEncodeError on them, and one such document would otherwise sink the
    whole full-text pass instead of failing open per row."""
    if not text or not _SURROGATE_RE.search(text):
        return text
    return _SURROGATE_RE.sub("\ufffd", text)


def _write_atomic(path: Path, data) -> None:
    """Write ``data`` (str as UTF-8, or bytes) to a temporary sibling, then
    os.replace it into place. A failed write never truncates the file already
    there (often the Exa snippet slice_search saved under the same name)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        if isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _pdf_to_text(data: bytes) -> str:
    try:
        import pypdf
    except ImportError:
        print("  ⚠ pypdf not installed — PDF text extraction skipped "
              "(pip install pypdf)", file=sys.stderr)
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ PDF parse failed ({type(exc).__name__})", file=sys.stderr)
        return ""
    out = []
    try:
        for page in reader.pages:
            try:
                out.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 — one bad page never sinks the doc
                continue
    except Exception as exc:  # noqa: BLE001 — the pages tree itself can be malformed
        print(f"  ⚠ PDF pages unreadable ({type(exc).__name__})", file=sys.stderr)
    return _strip_surrogates(re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip())


def _looks_like_pdf(url: str, content_type: str, data: bytes) -> bool:
    if data[:5] == b"%PDF-":
        return True
    if "application/pdf" in (content_type or "").lower():
        return True
    return urlparse(url).path.lower().endswith(".pdf")


def _extract(url: str, content_type: str, data: bytes) -> tuple[str, str]:
    """Return (text, method) — method is 'pdf' or 'html'."""
    if _looks_like_pdf(url, content_type, data):
        return _pdf_to_text(data), "pdf"
    return _html_to_text(data), "html"


# --------------------------------------------------------------------------- #
# Safe fetch (SSRF-vetted, redirects re-vetted per hop)
# --------------------------------------------------------------------------- #

def _safe_get(url: str, max_bytes: int, timeout: int):
    """Fetch ``url`` through the SSRF-hardened, IP-pinned path in verify_citations,
    following up to MAX_REDIRECTS redirects and RE-VETTING every hop.

    Returns (data_bytes, content_type, final_url) on success, else (None, reason,
    None). Never raises."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed, err = vc._parse_probe_target(current)
        if err is not None:
            return None, f"policy:{err.reason}", None
        host, _port, pinned_ip, pinned_url = parsed
        sess = vc._probe_session(host, pinned_ip)
        try:
            resp = sess.get(
                pinned_url,
                headers={"Host": host, "Accept": "*/*"},
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"fetch-error:{type(exc).__name__}", None

        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            resp.close()
            if not loc:
                return None, "redirect-no-location", None
            # Resolve relative redirects against the current URL, then re-vet.
            current = _resolve_redirect(current, loc)
            continue

        if resp.status_code != 200:
            code = resp.status_code
            resp.close()
            return None, f"http-{code}", None

        content_type = resp.headers.get("Content-Type", "")
        chunks = bytearray()
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                chunks.extend(chunk)
                if len(chunks) >= max_bytes:
                    break
        except Exception as exc:  # noqa: BLE001
            return None, f"read-error:{type(exc).__name__}", None
        finally:
            resp.close()
        return bytes(chunks), content_type, current

    return None, "too-many-redirects", None


def _resolve_redirect(base: str, location: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, location)


# --------------------------------------------------------------------------- #
# Open-access resolution for DOI / academic rows
# --------------------------------------------------------------------------- #

def _doi_from_url(url: str) -> str | None:
    m = re.search(r"10\.\d{4,9}/[^\s?#]+", url or "")
    return m.group(0).rstrip("/.") if m else None


def _openalex_id_from_row(row: dict, url: str) -> str | None:
    """Return a bare OpenAlex W-id for a DOI-less row, from row["openalex_id"]
    or parsed out of an https://openalex.org/W... url. None if neither is present."""
    raw = (row.get("openalex_id") or "").strip()
    if not raw:
        m = re.search(r"openalex\.org/(W\d+)", url or "", re.IGNORECASE)
        if m:
            raw = m.group(1)
    if not raw:
        return None
    m = re.search(r"(W\d+)", raw, re.IGNORECASE)
    return m.group(1) if m else None


def _best_oa_pdf_from_work(work: dict) -> str | None:
    """Read the best open-access PDF (or landing/oa) url from an OpenAlex work
    record, in priority order. Returns a url or None."""
    best = work.get("best_oa_location") or {}
    for key in ("pdf_url", "landing_page_url"):
        val = best.get(key)
        if val:
            return val
    oa = work.get("open_access") or {}
    if oa.get("oa_url"):
        return oa.get("oa_url")
    primary = work.get("primary_location") or {}
    return primary.get("pdf_url")


def _openalex_oa_pdf(doi: str, timeout: int) -> str | None:
    """Ask OpenAlex for an open-access PDF url for ``doi`` (free API, no key).
    The lookup itself goes through the same safe fetch. Returns a url or None."""
    api = f"{OPENALEX}/works/doi:{doi}?mailto={CONTACT}{_OA_KEY_SUFFIX}"
    data, ctype_or_reason, _ = _safe_get(api, 2_000_000, timeout)
    if data is None:
        return None
    try:
        work = json.loads(data.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
    return _best_oa_pdf_from_work(work)


def _openalex_oa_pdf_by_id(work_id: str, timeout: int) -> str | None:
    """Resolve an open-access PDF url from a bare OpenAlex W-id (DOI-less rows).
    GETs works/<Wid> and reads best_oa_location.pdf_url (or oa_url / primary
    pdf_url). Same free API, same safe fetch. Returns a url or None."""
    api = f"{OPENALEX}/works/{work_id}?mailto={CONTACT}{_OA_KEY_SUFFIX}"
    data, ctype_or_reason, _ = _safe_get(api, 2_000_000, timeout)
    if data is None:
        return None
    try:
        work = json.loads(data.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
    return _best_oa_pdf_from_work(work)


# --------------------------------------------------------------------------- #
# Per-row processing
# --------------------------------------------------------------------------- #

def _fetch_row_text(url: str, is_academic: bool, max_bytes: int,
                    max_chars: int, timeout: int,
                    row: dict | None = None
                    ) -> tuple[str, str, bytes | None, str | None]:
    """Return (text, method, raw_bytes, ext) for one source url.

    On failure: ('', reason, None, None). ``raw_bytes`` is the full document as
    downloaded (capped at max_bytes) so the caller can persist the original file;
    ``ext`` is 'pdf' or 'html'."""
    row = row or {}
    target = url
    method_prefix = ""
    if is_academic:
        doi = _doi_from_url(url)
        if doi:
            oa = _openalex_oa_pdf(doi, timeout)
            if oa:
                target = oa
                method_prefix = "oa:"
        else:
            # DOI-less citation candidate: resolve its OA location from an
            # OpenAlex W-id (row["openalex_id"] or an openalex.org/W... url).
            # A raw GET of a bare openalex.org URL returns HTTP 403, so we must
            # hydrate the work and fetch its actual OA pdf instead.
            wid = _openalex_id_from_row(row, url)
            oa = _openalex_oa_pdf_by_id(wid, timeout) if wid else None
            if oa:
                target = oa
                method_prefix = "oa:"
            else:
                # G6: no resolvable OA url for this DOI-less row (no W-id, or the
                # work has no OA pdf/landing/oa_url). SKIP the fetch rather than
                # raw-GETting the openalex.org page (which 403s / returns empty).
                # Leave the row with no text; only a real OA url is worth fetching.
                return "", "no-oa-url", None, None
        # Fetching a bare doi.org link usually lands on a paywalled HTML page;
        # only worth it when OpenAlex found nothing and there IS a DOI target.

    data, ctype_or_reason, final_url = _safe_get(target, max_bytes, timeout)
    if data is None:
        return "", ctype_or_reason, None, None  # reason string
    text, method = _extract(final_url or target, ctype_or_reason, data)
    if len(text) > max_chars:
        text = text[:max_chars]
    ext = "pdf" if method == "pdf" else "html"
    return text, f"{method_prefix}{method}", data, ext


def process_run(run_dir: Path, min_chars: int, max_bytes: int,
                max_chars: int, timeout: int) -> dict:
    layout = resolve_helper_layout(run_dir)
    round1 = layout.round1
    sources_dir = layout.extracted_sources
    slice_files = sorted(round1.glob("slice_*.jsonl"))
    summary = {"attempted": 0, "fetched": 0, "skipped": 0,
               "by_method": {}, "failures": {}}

    for jsonl_path in slice_files:
        rows = _read_rows(jsonl_path)
        if not rows:
            continue
        changed = False
        for row in rows:
            url = (row.get("url") or "").strip()
            if not url:
                continue
            # Attempt a direct full-text download for EVERY source (not just the
            # ones Exa left thin). We keep whichever text is longer — Exa's
            # snippet or the document we fetch ourselves — and always persist the
            # raw file when the fetch lands one. A source is only left at its Exa
            # snippet when the direct fetch fails (paywall/404/no-OA), i.e. when
            # we genuinely cannot access the full document.
            summary["attempted"] += 1
            existing_chars = row.get("text_chars") or 0
            # Citation-chase rows resolve via OpenAlex id / DOI, so route
            # them through the academic OA-PDF path rather than a raw GET
            # (a bare openalex.org URL returns HTTP 403 on a raw fetch).
            is_academic = (
                row.get("slice") in ("anchor", "citation")
                or row.get("academic") is True
                or "doi.org" in url
                or bool(row.get("openalex_id"))
                or "openalex.org/W" in url
            )
            text, method, raw, ext = _fetch_row_text(
                url, is_academic, max_bytes, max_chars, timeout, row=row)
            # Belt and braces for the write below: whatever the extractor
            # returned, the row must stay fail-open (see _strip_surrogates).
            text = _strip_surrogates(text)

            # Persist the original downloaded document whenever we got one,
            # regardless of whether its extracted text beats the Exa snippet.
            if raw is not None:
                base = _source_filename(row)[:-4]  # strip trailing '.txt'
                raw_name = f"{base}.{ext or 'bin'}"
                try:
                    _write_atomic(sources_dir / raw_name, raw)
                except OSError as exc:
                    # One unwritable file must not sink the pass (issue #8).
                    print(f"  ⚠ raw write failed ({type(exc).__name__})  {url}",
                          file=sys.stderr)
                else:
                    row["raw_path"] = (
                        f"Sources/Extracted/{raw_name}"
                        if layout.kind is LayoutKind.V2
                        else f"sources/{raw_name}"
                    )
                    changed = True

            # Keep the longer text: our direct fetch vs. the existing Exa snippet.
            write_error = None
            if text and len(text) > existing_chars:
                fname = _source_filename(row)
                try:
                    _write_atomic(sources_dir / fname, text)
                except (OSError, UnicodeError) as exc:
                    # Fail open per row: any write error (not just a surrogate
                    # the sanitizer missed) skips this row, never the pass.
                    write_error = f"write-error:{type(exc).__name__}"
            if text and len(text) > existing_chars and not write_error:
                row["text_path"] = (
                    f"Sources/Extracted/{fname}"
                    if layout.kind is LayoutKind.V2
                    else f"sources/{fname}"
                )
                row["text_chars"] = len(text)
                row["fulltext_method"] = method
                summary["fetched"] += 1
                summary["by_method"][method] = \
                    summary["by_method"].get(method, 0) + 1
                changed = True
                print(f"  ✓ {method:8} {len(text):>7,} chars  {url}",
                      file=sys.stderr)
            else:
                summary["skipped"] += 1
                # Distinguish "could not fetch" from "fetched, but Exa's snippet
                # was already as long or longer" (kept, not replaced).
                reason = write_error or (method if not text else "kept-existing")
                summary["failures"][reason] = \
                    summary["failures"].get(reason, 0) + 1
                print(f"  · skip ({reason})  {url}", file=sys.stderr)
        if changed:
            document = jsonl_path.relative_to(layout.run_root).as_posix()
            for row in rows:
                PATH_SCHEMAS.validate_document(layout, document, row)
            _write_rows(jsonl_path, rows)

    (round1 / "fulltext_manifest.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def _read_rows(path: Path) -> list[dict]:
    rows = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            return []  # malformed slice — do not touch it
    return rows


def _write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Fetch full text for Round-1 sources Exa left thin.")
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS,
                    help="RETAINED for backward-compat; no longer gates fetching. "
                         "Every row is now attempted and the longer of "
                         "(Exa snippet, direct fetch) is kept — see process_run.")
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args(argv)

    layout = resolve_helper_layout(args.run_dir)
    require_managed_mutation(layout, "full-text retrieval")
    round1 = layout.round1
    if not round1.is_dir():
        print(f"  ⚠ no round1/ under {args.run_dir} — nothing to do",
              file=sys.stderr)
        return 0

    summary = process_run(args.run_dir, args.min_chars, args.max_bytes,
                          args.max_chars, args.timeout)
    print(f"Full-text pass: {summary['fetched']} fetched / "
          f"{summary['attempted']} attempted "
          f"({summary['skipped']} skipped). Methods: {summary['by_method']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
