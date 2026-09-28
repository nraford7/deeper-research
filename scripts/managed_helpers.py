"""Allowlisted broker adapters for mutating research helper CLIs."""

from __future__ import annotations

from pathlib import Path

from scripts.helper_runtime import broker_managed_context, require_managed_mutation
from scripts.run_state import make_state_guard


def _flag(argv, name, value):
    if value is None or value is False:
        return
    argv.append(name)
    if value is not True:
        argv.append(str(value))


def _guard(layout):
    require_managed_mutation(layout, "research helper")
    make_state_guard(layout)("write", "Process")


def managed_slice(*, layout, fs, typed_args):
    from scripts import slice_search
    _guard(layout)
    argv = ["--run-dir", str(layout.run_root), "--topic", typed_args["topic"]]
    for key, flag in (
        ("resume", "--resume"), ("max_retrieval_usd", "--max-retrieval-usd"),
        ("fresh_since", "--fresh-since"), ("only_slice", "--only-slice"),
        ("query", "--query"), ("add_slice", "--add-slice"),
    ):
        _flag(argv, flag, typed_args.get(key))
    with broker_managed_context():
        return {"exit_code": slice_search.main(argv)}


def managed_agent_scout(*, layout, fs, typed_args):
    from scripts import agent_scout
    _guard(layout)
    argv = ["--run-dir", str(layout.run_root), "--topic", typed_args["topic"]]
    for key, flag in (
        ("scope", "--scope"), ("max_agent_usd", "--max-agent-usd"),
        ("max_seconds", "--max-seconds"), ("start_only", "--start-only"),
    ):
        _flag(argv, flag, typed_args.get(key))
    with broker_managed_context():
        return {"exit_code": agent_scout.main(argv)}


def managed_fetch_fulltext(*, layout, fs, typed_args):
    from scripts import fetch_fulltext
    _guard(layout)
    argv = ["--run-dir", str(layout.run_root)]
    for key, flag in (("min_chars", "--min-chars"), ("max_bytes", "--max-bytes"), ("max_chars", "--max-chars"), ("timeout", "--timeout")):
        _flag(argv, flag, typed_args.get(key))
    with broker_managed_context():
        return {"exit_code": fetch_fulltext.main(argv)}


def managed_citation_chase(*, layout, fs, typed_args):
    from scripts import citation_chase
    _guard(layout)
    argv = ["--run-dir", str(layout.run_root), "--topic", typed_args["topic"]]
    for key, flag in (("max_seeds", "--max-seeds"), ("max_candidates", "--max-candidates"), ("openalex_call_ceiling", "--openalex-call-ceiling")):
        _flag(argv, flag, typed_args.get(key))
    if "forward" in typed_args:
        argv.append("--forward" if typed_args["forward"] else "--no-forward")
    with broker_managed_context():
        return {"exit_code": citation_chase.main(argv)}


def managed_coverage_audit(*, layout, fs, typed_args):
    from scripts import coverage_audit
    _guard(layout)
    argv = ["--run-dir", str(layout.run_root), "--topic", typed_args["topic"]]
    for key, flag in (("max_audit_rounds", "--max-audit-rounds"), ("audit_usd", "--audit-usd"), ("current_year", "--current-year")):
        _flag(argv, flag, typed_args.get(key))
    _flag(argv, "--use-matrix", typed_args.get("use_matrix"))
    with broker_managed_context():
        return {"exit_code": coverage_audit.main(argv)}


def managed_deepen(*, layout, fs, typed_args):
    from scripts import deepen_questions
    _guard(layout)
    argv = ["--run-dir", str(layout.run_root)]
    round2 = typed_args.get("round2_file")
    if round2:
        candidate = Path(round2)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("round2_file must be run-relative")
        _flag(argv, "--round2-file", layout.run_root / candidate)
    for key, flag in (("single_question", "--single-question"), ("bucket", "--bucket"), ("max_retrieval_usd", "--max-retrieval-usd")):
        _flag(argv, flag, typed_args.get(key))
    with broker_managed_context():
        return {"exit_code": deepen_questions.main(argv)}


def managed_ingest_local(*, layout, fs, typed_args):
    from scripts import ingest_local
    _guard(layout)
    files = typed_args["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("files must be a non-empty list of paths")
    argv = ["--run-dir", str(layout.run_root)]
    for key, flag in (("title", "--title"), ("slug", "--slug"), ("year", "--year")):
        _flag(argv, flag, typed_args.get(key))
    argv.extend(str(f) for f in files)
    with broker_managed_context():
        return {"exit_code": ingest_local.main(argv)}
