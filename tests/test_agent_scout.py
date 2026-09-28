"""agent_scout.py — OFFLINE. The Exa Agent HTTP calls are monkeypatched.

The scout is one Exa Agent ``ultra`` run per research run. Its sources land in
round1/slice_agent_scout.jsonl, so fetch_fulltext, the evidence gate, citation
chase and the coverage audit treat them like any other slice.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from scripts import agent_scout


SRC = {
    "url": "https://doi.org/10.1080/10410236.2022.2137750",
    "title": "Narrative vs statistical evidence: a meta-analysis",
    "year": "2022",
    "authors": "Xu",
    "finding": "Overall r = .016; the CI crosses zero.",
}


class FakeAgentAPI:
    """Stands in for _create_run / _get_run and records calls."""

    def __init__(self, polls=None, create_error=None):
        self.created = []
        self.polled = []
        self.polls = list(polls or [])
        self.create_error = create_error

    def create(self, api_key, body):
        self.created.append(body)
        if self.create_error:
            raise self.create_error
        return "agent_run_x"

    def get(self, api_key, run_id):
        self.polled.append(run_id)
        nxt = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _done(sources, cost=6.5):
    return {"id": "agent_run_x", "status": "completed", "stopReason": "schema_satisfied",
            "output": {"structured": {"sources": sources}},
            "costDollars": {"total": cost}}


def _run_cfg(agent_scout_usd=10.0):
    return config.RunConfig(
        mode="slices", max_retrieval_usd=1.0, min_evidence_total=1,
        min_nonempty_slices=1, slices={}, adversary_chain=["grok"],
        adversary="grok", synthesizer="claude", adversary_warning=None,
        agent_scout_usd=agent_scout_usd)


def _patch(monkeypatch, api, *, usd=10.0):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(config, "load_run_config", lambda *a, **k: _run_cfg(usd))
    monkeypatch.setattr(agent_scout, "_create_run", api.create)
    monkeypatch.setattr(agent_scout, "_get_run", api.get)
    monkeypatch.setattr(agent_scout, "_sleep", lambda s: None)


def _main(tmp_path, *extra):
    return agent_scout.main(["--run-dir", str(tmp_path), "--topic", "narrative persuasion",
                             "--scope", "meta-analyses; message order", *extra])


def _rows(tmp_path):
    p = tmp_path / "round1" / "slice_agent_scout.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_query_carries_topic_and_scope():
    q = agent_scout.build_query("narrative persuasion", "meta-analyses; message order")
    assert "narrative persuasion" in q and "message order" in q


def test_completed_run_writes_gate_visible_slice(monkeypatch, tmp_path):
    api = FakeAgentAPI(polls=[{"status": "running"}, _done([SRC, SRC])])
    _patch(monkeypatch, api)
    assert _main(tmp_path) == 0

    rows = _rows(tmp_path)
    assert len(rows) == 1  # deduped
    r = rows[0]
    assert r["slice"] == "agent_scout"
    assert r["url"] == SRC["url"]
    assert r["published_date"] == "2022"
    assert r["highlights"] == [SRC["finding"]]
    assert r["tier"] and r["authority_tag"]
    assert r["text_chars"] == 0  # fetch_fulltext fills the text later
    assert (tmp_path / "round1" / "brief_agent_scout.md").exists()

    body = api.created[0]
    assert body["effort"] == "ultra"
    assert body["budget"]["maxCostDollars"] == pytest.approx(10.0)
    assert body["outputSchema"]["properties"]["sources"]["type"] == "array"


def test_own_ledger_charged_then_reconciled(monkeypatch, tmp_path):
    _patch(monkeypatch, FakeAgentAPI(polls=[_done([SRC], cost=6.5)]))
    _main(tmp_path)
    led = json.loads((tmp_path / "agent_ledger.json").read_text())
    assert led["cap_usd"] == pytest.approx(10.0)
    assert led["entries"][0]["worst_case_usd"] == pytest.approx(10.0)
    assert led["entries"][0]["actual_usd"] == pytest.approx(6.5)
    # The search ledger ($1 cap) is untouched.
    assert not (tmp_path / "retrieval_ledger.json").exists()


def test_cap_flag_overrides_config(monkeypatch, tmp_path):
    api = FakeAgentAPI(polls=[_done([SRC])])
    _patch(monkeypatch, api)
    _main(tmp_path, "--max-agent-usd", "4")
    assert api.created[0]["budget"]["maxCostDollars"] == pytest.approx(4.0)


def test_zero_cap_turns_scout_off(monkeypatch, tmp_path):
    api = FakeAgentAPI(polls=[_done([SRC])])
    _patch(monkeypatch, api, usd=0.0)
    assert _main(tmp_path) == 0
    assert api.created == []
    assert not (tmp_path / "round1" / "slice_agent_scout.jsonl").exists()


def test_missing_key_exits_20(monkeypatch, tmp_path):
    api = FakeAgentAPI(polls=[_done([SRC])])
    _patch(monkeypatch, api)
    monkeypatch.delenv("EXA_API_KEY")
    monkeypatch.delenv("EXA_BASE_URL", raising=False)
    assert _main(tmp_path) == 20
    assert api.created == []


def test_start_only_then_collect_reuses_run(monkeypatch, tmp_path):
    api = FakeAgentAPI(polls=[_done([SRC])])
    _patch(monkeypatch, api)
    assert _main(tmp_path, "--start-only") == 0
    assert api.polled == []
    assert not (tmp_path / "round1" / "slice_agent_scout.jsonl").exists()

    assert _main(tmp_path) == 0
    assert len(api.created) == 1  # the second call resumed, not re-created
    assert api.polled and api.polled[0] == "agent_run_x"
    assert _rows(tmp_path)[0]["url"] == SRC["url"]
    led = json.loads((tmp_path / "agent_ledger.json").read_text())
    assert len(led["entries"]) == 1


def test_done_run_is_not_repeated(monkeypatch, tmp_path):
    api = FakeAgentAPI(polls=[_done([SRC])])
    _patch(monkeypatch, api)
    _main(tmp_path)
    _main(tmp_path)
    assert len(api.created) == 1


def test_failed_run_fails_open_with_empty_slice(monkeypatch, tmp_path):
    api = FakeAgentAPI(polls=[{"status": "failed", "error": {"message": "boom"}}])
    _patch(monkeypatch, api)
    assert _main(tmp_path) == 0
    assert _rows(tmp_path) == []
    state = json.loads((tmp_path / "agent_scout_run.json").read_text())
    assert state["state"] == "failed"
    # Not retried automatically.
    _main(tmp_path)
    assert len(api.created) == 1


def test_create_error_fails_open_and_frees_budget(monkeypatch, tmp_path):
    api = FakeAgentAPI(create_error=agent_scout.AgentError("HTTP 402"))
    _patch(monkeypatch, api)
    assert _main(tmp_path) == 0
    assert _rows(tmp_path) == []
    led = json.loads((tmp_path / "agent_ledger.json").read_text())
    assert led["entries"][0]["actual_usd"] == 0.0


def test_transient_poll_errors_are_tolerated(monkeypatch, tmp_path):
    api = FakeAgentAPI(polls=[agent_scout.AgentError("timeout"), _done([SRC])])
    _patch(monkeypatch, api)
    assert _main(tmp_path) == 0
    assert len(_rows(tmp_path)) == 1


def test_bad_urls_are_dropped(monkeypatch, tmp_path):
    bad = dict(SRC, url="javascript:alert(1)")
    _patch(monkeypatch, FakeAgentAPI(polls=[_done([bad, SRC])]))
    _main(tmp_path)
    assert [r["url"] for r in _rows(tmp_path)] == [SRC["url"]]


def test_run_config_reads_agent_scout_usd(tmp_path):
    toml = tmp_path / "c.toml"
    toml.write_text('[run]\nagent_scout_usd = 3.5\n')
    cfg = config.load_run_config([toml], env={})
    assert cfg.agent_scout_usd == pytest.approx(3.5)


def test_run_config_rejects_negative_agent_scout_usd(tmp_path):
    toml = tmp_path / "c.toml"
    toml.write_text('[run]\nagent_scout_usd = -1\n')
    with pytest.raises(Exception):
        config.load_run_config([toml], env={})


def test_managed_helper_is_registered():
    import importlib
    from scripts.run_manager import MANAGED_HELPERS
    module, func, args = MANAGED_HELPERS["agent-scout"]
    assert callable(getattr(importlib.import_module(module), func))
    assert {"topic", "scope", "max_agent_usd", "start_only"} <= args
