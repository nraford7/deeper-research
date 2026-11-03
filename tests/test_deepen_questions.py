"""deepen_questions — OFFLINE tests. Every Exa call is monkeypatched; NO network.

Round 2.5 question-driven deepening. The HTTP session factory is monkeypatched
to a FakeSession so no request ever leaves the process. A socket guard fixture
makes any accidental real connection fail loudly.
"""

import json
import socket

import pytest

from scripts import deepen_questions
from scripts.ledger import LedgerCapExceeded


# --- socket guard -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_socket(monkeypatch):
    """Any real outbound connection is a test bug — make it explode."""
    def boom(*a, **k):
        raise AssertionError("deepen_questions opened a real socket in a test")
    monkeypatch.setattr(socket.socket, "connect", boom)


# --- fixtures / helpers -----------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FakeSession:
    """Records every POST and returns queued canned bodies (or raises)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "body": json})
        if not self._responses:
            # Default: a trivial well-formed answer so tests that don't care
            # about the Nth response still get something.
            return FakeResponse(_answer_body("default answer"))
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeResponse(nxt)


def _answer_body(answer="an answer", cost_total=None, urls=None, highlights=None):
    """A deep-reasoning response body. Current Exa shape: the structured
    answer lives at output.content, plus result records carrying
    urls/highlights we turn into a Sources block."""
    body = {"output": {"content": {"answer": answer}, "grounding": []}}
    results = []
    for u in (urls or []):
        results.append({"url": u, "title": "T", "highlights": highlights or []})
    if results:
        body["results"] = results
    if cost_total is not None:
        body["costDollars"] = {"total": cost_total}
    return body


def _md(root_cause=None, consequence=None, new_questions=None, openings=None,
        *, mangle=None):
    """Build a Round-2 markdown blob with the exact headers."""
    parts = ["## Comparison", "- something", ""]
    if root_cause is not None:
        parts.append("## Root Cause Questions")
        parts += [f"- {q}" for q in root_cause]
        parts.append("")
    if consequence is not None:
        parts.append("## Consequence Questions")
        parts += [f"- {q}" for q in consequence]
        parts.append("")
    if new_questions is not None:
        parts.append("## New Questions")
        parts += [f"- {q}" for q in new_questions]
        parts.append("")
    if openings is not None:
        parts.append("## Openings")
        parts += [f"- {q}" for q in openings]
        parts.append("")
    text = "\n".join(parts)
    if mangle:
        text = mangle(text)
    return text


def _patch_common(monkeypatch, session):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(deepen_questions, "make_session", lambda: session)


def _write_round2(tmp_path, md):
    p = tmp_path / "round2.md"
    p.write_text(md, encoding="utf-8")
    return p


# ===========================================================================
# extract_buckets — header extraction (tolerant, never raises)
# ===========================================================================

def test_extract_buckets_all_headers():
    md = _md(root_cause=["rc1", "rc2"], consequence=["cq1"],
             new_questions=["nq1"], openings=["op1"])
    b1, b2, b3 = deepen_questions.extract_buckets(md)
    assert b1 == ["rc1", "rc2"]
    assert b2 == ["cq1"]
    # b3 = new_questions THEN openings appended
    assert b3 == ["nq1", "op1"]


def test_extract_buckets_star_bullets():
    md = "## Root Cause Questions\n* rc-star\n* rc-two\n"
    b1, b2, b3 = deepen_questions.extract_buckets(md)
    assert b1 == ["rc-star", "rc-two"]


def test_extract_buckets_missing_header_empty():
    # Only New Questions present.
    md = _md(new_questions=["q1", "q2"])
    b1, b2, b3 = deepen_questions.extract_buckets(md)
    assert b1 == []
    assert b2 == []
    assert b3 == ["q1", "q2"]


def test_extract_buckets_stops_at_next_header():
    md = ("## Root Cause Questions\n"
          "- rc1\n"
          "## Consequence Questions\n"
          "- cq1\n")
    b1, b2, b3 = deepen_questions.extract_buckets(md)
    assert b1 == ["rc1"]
    assert b2 == ["cq1"]


def test_extract_buckets_empty_string_never_raises():
    b1, b2, b3 = deepen_questions.extract_buckets("")
    assert (b1, b2, b3) == ([], [], [])


def test_extract_buckets_malformed_never_raises(capsys):
    # Pass a non-string — must not raise, returns empties + a stderr warning.
    b1, b2, b3 = deepen_questions.extract_buckets(None)
    assert (b1, b2, b3) == ([], [], [])
    err = capsys.readouterr().err
    assert "warn" in err.lower() or "malformed" in err.lower()


def test_extract_buckets_ignores_non_bullet_lines():
    md = ("## Root Cause Questions\n"
          "Some intro prose that is not a bullet.\n"
          "- rc1\n"
          "not a bullet either\n"
          "- rc2\n")
    b1, _, _ = deepen_questions.extract_buckets(md)
    assert b1 == ["rc1", "rc2"]


# ===========================================================================
# allocate — Phase-A rules 1–4 (3/3/3 cap 9, dedupe, backfill)
# ===========================================================================

def test_allocate_full_3_3_3():
    b1 = [f"rc{i}" for i in range(5)]
    b2 = [f"cq{i}" for i in range(5)]
    b3 = [f"nq{i}" for i in range(5)]
    pairs = deepen_questions.allocate(b1, b2, b3)
    assert len(pairs) == 9
    labels = [lbl for lbl, _ in pairs]
    qs = [q for _, q in pairs]
    assert labels[:3] == ["root_cause"] * 3
    assert qs[:3] == ["rc0", "rc1", "rc2"]
    assert labels[3:6] == ["consequence"] * 3
    assert qs[3:6] == ["cq0", "cq1", "cq2"]
    assert labels[6:9] == ["gap"] * 3
    assert qs[6:9] == ["nq0", "nq1", "nq2"]


def test_allocate_cap_9_not_exceeded():
    b1 = [f"rc{i}" for i in range(20)]
    b2 = [f"cq{i}" for i in range(20)]
    b3 = [f"nq{i}" for i in range(20)]
    assert len(deepen_questions.allocate(b1, b2, b3)) == 9


def test_allocate_thin_b1_backfills():
    b1 = ["rc0"]
    b2 = ["cq0", "cq1", "cq2", "cq3", "cq4"]
    b3 = ["nq0", "nq1", "nq2"]
    pairs = deepen_questions.allocate(b1, b2, b3)
    assert len(pairs) == 9
    qs = [q for _, q in pairs]
    assert "rc0" in qs
    # leftover backfill pulls cq3, cq4 (b2 leftovers before b3 leftovers)
    assert "cq3" in qs
    assert "cq4" in qs


def test_allocate_all_empty():
    assert deepen_questions.allocate([], [], []) == []


def test_allocate_dedupe_priority_b1_over_b3():
    # A question in b1 AND b3 should have been removed from b3 during extraction;
    # allocate operates on already-deduped buckets, but the module dedupes as a
    # unit. Assert the whole ingest+allocate pipe dedupes correctly.
    md = _md(root_cause=["dupe q", "rc-other"],
             consequence=["cq1"],
             new_questions=["dupe q", "nq-other"])
    b1, b2, b3 = deepen_questions.extract_buckets(md)
    b1, b2, b3 = deepen_questions.dedupe_buckets(b1, b2, b3)
    assert "dupe q" in b1
    assert "dupe q" not in b3


def test_dedupe_casefold_and_strip():
    b1, b2, b3 = deepen_questions.dedupe_buckets(
        ["  Why Does Cost Decline?  "], [], ["why does cost decline?"])
    assert b1 == ["Why Does Cost Decline?"]
    assert b3 == []


def test_dedupe_b2_wins_over_b3():
    b1, b2, b3 = deepen_questions.dedupe_buckets(
        ["rc1"], ["dupe", "cq-other"], ["dupe", "nq-other"])
    assert "dupe" in b2
    assert "dupe" not in b3


def test_dedupe_empty_strings_filtered():
    b1, b2, b3 = deepen_questions.dedupe_buckets(
        ["  ", "", "rc-valid"], [""], ["", "nq-valid"])
    assert b1 == ["rc-valid"]
    assert b2 == []
    assert b3 == ["nq-valid"]


# ===========================================================================
# fan-out: ledger charge/reconcile + request shape + outputs
# ===========================================================================

def test_request_shape_deep_reasoning(monkeypatch, tmp_path):
    session = FakeSession([_answer_body("A", urls=["https://x.com/1"])])
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["Why does X happen?"])
    rc = deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    assert rc == 0
    body = session.calls[0]["body"]
    assert body["type"] == "deep-reasoning"
    assert "systemPrompt" in body and body["systemPrompt"]
    assert body["outputSchema"]["required"] == ["answer"]
    assert body["contents"] == {"highlights": True}
    # root_cause preamble prepended to the question
    assert "causes, preconditions, and drivers" in body["query"]
    assert "Why does X happen?" in body["query"]
    # header carries the api key
    assert session.calls[0]["headers"]["x-api-key"] == "test-key"


def test_consequence_and_gap_preambles(monkeypatch, tmp_path):
    session = FakeSession([_answer_body("A"), _answer_body("B")])
    _patch_common(monkeypatch, session)
    md = _md(consequence=["What happens next?"], new_questions=["Open one?"])
    deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    queries = [c["body"]["query"] for c in session.calls]
    assert any("first-order effects" in q and "second-order" in q for q in queries)
    # gap uses the neutral "answer this open question" framing
    assert any("open question" in q.lower() for q in queries)


def test_ledger_charge_004_then_reconcile(monkeypatch, tmp_path):
    session = FakeSession([
        _answer_body("A", cost_total=0.011),
        _answer_body("B", cost_total=0.011),
    ])
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["q1"], consequence=["q2"])
    deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    led = json.loads((tmp_path / "retrieval_ledger.json").read_text())
    assert len(led["entries"]) == 2
    for e in led["entries"]:
        assert e["worst_case_usd"] == pytest.approx(0.04)   # $0.02 fee × 2 retry
        assert e["actual_usd"] == pytest.approx(0.011)      # from costDollars.total


def test_ledger_reconciles_none_when_cost_absent(monkeypatch, tmp_path):
    session = FakeSession([_answer_body("A")])   # no costDollars
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["q1"])
    deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    led = json.loads((tmp_path / "retrieval_ledger.json").read_text())
    assert led["entries"][0]["actual_usd"] is None


def test_ledger_refusal_writes_coverage_then_exit_21(monkeypatch, tmp_path):
    # Cap allows exactly ONE $0.04 charge; the second question's charge is refused.
    session = FakeSession([
        _answer_body("A", cost_total=0.04),
        _answer_body("B"),   # never reached
    ])
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["q1"], consequence=["q2"])
    rc = deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md)),
         "--max-retrieval-usd", "0.04"])
    assert rc == LedgerCapExceeded.EXIT_CODE
    # coverage.json was written FIRST, reflecting the shortfall
    cov = json.loads((tmp_path / "round2_5" / "coverage.json").read_text())
    assert cov["questions"] == 2
    assert cov["answered"] == 1     # only the first completed before the refusal
    # first answer file survives
    assert (tmp_path / "round2_5" / "answer_00_root_cause.md").exists()


def test_per_question_fail_open(monkeypatch, tmp_path):
    # First question errors (HTTP), the rest proceed.
    session = FakeSession([
        RuntimeError("network boom"),
        _answer_body("second answer"),
    ])
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["q1"], consequence=["q2"])
    rc = deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    assert rc == 0
    cov = json.loads((tmp_path / "round2_5" / "coverage.json").read_text())
    assert cov["questions"] == 2
    assert cov["answered"] == 1   # one skipped, one succeeded
    # the failed question was reconciled to 0.0
    led = json.loads((tmp_path / "retrieval_ledger.json").read_text())
    assert led["entries"][0]["actual_usd"] == pytest.approx(0.0)


def test_answer_files_carry_sources(monkeypatch, tmp_path):
    session = FakeSession([
        _answer_body("The answer body.", urls=["https://src.com/a", "https://src.com/b"]),
    ])
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["q1"])
    deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    txt = (tmp_path / "round2_5" / "answer_00_root_cause.md").read_text()
    assert "The answer body." in txt
    assert "## Sources" in txt
    assert "https://src.com/a" in txt
    assert "https://src.com/b" in txt


def test_coverage_by_bucket(monkeypatch, tmp_path):
    session = FakeSession([
        _answer_body("a"), _answer_body("b"), _answer_body("c"),
    ])
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["r1"], consequence=["c1"], new_questions=["g1"])
    deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    cov = json.loads((tmp_path / "round2_5" / "coverage.json").read_text())
    assert cov["questions"] == 3
    assert cov["answered"] == 3
    assert cov["by_bucket"] == {"root_cause": 1, "consequence": 1, "gap": 1}


def test_empty_round2_no_calls(monkeypatch, tmp_path):
    session = FakeSession([])
    _patch_common(monkeypatch, session)
    md = _md()   # no question headers
    rc = deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    assert rc == 0
    assert session.calls == []
    cov = json.loads((tmp_path / "round2_5" / "coverage.json").read_text())
    assert cov["questions"] == 0
    assert cov["answered"] == 0


# ===========================================================================
# single-question Round-5 entry
# ===========================================================================

def test_single_question_one_call(monkeypatch, tmp_path):
    session = FakeSession([_answer_body("targeted answer", urls=["https://z.com/1"])])
    _patch_common(monkeypatch, session)
    rc = deepen_questions.main(
        ["--run-dir", str(tmp_path),
         "--single-question", "What is the weak-section gap?",
         "--bucket", "gap"])
    assert rc == 0
    assert len(session.calls) == 1
    body = session.calls[0]["body"]
    assert "What is the weak-section gap?" in body["query"]
    assert "open question" in body["query"].lower()
    # one answer file written, still ledger-charged
    files = list((tmp_path / "round2_5").glob("answer_*_gap.md"))
    assert len(files) == 1
    led = json.loads((tmp_path / "retrieval_ledger.json").read_text())
    assert len(led["entries"]) == 1
    assert led["entries"][0]["worst_case_usd"] == pytest.approx(0.04)


def test_single_question_root_cause_bucket_preamble(monkeypatch, tmp_path):
    session = FakeSession([_answer_body("A")])
    _patch_common(monkeypatch, session)
    deepen_questions.main(
        ["--run-dir", str(tmp_path),
         "--single-question", "Why did it happen?",
         "--bucket", "root_cause"])
    body = session.calls[0]["body"]
    assert "causes, preconditions, and drivers" in body["query"]


# ===========================================================================
# preflight + session retry
# ===========================================================================

def test_missing_exa_key_exits_20(monkeypatch, tmp_path):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    md = _md(root_cause=["q1"])
    rc = deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    assert rc == deepen_questions.EXA_PREFLIGHT_EXIT


def test_session_pins_retry_total_1():
    s = deepen_questions.make_session()
    adapter = s.get_adapter("https://api.exa.ai/search")
    assert adapter.max_retries.total == 1


def test_bad_bucket_arg_exits_2(monkeypatch, tmp_path):
    # argparse rejects an out-of-choices --bucket with SystemExit(2).
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    with pytest.raises(SystemExit) as ei:
        deepen_questions.main(
            ["--run-dir", str(tmp_path),
             "--single-question", "q", "--bucket", "nonsense"])
    assert ei.value.code == 2


def test_single_question_missing_bucket_exits_2(monkeypatch, tmp_path):
    # --single-question with no --bucket at all is a controlled return 2.
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    rc = deepen_questions.main(
        ["--run-dir", str(tmp_path), "--single-question", "q"])
    assert rc == 2


# ===========================================================================
# single-question calls add to Round 2.5, never overwrite it (issue #9)
# ===========================================================================

def _single(monkeypatch, tmp_path, question, bucket, answer="A", cap=None):
    session = FakeSession([_answer_body(answer, urls=["https://z.com/1"])])
    _patch_common(monkeypatch, session)
    argv = ["--run-dir", str(tmp_path), "--single-question", question, "--bucket", bucket]
    if cap is not None:
        argv += ["--max-retrieval-usd", str(cap)]
    return deepen_questions.main(argv)


def test_second_single_question_in_same_bucket_keeps_the_first(monkeypatch, tmp_path):
    assert _single(monkeypatch, tmp_path, "Q1", "root_cause", answer="first") == 0
    assert _single(monkeypatch, tmp_path, "Q2", "root_cause", answer="second") == 0
    r25 = tmp_path / "round2_5"
    files = sorted(p.name for p in r25.glob("answer_*_root_cause.md"))
    assert files == ["answer_00_root_cause.md", "answer_01_root_cause.md"]
    assert "first" in (r25 / "answer_00_root_cause.md").read_text()
    assert "second" in (r25 / "answer_01_root_cause.md").read_text()
    cov = json.loads((r25 / "coverage.json").read_text())
    assert cov["questions"] == 2 and cov["answered"] == 2
    assert cov["by_bucket"]["root_cause"] == 2


def test_single_question_after_normal_pass_extends_it(monkeypatch, tmp_path):
    # Normal 3-question pass first (indices 0..2, one grounding file each).
    session = FakeSession([_answer_body("a"), _answer_body("b"), _answer_body("c")])
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["r1"], consequence=["c1"], new_questions=["g1"])
    assert deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))]) == 0
    r25 = tmp_path / "round2_5"
    before = {p.name: p.read_bytes() for p in r25.iterdir()}
    assert "grounding_00.json" in before and "coverage.json" in before

    # A Round-5 root-cause follow-up lands at index 3 in a different bucket
    # than index 0 — grounding_00.json (not bucket-suffixed) must survive.
    assert _single(monkeypatch, tmp_path, "Why really?", "gap", answer="deeper") == 0
    assert (r25 / "answer_03_gap.md").exists()
    assert (r25 / "grounding_03.json").exists()
    for name, data in before.items():
        if name != "coverage.json":
            assert (r25 / name).read_bytes() == data, f"{name} was rewritten"
    cov = json.loads((r25 / "coverage.json").read_text())
    assert cov["questions"] == 4 and cov["answered"] == 4
    assert cov["by_bucket"] == {"root_cause": 1, "consequence": 1, "gap": 2}


def test_single_question_cap_refusal_merges_coverage(monkeypatch, tmp_path):
    assert _single(monkeypatch, tmp_path, "Q1", "gap") == 0
    # cap already spent by the ledger of the first call: the second is refused
    rc = _single(monkeypatch, tmp_path, "Q2", "gap", cap=0.0)
    assert rc == LedgerCapExceeded.EXIT_CODE
    cov = json.loads((tmp_path / "round2_5" / "coverage.json").read_text())
    assert cov["questions"] == 2 and cov["answered"] == 1
    assert cov["by_bucket"]["gap"] == 1


def test_normal_pass_still_numbers_from_zero(monkeypatch, tmp_path):
    session = FakeSession([_answer_body("a")])
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["r1"])
    assert deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))]) == 0
    assert (tmp_path / "round2_5" / "answer_00_root_cause.md").exists()


def test_next_answer_index_reads_both_answer_and_grounding_files(tmp_path):
    assert deepen_questions._next_answer_index(tmp_path / "missing") == 0
    r25 = tmp_path / "round2_5"
    r25.mkdir()
    assert deepen_questions._next_answer_index(r25) == 0
    (r25 / "answer_00_gap.md").write_text("x")
    (r25 / "grounding_04.json").write_text("{}")
    (r25 / "coverage.json").write_text("{}")
    assert deepen_questions._next_answer_index(r25) == 5


def test_extract_buckets_warns_when_header_has_no_items(capsys):
    md = ("## Root Cause Questions\n"
          "Why does the thing happen? It seems driven by X.\n\n"
          "## Openings\n- a real bullet\n")
    b1, b2, b3 = deepen_questions.extract_buckets(md)
    assert b1 == [] and b3 == ["a real bullet"]
    err = capsys.readouterr().err
    assert "Root Cause Questions" in err and "no list items" in err
    assert "Openings" not in err
    # an absent header is not a warning (the common case)
    capsys.readouterr()
    deepen_questions.extract_buckets("## Openings\n- q\n")
    assert "warning" not in capsys.readouterr().err


def test_normal_rerun_sets_aside_stale_single_question_files(monkeypatch, tmp_path):
    """A normal pass after Round-5 additions replaces the round: the earlier
    answer/grounding files move to round2_5/superseded/, so nothing that globs
    answer_*.md reads a stale answer beside the new ones."""
    assert _single(monkeypatch, tmp_path, "Q1", "gap", answer="old") == 0
    assert _single(monkeypatch, tmp_path, "Q2", "gap", answer="older") == 0
    r25 = tmp_path / "round2_5"
    assert (r25 / "answer_01_gap.md").exists()

    session = FakeSession([_answer_body("fresh")])
    _patch_common(monkeypatch, session)
    md = _md(root_cause=["r1"])
    assert deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))]) == 0
    top = sorted(p.name for p in r25.iterdir() if p.is_file())
    assert top == ["answer_00_root_cause.md", "coverage.json", "grounding_00.json"]
    moved = sorted(p.name for p in (r25 / "superseded").rglob("*") if p.is_file())
    assert moved == ["answer_00_gap.md", "answer_01_gap.md",
                     "grounding_00.json", "grounding_01.json"]
    assert json.loads((r25 / "coverage.json").read_text())["questions"] == 1


def test_preflight_failure_leaves_prior_answers_in_place(monkeypatch, tmp_path):
    """Nothing is moved unless the pass is actually about to call Exa."""
    assert _single(monkeypatch, tmp_path, "Q1", "gap") == 0
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("EXA_BASE_URL", raising=False)
    md = _md(root_cause=["r1"])
    rc = deepen_questions.main(
        ["--run-dir", str(tmp_path), "--round2-file", str(_write_round2(tmp_path, md))])
    assert rc == deepen_questions.EXA_PREFLIGHT_EXIT
    assert (tmp_path / "round2_5" / "answer_00_gap.md").exists()
    assert not (tmp_path / "round2_5" / "superseded").exists()


def test_two_archives_in_one_second_both_survive(monkeypatch, tmp_path):
    r25 = tmp_path / "round2_5"
    r25.mkdir()
    monkeypatch.setattr(deepen_questions.time, "strftime", lambda *a: "20261103T090000Z")
    (r25 / "answer_00_gap.md").write_text("first")
    assert deepen_questions._set_aside_prior_answers(r25) == 1
    (r25 / "answer_00_gap.md").write_text("second")
    assert deepen_questions._set_aside_prior_answers(r25) == 1
    sup = r25 / "superseded"
    assert (sup / "20261103T090000Z" / "answer_00_gap.md").read_text() == "first"
    assert (sup / "20261103T090000Z-2" / "answer_00_gap.md").read_text() == "second"
