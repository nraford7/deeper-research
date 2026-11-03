#!/usr/bin/env python3
"""deepen_questions.py — Round 2.5: question-driven deepening over three buckets.

Round 2 emits a synthesis markdown file with EXACT headers. This script ingests
that file, splits the questions into three buckets, allocates up to 9 across
them (Phase-A rules), and fires ONE Exa ``deep-reasoning`` call per allocated
question. Each answer lands under ``<run-dir>/round2_5/`` with a terminal
``## Sources`` block; a ``coverage.json`` records how many questions were asked
vs answered, per bucket.

BUCKETS (ingestion contract):
  * b1 (root_cause)  ← bullets under ``## Root Cause Questions``
  * b2 (consequence) ← bullets under ``## Consequence Questions``
  * b3 (gap)         ← bullets under ``## New Questions``, THEN ``## Openings``

``extract_buckets`` is TOLERANT: a missing header yields an empty bucket, a
malformed input yields empties plus a stderr warning, and it NEVER raises — a
paid run must not die in ingestion code.

ALLOCATION (Phase-A rules 1–4, mirrored exactly):
  1. 3 per bucket, cap 9 total.
  2. Cross-bucket casefold+strip dedupe with priority b1 → b2 → b3 (a question
     appearing in b1 and b3 stays in b1, removed from b3).
  3. Take the first 3 of each bucket in b1,b2,b3 order (the "labelled" head).
  4. Leftover-order backfill: append each bucket's items past index 3, in
     b1,b2,b3 order, until the global cap of 9 is reached.

MONEY: every Exa call is pre-charged against ``RetrievalLedger`` at
``RETRIEVAL_FEES["deep_reasoning"] * RETRY_MULTIPLIER`` (= $0.04) BEFORE the
request, then reconciled from ``costDollars.total`` after. A charge that would
breach the cap raises ``LedgerCapExceeded`` → we WRITE ``coverage.json``
reflecting the shortfall FIRST, THEN exit 21 (a refusal is an exit, never a
silent skip).

RETRIES: the HTTP session pins ``Retry(total=1)`` EXPLICITLY. The ×2 ledger
worst-case bound depends on this — do NOT copy lit_search's ``total=4``.

FAIL-OPEN: any HTTP/parse error for ONE question skips it, counts it (reconcile
to $0), and the run continues (exit 0). Thin coverage is visible in
``coverage.json`` (answered < questions).

Usage:
  python3 scripts/deepen_questions.py --run-dir DIR --round2-file PATH
      [--max-retrieval-usd X]
  python3 scripts/deepen_questions.py --run-dir DIR \
      --single-question "..." --bucket gap|root_cause|consequence   # Round-5
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from scripts.helper_runtime import require_managed_mutation, resolve_helper_layout

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - dependency preflight
    sys.stderr.write("Missing dep: pip install requests\n")
    sys.exit(1)


class CappedRetry(Retry):
    """Retry that honors server Retry-After headers but caps the sleep.

    urllib3 sleeps for the full Retry-After value with NO ceiling; a
    rate-limiting server (Crossref/OpenAlex 429s especially) can park the
    process for an hour inside the retry machinery, outside every request
    timeout. Cap it so a stall becomes a bounded pause. (Root cause of a
    42-minute silent hang, 2026-08-25.)"""

    RETRY_AFTER_CAP = 30.0  # seconds

    def get_retry_after(self, response):
        retry_after = super().get_retry_after(response)
        if retry_after is None:
            return retry_after
        return min(retry_after, self.RETRY_AFTER_CAP)


# Allow running both as `python3 scripts/deepen_questions.py` and `-m scripts.deepen_questions`.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
from scripts.cost import RETRIEVAL_FEES, RETRY_MULTIPLIER
from scripts.ledger import RetrievalLedger, LedgerCapExceeded

# Base URL override for Exa-compatible endpoints (proxies, gateways). When
# EXA_BASE_URL is set, the x-api-key header is optional (auth may be
# out-of-band).
EXA_BASE_URL = os.environ.get("EXA_BASE_URL", "https://api.exa.ai").rstrip("/")
EXA_SEARCH_URL = EXA_BASE_URL + "/search"
EXA_PREFLIGHT_EXIT = 20

# 3 per bucket, cap 9 total (Phase-A rule 1).
_PER_BUCKET = 3
_CAP = 9

# The per-call worst-case pre-charge (fee × retry multiplier). Single source of
# truth is scripts/cost.py — never hardcode $0.04 here.
DEEPEN_WORST_CASE = RETRIEVAL_FEES["deep_reasoning"] * RETRY_MULTIPLIER

# Honesty system prompt for deep-reasoning calls.
# Never fabricate sources/URLs/quotes; mark unverified; cite real sources only.
HONESTY = (
    "You are a rigorous research agent. Follow these integrity rules WITHOUT "
    "exception. (1) NO FABRICATION: never invent sources, URLs, quotes, "
    "figures, or findings. If a claim cannot be grounded in a real source, "
    "mark it UNVERIFIED and say so explicitly. (2) Cite only REAL sources you "
    "actually consulted, with enough detail (author/title/year/venue or URL) "
    "to verify. (3) Date-stamp live or statistical claims; if you do not know "
    "the date, mark it UNVERIFIED."
)

# Per-bucket preambles (mirror the online Phase-A framings). Prepended to the
# question to form the deep-reasoning query/prompt.
_PREAMBLE_ROOT_CAUSE = (
    "Investigate the causes, preconditions, and drivers behind this finding. ")
_PREAMBLE_CONSEQUENCE = (
    "Trace the first-order effects and the second-order knock-on consequences "
    "of this finding. ")
_PREAMBLE_GAP = (
    "This is an open question left unanswered by an earlier research round. "
    "Answer this open question directly, as thoroughly as the evidence allows. ")

_BUCKET_PREAMBLES = {
    "root_cause": _PREAMBLE_ROOT_CAUSE,
    "consequence": _PREAMBLE_CONSEQUENCE,
    "gap": _PREAMBLE_GAP,
}

_VALID_BUCKETS = tuple(_BUCKET_PREAMBLES.keys())

# EXACT Round-2 headers this script ingests.
_HEADER_ROOT_CAUSE = "## Root Cause Questions"
_HEADER_CONSEQUENCE = "## Consequence Questions"
_HEADER_NEW = "## New Questions"
_HEADER_OPENINGS = "## Openings"


def make_session():
    """A requests session whose adapter pins ``Retry(total=1)`` EXPLICITLY.

    The ledger's ×2 worst-case bound assumes at most one automatic retry per
    call; do not raise ``total`` here without revisiting RETRY_MULTIPLIER."""
    s = requests.Session()
    retry = CappedRetry(
        total=1,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["POST", "GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ---------------------------------------------------------------------------
# Ingestion — tolerant markdown header extraction (NEVER raises)
# ---------------------------------------------------------------------------

_NUMBERED = re.compile(r"^\d+[.)]\s+")


def _bullets_under(md, header):
    """Return the bullet strings under EXACT ``header`` up to the next ``##``
    header. Bullets are lines starting with ``-`` or ``*``. Non-bullet lines are
    skipped (they don't terminate the section — only a ``##`` header does)."""
    lines = md.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == header:
            i += 1
            while i < n:
                stripped = lines[i].strip()
                if stripped.startswith("##"):
                    break
                if stripped.startswith("- ") or stripped.startswith("* "):
                    out.append(stripped[2:].strip())
                elif _NUMBERED.match(stripped):
                    # numbered lists ("1. question") are a natural synthesis
                    # output; silently dropping them empties a paid bucket
                    out.append(_NUMBERED.sub("", stripped, count=1).strip())
                elif stripped in ("-", "*"):
                    pass  # bare bullet marker, no text
                i += 1
            break
        i += 1
    return out


def extract_buckets(md):
    """Parse the Round-2 markdown into (b1_root_cause, b2_consequence, b3_gap).

    b3 = ``## New Questions`` bullets THEN ``## Openings`` bullets appended.
    Missing header → empty bucket. Malformed input (non-str, parse trouble) →
    empties + a stderr warning. NEVER raises."""
    try:
        if not isinstance(md, str):
            raise TypeError(f"expected markdown str, got {type(md).__name__}")
        b1 = _bullets_under(md, _HEADER_ROOT_CAUSE)
        b2 = _bullets_under(md, _HEADER_CONSEQUENCE)
        b3 = _bullets_under(md, _HEADER_NEW) + _bullets_under(md, _HEADER_OPENINGS)
        # A header that is present but yields nothing is almost always a
        # synthesizer that wrote its questions as paragraphs instead of list
        # items. Silently empty buckets hand the whole 9-question budget to
        # the others, so say so.
        headers = md.split("\n")
        for header, items in ((_HEADER_ROOT_CAUSE, b1), (_HEADER_CONSEQUENCE, b2),
                              (_HEADER_NEW, None), (_HEADER_OPENINGS, None)):
            present = any(line.strip() == header for line in headers)
            empty = not items if items is not None else not _bullets_under(md, header)
            if present and empty:
                print(f"  ⚠ warning: '{header}' is present but has no list items "
                      f"(- / * / 1.) — its questions were not ingested",
                      file=sys.stderr)
        return b1, b2, b3
    except Exception as exc:  # noqa: BLE001 — tolerant by construction
        print(f"  ⚠ warning: Round-2 markdown malformed, ingesting empty buckets "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return [], [], []


# ---------------------------------------------------------------------------
# Allocation — Phase-A rules 1–4 (pure functions, unit-testable, no network)
# ---------------------------------------------------------------------------

def dedupe_buckets(b1, b2, b3):
    """Cross-bucket casefold+strip dedupe with priority b1 → b2 → b3 (rule 2).

    A question appearing in an earlier bucket stays there and is dropped from the
    later one(s). Empty/whitespace-only questions are filtered. Returns the three
    cleaned buckets (stripped originals)."""
    seen = set()
    out = []
    for bucket in (b1, b2, b3):
        kept = []
        for q in bucket:
            key = str(q).strip().casefold()
            if key and key not in seen:
                seen.add(key)
                kept.append(str(q).strip())
        out.append(kept)
    return out[0], out[1], out[2]


def allocate(b1, b2, b3, per_bucket=_PER_BUCKET, cap=_CAP):
    """Allocate up to ``cap`` (bucket, question) pairs (rules 1, 3, 4).

    Buckets are deduped first (rule 2). Take the first ``per_bucket`` of each in
    b1,b2,b3 order (the labelled head), then backfill from each bucket's leftover
    tail in the same order, capped at ``cap``."""
    b1, b2, b3 = dedupe_buckets(b1, b2, b3)
    labelled = (
        [("root_cause", q) for q in b1[:per_bucket]]
        + [("consequence", q) for q in b2[:per_bucket]]
        + [("gap", q) for q in b3[:per_bucket]]
    )
    leftovers = (
        [("root_cause", q) for q in b1[per_bucket:]]
        + [("consequence", q) for q in b2[per_bucket:]]
        + [("gap", q) for q in b3[per_bucket:]]
    )
    return (labelled + leftovers)[:cap]


# ---------------------------------------------------------------------------
# Exa deep-reasoning call + answer file
# ---------------------------------------------------------------------------

def build_request_body(bucket, question):
    """Assemble the Exa /search deep-reasoning body for one question."""
    preamble = _BUCKET_PREAMBLES.get(bucket, "")
    query = f"{preamble}{question}".strip()
    return {
        "query": query,
        "type": "deep-reasoning",
        "systemPrompt": HONESTY,
        "outputSchema": {
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        },
        "contents": {"highlights": True},
    }


def _extract_answer(payload):
    """Pull the answer text out of a deep-reasoning response.

    Current Exa shape: the synthesized payload lives at ``output.content`` —
    an object matching outputSchema (here ``{"answer": str}``) or a plain
    string for text schemas — with ``output.grounding`` carrying citations.
    Older deep responses surfaced a top-level ``answer``; kept as a fallback."""
    if not isinstance(payload, dict):
        return ""
    output = payload.get("output")
    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, dict) and isinstance(content.get("answer"), str):
            return content["answer"]
        if isinstance(content, str):
            return content
    ans = payload.get("answer")
    if isinstance(ans, str):
        return ans
    if isinstance(ans, dict) and isinstance(ans.get("answer"), str):
        return ans["answer"]
    return ""


def _collect_sources(payload):
    """Gather (url, highlight-snippet) source rows from the response results/
    highlights so the answer file can carry a real ## Sources block."""
    sources = []
    seen = set()
    if not isinstance(payload, dict):
        return sources
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        url = (raw.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = (raw.get("title") or "").strip()
        sources.append((url, title))
    return sources


def _collect_evidence(payload):
    """Gather the textual EVIDENCE in the response: results[].highlights
    (type-guarded: must be a list), results[].text when a str, plus strings
    inside output.grounding that sit under evidence-text keys (same set as
    sweep_numbers) — NEVER urls/ids/titles, whose numeric fragments would
    falsely suppress UNVERIFIED marks (S2/FE1)."""
    # Grounding keys whose values are EVIDENCE TEXT — mirror of
    # sweep_numbers._EVIDENCE_KEYS.
    evidence_keys = {"text", "highlights", "snippet", "snippets", "quote",
                     "excerpt", "content", "citation"}
    evidence = []
    if not isinstance(payload, dict):
        return evidence
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        highlights = raw.get("highlights")
        snippets = ([str(h) for h in highlights if h]
                    if isinstance(highlights, list) else [])
        text = raw.get("text")
        if isinstance(text, str) and text.strip():
            snippets.append(text.strip())
        if snippets:
            evidence.append({"url": (raw.get("url") or "").strip(), "snippets": snippets})
    output = payload.get("output")
    if isinstance(output, dict) and output.get("grounding") is not None:
        def _strings(node, under_evidence_key):
            if isinstance(node, str):
                if under_evidence_key and node.strip():
                    yield node.strip()
            elif isinstance(node, dict):
                for k, v in node.items():
                    yield from _strings(v, str(k).lower() in evidence_keys)
            elif isinstance(node, list):
                for v in node:
                    yield from _strings(v, under_evidence_key)
        snippets = list(_strings(output["grounding"], False))
        if snippets:
            evidence.append({"url": "(grounding)", "snippets": snippets})
    return evidence


UNVERIFIED_MARK = "[UNVERIFIED — not in retrieved evidence]"


def _mark_unverified(answer, evidence_text):
    """Prefix every sentence containing ANY quantity the evidence text does
    not support — one fabricated number marks the sentence even when its
    neighbors are supported. Empty evidence ⇒ every quantity sentence is
    marked (fail-safe, not fail-open).

    Markdown structure is preserved (S3): leading whitespace and a list
    marker ("- ", "* ", "1. ") are peeled BEFORE sentence work and
    reattached after, so nesting survives, the ordinal of a numbered item is
    never a claim, and the mark lands after the marker, before the
    sentence."""
    import re as _re
    from scripts import numkeys
    from scripts.lint_background import _split_sentences
    list_prefix = _re.compile(r"^(\s*)((?:[-*+]|\d+[.)])\s+)?")
    norm = numkeys.normalize_haystack(evidence_text or "")
    out_lines = []
    for line in answer.split("\n"):
        if not line.strip():
            out_lines.append(line)
            continue
        m = list_prefix.match(line)
        indent, marker = m.group(1), m.group(2) or ""
        body = line[m.end():]
        rebuilt = []
        for sent in _split_sentences(body):
            claims = numkeys.extract_claims(numkeys.strip_nonclaims(sent))
            if claims and (not norm or any(
                    not numkeys.claim_supported(c, norm) for c in claims)):
                rebuilt.append(f"{UNVERIFIED_MARK} {sent.strip()}")
            else:
                rebuilt.append(sent.strip())
        out_lines.append(f"{indent}{marker}{' '.join(rebuilt)}")
    return "\n".join(out_lines)


def _write_answer(path, question, answer, sources, evidence):
    lines = [f"# Round-2.5 answer", "", f"**Question:** {question}", "", answer.rstrip(), ""]
    lines += ["## Evidence", ""]
    if evidence:
        for e in evidence:
            lines.append(f"- {e['url'] or 'source'}")
            for s in e["snippets"]:
                snippet = s if len(s) <= 1500 else s[:1500] + " …[truncated]"
                lines.append(f"  > {snippet}")
    else:
        lines.append("_(no evidence returned)_")
    lines += ["", "## Sources", ""]
    if sources:
        for url, title in sources:
            lines.append(f"- {title or 'source'} — {url}")
    else:
        lines.append("_(no sources returned)_")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_question(idx, bucket, question, session, api_key, ledger, round25_dir):
    """Fire one Exa deep-reasoning call for ``question``, write its answer file.

    Returns True on success, False on fail-open skip. Ledger-charges BEFORE the
    call and reconciles from costDollars.total after. LedgerCapExceeded from the
    charge propagates (caller writes coverage + exits 21)."""
    answer_path = round25_dir / f"answer_{idx:02d}_{bucket}.md"

    # LEDGER: charge the worst case BEFORE the call. LedgerCapExceeded propagates.
    entry = ledger.charge("deepen_questions", "deep_reasoning", DEEPEN_WORST_CASE)

    body = build_request_body(bucket, question)
    try:
        resp = session.post(
            EXA_SEARCH_URL, headers={"x-api-key": api_key},
            json=body, timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — per-question fail-open by design
        ledger.reconcile(entry, 0.0)
        print(f"  ⚠ question {idx} ({bucket}) failed open "
              f"({type(exc).__name__}: {exc}) — skipped", file=sys.stderr)
        return False

    # Reconcile from costDollars.total when present; else None (unknown).
    actual = None
    cost = payload.get("costDollars") if isinstance(payload, dict) else None
    if isinstance(cost, dict) and "total" in cost:
        try:
            actual = float(cost["total"])
        except (TypeError, ValueError):
            actual = None
    ledger.reconcile(entry, actual)

    # FAIL-OPEN (S4): the money is already reconciled — an unexpected
    # exception in evidence processing must never kill the run. Fall back to
    # writing the answer unmarked-but-flagged and still count it answered.
    try:
        answer = _extract_answer(payload)
        sources = _collect_sources(payload)
        evidence = _collect_evidence(payload)
        (round25_dir / f"grounding_{idx:02d}.json").write_text(
            json.dumps({"results": payload.get("results"),
                        "grounding": (payload.get("output") or {}).get("grounding")
                        if isinstance(payload.get("output"), dict) else None},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        evidence_text = "\n".join(s for e in evidence for s in e["snippets"])
        answer = _mark_unverified(answer, evidence_text)
        _write_answer(answer_path, question, answer, sources, evidence)
    except Exception as exc:  # noqa: BLE001 — post-payment fail-open by design
        print(f"  ⚠ question {idx} ({bucket}) evidence processing failed "
              f"({type(exc).__name__}: {exc}) — answer written unverified",
              file=sys.stderr)
        try:
            raw_answer = _extract_answer(payload)
        except Exception:  # noqa: BLE001 — pathological payloads included
            raw_answer = ""
        _write_answer(answer_path, question,
                      "[UNVERIFIED — evidence processing failed]\n" + raw_answer,
                      [], [])
    return True


def _write_coverage(round25_dir, questions, answered, by_bucket):
    round25_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "questions": questions,
        "answered": answered,
        "by_bucket": by_bucket,
    }
    (round25_dir / "coverage.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


def _merge_coverage(round25_dir, questions, answered, by_bucket):
    """Add this call's counts to an existing coverage.json instead of
    replacing it — the single-question (Round-5) entry runs AFTER the normal
    pass and must not erase its record. An unreadable file is treated as
    empty."""
    prior = {}
    try:
        prior = json.loads((round25_dir / "coverage.json").read_text(encoding="utf-8"))
        if not isinstance(prior, dict):
            prior = {}
    except (OSError, ValueError):
        prior = {}
    merged_buckets = dict(prior.get("by_bucket") or {})
    for bucket, n in by_bucket.items():
        merged_buckets[bucket] = int(merged_buckets.get(bucket, 0) or 0) + n
    _write_coverage(round25_dir,
                    int(prior.get("questions", 0) or 0) + questions,
                    int(prior.get("answered", 0) or 0) + answered,
                    merged_buckets)


_ANSWER_INDEX_RE = re.compile(r"^(?:answer|grounding)_(\d+)(?:_|\.)")


def _next_answer_index(round25_dir):
    """First index no existing answer_NN_<bucket>.md or grounding_NN.json uses.

    Single-question calls used to start at 0 every time, so a second call in
    the same bucket overwrote the first answer, and a call in a different
    bucket overwrote grounding_00.json (which is not bucket-suffixed)."""
    highest = -1
    try:
        names = [p.name for p in round25_dir.iterdir()]
    except OSError:
        return 0
    for name in names:
        m = _ANSWER_INDEX_RE.match(name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--round2-file", default=None,
                    help="Round-2 synthesis markdown to ingest (normal path)")
    ap.add_argument("--single-question", default=None,
                    help="Round-5 entry: deepen ONE question, bypassing ingestion")
    ap.add_argument("--bucket", default=None, choices=_VALID_BUCKETS,
                    help="bucket for --single-question")
    ap.add_argument("--max-retrieval-usd", type=float, default=None)
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    layout = resolve_helper_layout(run_dir)
    require_managed_mutation(layout, "question deepening")
    round25_dir = layout.round2_5

    # Build the allocation (bucket, question) pairs.
    single = args.single_question is not None
    if single:
        if not args.bucket:
            print("--single-question requires --bucket", file=sys.stderr)
            return 2
        pairs = [(args.bucket, args.single_question)]
    else:
        if not args.round2_file:
            print("--round2-file is required for the normal deepening path",
                  file=sys.stderr)
            return 2
        try:
            md = Path(args.round2_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"cannot read --round2-file: {exc}", file=sys.stderr)
            return 2
        b1, b2, b3 = extract_buckets(md)
        pairs = allocate(b1, b2, b3)

    run_cfg = config.load_run_config()
    cap = args.max_retrieval_usd if args.max_retrieval_usd is not None else run_cfg.max_retrieval_usd

    # A single-question call is an ADDITION to whatever Round 2.5 already
    # produced: number its files after the existing ones and add its counts to
    # coverage.json. The normal path is a full pass and still writes from 0.
    start_idx = _next_answer_index(round25_dir) if single else 0
    write_coverage = _merge_coverage if single else _write_coverage

    n_questions = len(pairs)
    by_bucket_total = {"root_cause": 0, "consequence": 0, "gap": 0}
    by_bucket_answered = {"root_cause": 0, "consequence": 0, "gap": 0}
    for bucket, _q in pairs:
        by_bucket_total[bucket] = by_bucket_total.get(bucket, 0) + 1

    # No questions → write a zeroed coverage file and exit clean (no Exa call,
    # so no key needed).
    if n_questions == 0:
        write_coverage(round25_dir, 0, 0, by_bucket_answered)
        print("Round-2.5 deepening: no questions to deepen (coverage written).")
        return 0

    # PREFLIGHT — only hard-fail when we are actually about to call Exa.
    # A custom EXA_BASE_URL (proxy/gateway) may authenticate out-of-band, in
    # which case a missing EXA_API_KEY is not fatal.
    api_key = os.environ.get("EXA_API_KEY", "")
    if not api_key and not os.environ.get("EXA_BASE_URL"):
        print("EXA_API_KEY is not set — cannot run Exa deep-reasoning. "
              "Export EXA_API_KEY and re-run, or set EXA_BASE_URL to an "
              "Exa-compatible endpoint.", file=sys.stderr)
        return EXA_PREFLIGHT_EXIT

    ledger = RetrievalLedger(layout, cap)
    session = make_session()
    round25_dir.mkdir(parents=True, exist_ok=True)

    answered = 0
    for idx, (bucket, question) in enumerate(pairs, start=start_idx):
        try:
            ok = run_question(idx, bucket, question, session, api_key,
                              ledger, round25_dir)
        except LedgerCapExceeded as exc:
            # Refusal is an exit, never a silent skip: write coverage FIRST.
            write_coverage(round25_dir, n_questions, answered, by_bucket_answered)
            print(f"  ✗ retrieval cap reached: {exc}", file=sys.stderr)
            print("    coverage.json written reflecting the shortfall. "
                  "Raise --max-retrieval-usd to continue.", file=sys.stderr)
            return LedgerCapExceeded.EXIT_CODE
        if ok:
            answered += 1
            by_bucket_answered[bucket] = by_bucket_answered.get(bucket, 0) + 1

    write_coverage(round25_dir, n_questions, answered, by_bucket_answered)
    print(f"Round-2.5 deepening complete: {answered}/{n_questions} answered, "
          f"committed=${ledger.committed():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
