#!/usr/bin/env python3
"""agent_scout.py — Round-1 scout: one Exa Agent ``ultra`` run per research run.

The scout asks Exa Agent (effort ``ultra``) for every high-quality source on the
topic and writes them as one more Round-1 slice:

  * ``round1/slice_agent_scout.jsonl`` — same item schema as slice_search.py
  * ``round1/brief_agent_scout.md``    — the usual brief + ``## Sources`` block

Every downstream stage globs ``slice_*.jsonl``, so these sources pass the same
full-text fetch, evidence gate, citation chase, coverage audit and citation
verification as any search hit. The agent's own prose is never kept: only
URLs, titles, years, authors and a one-line finding (stored as a highlight).

WHY: on 2026-09-28 one ultra run on a finished run's question found 54 sources
the Bible lacked. Re-running 27 of its searches at 100 results each found only
11 of those 54, so the gain comes from the agent's follow-up queries, not from
wider searches.

MONEY: the scout has its OWN ledger (``agent_ledger.json`` beside the retrieval
ledger) with its own cap (``[run].agent_scout_usd``, default $10, or
``--max-agent-usd``; 0 turns the scout off). The whole cap is charged BEFORE
the run starts and reconciled from ``costDollars.total`` after.

RESUME: the run id is saved to ``agent_scout_run.json`` before polling, so a
crash or a ``--start-only`` call resumes the SAME paid run. A finished or failed
scout is never started again for this run dir.

FAIL-OPEN: a failed run writes an empty slice + a notice and exits 0, like a
failed search slice. Thin evidence is the evidence gate's job.

Usage:
  python3 scripts/agent_scout.py --run-dir DIR --topic "..." [--scope "..."]
      [--max-agent-usd X] [--max-seconds N] [--start-only]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - dependency preflight
    sys.stderr.write("Missing dep: pip install requests\n")
    sys.exit(1)

# Allow running both as `python3 scripts/agent_scout.py` and `-m scripts.agent_scout`.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
from scripts.helper_runtime import require_managed_mutation, resolve_helper_layout
from scripts.ledger import LedgerCapExceeded, RetrievalLedger
from scripts.slice_search import (
    EXA_BASE_URL, EXA_PREFLIGHT_EXIT, _norm_key, _result_to_item,
    _spill_fulltext, _write_brief, _write_jsonl,
)

AGENT_RUNS_URL = EXA_BASE_URL + "/agent/runs"
SLICE_NAME = "agent_scout"
# The Agent API accepts budget.maxCostDollars in [1, 100] and
# budget.maxDurationSeconds in [300, 10800].
MIN_USD = 1.0
MAX_USD = 100.0
DEFAULT_MAX_SECONDS = 2700
POLL_SECONDS = 30
POLL_GRACE_SECONDS = 900
MAX_POLL_ERRORS = 20

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "title": {"type": "string"},
                    "year": {"type": "string"},
                    "authors": {"type": "string"},
                    "finding": {"type": "string"},
                },
                "required": ["url", "title", "year", "authors", "finding"],
            },
        }
    },
    "required": ["sources"],
}


class AgentError(RuntimeError):
    """An Exa Agent API call failed (HTTP, transport, or bad body)."""


def _sleep(seconds):
    time.sleep(seconds)


def _poll_session():
    """GET session with one retry. The create POST never retries: a retried
    create could start (and bill) a second run."""
    s = requests.Session()
    retry = Retry(total=1, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _headers(api_key):
    h = {"Content-Type": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    return h


def _create_run(api_key, body):
    """Start one Agent run; return its id."""
    try:
        resp = requests.post(AGENT_RUNS_URL, headers=_headers(api_key), json=body, timeout=60)
        resp.raise_for_status()
        run_id = resp.json().get("id")
    except Exception as exc:  # noqa: BLE001
        raise AgentError(f"create failed ({type(exc).__name__}: {exc})") from exc
    if not run_id:
        raise AgentError("create returned no run id")
    return run_id


_SESSION = None


def _get_run(api_key, run_id):
    """Fetch one Agent run object."""
    global _SESSION
    if _SESSION is None:
        _SESSION = _poll_session()
    try:
        resp = _SESSION.get(f"{AGENT_RUNS_URL}/{run_id}", headers=_headers(api_key), timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise AgentError(f"poll failed ({type(exc).__name__}: {exc})") from exc
    if not isinstance(data, dict):
        raise AgentError("poll returned a non-object body")
    return data


def build_query(topic, scope=None):
    scope_line = f" Scope: {scope.strip()}" if scope and scope.strip() else ""
    return (
        f"Find every high-quality source that bears on this research question: "
        f"{topic.strip()}.{scope_line} Prefer meta-analyses, systematic reviews, "
        "peer-reviewed studies and authoritative institutional or practitioner "
        "reports; include contested findings and failed replications. For each "
        "source give the URL of the source itself, its title, year, authors or "
        "publisher, and a one-sentence key finding with numbers where available."
    )


def _state_path(layout):
    return Path(layout.ledger).parent / "agent_scout_run.json"


def _load_state(layout):
    path = _state_path(layout)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_state(layout, state):
    path = _state_path(layout)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _to_item(src):
    """Map one agent source to the slice item schema, or None if unusable."""
    if not isinstance(src, dict):
        return None
    url = str(src.get("url") or "").strip()
    if urlsplit(url).scheme not in ("http", "https") or len(url) > 2048:
        return None
    finding = str(src.get("finding") or "").strip()
    year = str(src.get("year") or "").strip()
    raw = {
        "title": str(src.get("title") or "").strip()[:300],
        "url": url,
        "publishedDate": year or None,
        "author": str(src.get("authors") or "").strip() or None,
        "highlights": [finding] if finding else [],
    }
    return _result_to_item(raw, SLICE_NAME)


def _write_slice(layout, round1_dir, sources):
    seen, items = set(), []
    for src in sources:
        item = _to_item(src)
        if item is None:
            continue
        key = _norm_key(item["url"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    _spill_fulltext(items, round1_dir, layout=layout)
    _write_jsonl(round1_dir / f"slice_{SLICE_NAME}.jsonl", items)
    _write_brief(round1_dir / f"brief_{SLICE_NAME}.md", SLICE_NAME, items)
    return len(items)


def _fail_open(layout, round1_dir, state, reason):
    _write_jsonl(round1_dir / f"slice_{SLICE_NAME}.jsonl", [])
    _write_brief(round1_dir / f"brief_{SLICE_NAME}.md", SLICE_NAME, [])
    state.update(state="failed", reason=reason)
    _save_state(layout, state)
    print(f"  ⚠ agent scout failed open ({reason}) — empty slice written", file=sys.stderr)
    return 0


def _poll(api_key, run_id, max_seconds):
    polls_left = (max_seconds + POLL_GRACE_SECONDS) // POLL_SECONDS + 1
    errors = 0
    while True:
        try:
            run = _get_run(api_key, run_id)
            errors = 0
        except AgentError:
            errors += 1
            if errors > MAX_POLL_ERRORS:
                raise
            run = {}
        status = run.get("status")
        if status == "completed":
            return run
        if status in ("failed", "cancelled"):
            raise AgentError(f"run {status}: {(run.get('error') or {}).get('message')!r}")
        polls_left -= 1
        if polls_left <= 0:
            raise AgentError(f"run still {status!r} past the poll deadline")
        _sleep(POLL_SECONDS)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--scope", default=None)
    ap.add_argument("--max-agent-usd", type=float, default=None)
    ap.add_argument("--max-seconds", type=int, default=DEFAULT_MAX_SECONDS)
    ap.add_argument("--start-only", action="store_true",
                    help="start the run, save its id, exit; a later call collects it")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    layout = resolve_helper_layout(run_dir)
    require_managed_mutation(layout, "agent scout")
    round1_dir = layout.round1
    round1_dir.mkdir(parents=True, exist_ok=True)

    state = _load_state(layout)
    if state and state.get("state") in ("done", "failed"):
        print(f"  ↻ agent scout already {state['state']} for this run — skipping")
        return 0

    run_cfg = config.load_run_config()
    cap = args.max_agent_usd if args.max_agent_usd is not None else run_cfg.agent_scout_usd
    if state is None and cap < MIN_USD:
        print(f"  · agent scout off (cap ${cap:.2f} < ${MIN_USD:.0f} minimum)")
        return 0

    api_key = os.environ.get("EXA_API_KEY", "")
    if not api_key and not os.environ.get("EXA_BASE_URL"):
        print("EXA_API_KEY is not set — cannot run the agent scout. Export "
              "EXA_API_KEY and re-run, or set EXA_BASE_URL to an Exa-compatible "
              "endpoint.", file=sys.stderr)
        return EXA_PREFLIGHT_EXIT

    ledger = RetrievalLedger(layout, max(cap, 0.0),
                             ledger_path=Path(layout.ledger).parent / "agent_ledger.json")
    max_seconds = max(300, min(10800, int(args.max_seconds)))

    if state is None:
        budget = max(MIN_USD, min(MAX_USD, round(cap, 2)))
        try:
            idx = ledger.charge("agent_scout", "agent_ultra", budget)
        except LedgerCapExceeded as exc:
            print(f"  ✗ agent scout cap reached: {exc}", file=sys.stderr)
            return LedgerCapExceeded.EXIT_CODE
        state = {"state": "starting", "ledger_index": idx, "budget_usd": budget}
        body = {
            "query": build_query(args.topic, args.scope),
            "effort": "ultra",
            "budget": {"maxCostDollars": budget, "maxDurationSeconds": max_seconds},
            "outputSchema": OUTPUT_SCHEMA,
        }
        try:
            run_id = _create_run(api_key, body)
        except AgentError as exc:
            ledger.reconcile(idx, 0.0)
            return _fail_open(layout, round1_dir, state, str(exc))
        state.update(state="pending", run_id=run_id)
        _save_state(layout, state)
        print(f"  ▸ agent scout run {run_id} started (cap ${budget:.2f})")
    else:
        run_id = state.get("run_id")
        if not run_id:
            return _fail_open(layout, round1_dir, state, "saved state has no run id")
        print(f"  ↻ agent scout run {run_id} resumed")

    if args.start_only:
        return 0

    try:
        run = _poll(api_key, run_id, max_seconds)
    except AgentError as exc:
        # Keep the worst-case charge: a failed run may still have been billed.
        return _fail_open(layout, round1_dir, state, str(exc))

    output = run.get("output") or {}
    sources = (output.get("structured") or {}).get("sources") or []
    kept = _write_slice(layout, round1_dir, sources)
    cost = None
    try:
        cost = float((run.get("costDollars") or {}).get("total"))
    except (TypeError, ValueError):
        pass
    try:
        ledger.reconcile(state["ledger_index"], cost)
    except (ValueError, IndexError, KeyError):
        pass
    state.update(state="done", kept=kept, cost_usd=cost, stop_reason=run.get("stopReason"))
    _save_state(layout, state)
    cost_s = f"${cost:.2f}" if cost is not None else "unknown cost"
    print(f"Agent scout complete: {kept} sources kept ({len(sources)} returned), "
          f"{cost_s}, stop={run.get('stopReason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
