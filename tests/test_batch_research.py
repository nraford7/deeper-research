from scripts import batch_research
import pytest
import sys
import time
from types import SimpleNamespace


def test_adaptive_limit_starts_at_two_and_adds_one_per_healthy_window():
    limit = batch_research.AdaptiveLimit(
        initial=2,
        maximum=8,
        stability_window=60,
        backoff_seconds=120,
        now=0,
    )

    assert limit.target == 2
    assert limit.advance(59) == 2
    assert limit.advance(60) == 3
    assert limit.advance(120) == 4


def test_adaptive_limit_halves_on_saturation_and_requires_a_fresh_healthy_window():
    limit = batch_research.AdaptiveLimit(
        initial=2,
        maximum=8,
        stability_window=60,
        backoff_seconds=120,
        now=0,
    )
    assert limit.advance(300) == 7

    assert limit.on_saturation(301) == 3
    assert limit.can_launch(420) is False
    assert limit.can_launch(421) is True
    assert limit.advance(480) == 3
    assert limit.advance(481) == 4


def test_default_adaptive_clock_does_not_jump_straight_to_the_ceiling():
    limit = batch_research.AdaptiveLimit(
        initial=2,
        maximum=8,
        stability_window=60,
        backoff_seconds=120,
    )
    assert limit.advance(time.monotonic()) == 2


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 429: too many requests",
        "rate_limit_exceeded",
        "Too many active sessions for this account",
        "Provider capacity temporarily unavailable",
    ],
)
def test_only_failed_workers_with_capacity_signals_trigger_backoff(message):
    assert batch_research.is_saturation_failure(1, message) is True
    assert batch_research.is_saturation_failure(0, message) is False
    assert batch_research.is_saturation_failure(22, "evidence gate failed") is False


def test_prepare_jobs_isolates_slug_collisions_deduplicates_and_skips_completed(tmp_path):
    questions = [" Alpha / Beta? ", "Alpha Beta", "", "Alpha / Beta?"]

    jobs = batch_research.prepare_jobs(questions, tmp_path, managed=True)

    assert [job.question for job in jobs] == ["Alpha / Beta?", "Alpha Beta"]
    assert jobs[0].run_dir.name.startswith("alpha-beta-")
    assert jobs[1].run_dir.name.startswith("alpha-beta-")
    assert jobs[0].run_dir != jobs[1].run_dir
    assert jobs[0].log_path.parent == tmp_path / "_batch" / "logs"

    with pytest.raises(ValueError, match="choose one of"):
        batch_research.prepare_jobs(questions, tmp_path, managed=True)

    reordered = batch_research.prepare_jobs(
        ["Alpha Beta", "Alpha / Beta?"], tmp_path / "reordered", managed=True
    )
    original_slugs = {job.question: job.slug for job in jobs}
    reordered_slugs = {job.question: job.slug for job in reordered}
    assert reordered_slugs == original_slugs


def test_prepare_jobs_keeps_slugs_stable_when_a_later_batch_adds_a_collision(tmp_path):
    original = batch_research.prepare_jobs(["Alpha / Beta?"], tmp_path, managed=True)
    with pytest.raises(ValueError, match="choose one of"):
        batch_research.prepare_jobs(["Alpha / Beta?", "Alpha Beta"], tmp_path, managed=True)


def test_prepare_jobs_accepts_explicit_slug_tsv_for_resuming_named_runs(tmp_path):
    lines = [
        "wu-wei\tWhat is wu wei?",
        "shi-strategy\tWhat is shi?",
    ]

    jobs = batch_research.prepare_jobs(lines, tmp_path, managed=True)

    assert [job.slug for job in jobs] == ["wu-wei", "shi-strategy"]
    assert [job.question for job in jobs] == ["What is wu wei?", "What is shi?"]
    assert jobs[0].run_dir == tmp_path / "wu-wei"

    with pytest.raises(ValueError, match="choose one of"):
        batch_research.prepare_jobs(lines, tmp_path, managed=True)


@pytest.mark.parametrize("line", ["../escape\tQuestion", "/absolute\tQuestion", "bad slug\tQuestion"])
def test_prepare_jobs_rejects_unsafe_explicit_slugs(tmp_path, line):
    with pytest.raises(ValueError, match="invalid explicit slug"):
        batch_research.prepare_jobs([line], tmp_path)


def test_codex_invocation_is_workspace_scoped_and_scrubs_api_routing(tmp_path):
    job = batch_research.prepare_jobs(["Why adaptive scheduling?"], tmp_path, managed=True)[0]
    skill_root = tmp_path / "skill"
    skill_root.mkdir()

    invocation = batch_research.build_invocation("codex", job, skill_root)

    assert invocation.argv == [
        "codex",
        "exec",
        "-C",
        str(job.scratch_dir),
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "--skip-git-repo-check",
        "-",
    ]
    assert invocation.cwd == job.scratch_dir
    assert invocation.stdin_text is not None
    assert "QUESTION: Why adaptive scheduling?" in invocation.stdin_text
    assert str(skill_root) in invocation.stdin_text

    env = batch_research.child_environment(
        "codex",
        {
            "KEEP_ME": "yes",
            "OPENAI_API_KEY": "metered",
            "CODEX_API_KEY": "metered",
            "CODEX_ACCESS_TOKEN": "override",
            "OPENAI_BASE_URL": "https://gateway.invalid",
        },
    )
    assert env == {"KEEP_ME": "yes"}


def test_claude_requires_a_contained_runner_and_scrubs_metered_routing(tmp_path):
    job = batch_research.prepare_jobs(["How does containment work?"], tmp_path, managed=True)[0]
    skill_root = tmp_path / "skill"
    skill_root.mkdir()

    with pytest.raises(ValueError, match="contained runner"):
        batch_research.build_invocation("claude", job, skill_root)

    invocation = batch_research.build_invocation(
        "claude", job, skill_root, claude_runner=["contained-runner", "--quiet"]
    )
    assert invocation.argv[:4] == ["contained-runner", "--quiet", "claude", "-p"]
    prompt_index = invocation.argv.index("-p") + 1
    assert "QUESTION: How does containment work?" in invocation.argv[prompt_index]
    assert invocation.argv.index("--allowedTools") > prompt_index
    assert "Agent" in invocation.argv
    assert "WebFetch" not in invocation.argv
    assert invocation.cwd == job.scratch_dir
    assert invocation.stdin_text is None

    env = batch_research.child_environment(
        "claude",
        {
            "KEEP_ME": "yes",
            "ANTHROPIC_API_KEY": "metered",
            "ANTHROPIC_AUTH_TOKEN": "gateway",
            "ANTHROPIC_BASE_URL": "https://gateway.invalid",
        },
        skill_root=skill_root,
        run_dir=job.run_dir,
    )
    assert env == {
        "KEEP_ME": "yes",
        "DEEPER_RESEARCH_SKILL_ROOT": str(skill_root),
        "DEEPER_RESEARCH_RUN_DIR": str(job.run_dir),
    }


def test_run_batch_ramps_real_workers_and_keeps_outputs_isolated(tmp_path):
    jobs = batch_research.prepare_jobs(
        ["Question one", "Question two", "Question three", "Question four"],
        tmp_path / "research",
        managed=True,
    )
    skill_root = tmp_path / "skill"
    skill_root.mkdir()

    def worker_invocation(_adapter, job, _skill_root, _claude_runner=None):
        return batch_research.Invocation(
            [
                sys.executable,
                "-c",
                (
                    "import pathlib,time; time.sleep(0.12); "
                    "pathlib.Path('RESEARCH-BIBLE.md').write_text('done')"
                ),
            ],
            job.run_dir,
            None,
        )

    result = batch_research.run_batch(
        jobs,
        adapter="codex",
        skill_root=skill_root,
        limit=batch_research.AdaptiveLimit(
            initial=1,
            maximum=3,
            stability_window=0.03,
            backoff_seconds=0.05,
        ),
        poll_interval=0.01,
        invocation_builder=worker_invocation,
        source_env={},
        completion_validator=lambda _run: SimpleNamespace(ok=True),
    )

    assert result.succeeded == 4
    assert result.failed == 0
    assert result.peak_running == 3
    assert all((job.run_dir / "RESEARCH-BIBLE.md").read_text() == "done" for job in jobs)


def test_run_batch_backs_off_after_capacity_failure_without_killing_active_work(tmp_path):
    jobs = batch_research.prepare_jobs(
        ["Saturate", "Already active", "Wait for cooldown"],
        tmp_path / "research",
        managed=True,
    )
    skill_root = tmp_path / "skill"
    skill_root.mkdir()

    def worker_invocation(_adapter, job, _skill_root, _claude_runner=None):
        if job.question == "Saturate":
            code = "import time; time.sleep(0.03); print('HTTP 429: too many requests'); raise SystemExit(1)"
        elif job.question == "Already active":
            code = (
                "import pathlib,time; time.sleep(0.08); "
                "pathlib.Path('RESEARCH-BIBLE.md').write_text('active survived')"
            )
        else:
            code = "import pathlib; pathlib.Path('RESEARCH-BIBLE.md').write_text('after cooldown')"
        return batch_research.Invocation([sys.executable, "-c", code], job.run_dir, None)

    started = time.monotonic()
    result = batch_research.run_batch(
        jobs,
        adapter="codex",
        skill_root=skill_root,
        limit=batch_research.AdaptiveLimit(
            initial=2,
            maximum=4,
            stability_window=1,
            backoff_seconds=0.12,
        ),
        poll_interval=0.01,
        invocation_builder=worker_invocation,
        source_env={},
        completion_validator=lambda _run: SimpleNamespace(ok=True),
    )

    assert result.saturation_events == 1
    assert result.succeeded == 2
    assert result.failed == 1
    assert result.final_target == 1
    assert time.monotonic() - started >= 0.12
    assert (jobs[1].run_dir / "RESEARCH-BIBLE.md").read_text() == "active survived"


def test_run_batch_does_not_reuse_stale_capacity_signals_from_an_old_log(tmp_path):
    job = batch_research.prepare_jobs(["Ordinary failure"], tmp_path / "research")[0]
    job.log_path.parent.mkdir(parents=True)
    job.log_path.write_text("old attempt: HTTP 429 too many requests\n")
    skill_root = tmp_path / "skill"
    skill_root.mkdir()

    def worker_invocation(_adapter, current_job, _skill_root, _claude_runner=None):
        return batch_research.Invocation(
            [sys.executable, "-c", "print('evidence gate failed'); raise SystemExit(22)"],
            current_job.run_dir,
            None,
        )

    result = batch_research.run_batch(
        [job],
        adapter="codex",
        skill_root=skill_root,
        limit=batch_research.AdaptiveLimit(
            initial=1,
            maximum=2,
            stability_window=1,
            backoff_seconds=0.05,
        ),
        poll_interval=0.01,
        invocation_builder=worker_invocation,
        source_env={},
    )

    assert result.failed == 1
    assert result.saturation_events == 0


def test_cli_dry_run_reports_default_policy_without_launching_workers(tmp_path, capsys):
    questions = tmp_path / "questions.txt"
    questions.write_text("Question one\nQuestion two\n")
    skill_root = tmp_path / "skill"
    skill_root.mkdir()

    returncode = batch_research.main(
        [
            str(questions),
            "--adapter",
            "codex",
            "--skill-root",
            str(skill_root),
            "--output-root",
            str(tmp_path / "research"),
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert returncode == 0
    assert "adaptive concurrency: initial=2 maximum=8" in output
    assert output.count("codex exec") == 2
    assert not list(tmp_path.rglob("RESEARCH-BIBLE.md"))


def test_cli_defaults_output_root_to_project_launch_directory(
    tmp_path, monkeypatch, capsys
):
    project_launch_dir = tmp_path / "project-research"
    project_launch_dir.mkdir()
    questions = tmp_path / "questions.txt"
    questions.write_text("Question one\n")
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    monkeypatch.chdir(project_launch_dir)

    returncode = batch_research.main(
        [
            str(questions),
            "--adapter",
            "codex",
            "--skill-root",
            str(skill_root),
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert returncode == 0
    assert str(project_launch_dir) in output
    assert not (project_launch_dir / "_batch").exists()
    assert not (project_launch_dir / "Deeper_Research").exists()


def test_batch_defaults_to_project_research_and_dry_run_writes_nothing(tmp_path):
    jobs = batch_research.prepare_jobs(
        ["Question"], project_dir=tmp_path, dry_run=True
    )
    assert jobs[0].run_dir.parent == tmp_path / "Deeper_Research"
    assert list(tmp_path.iterdir()) == []


def test_per_row_mode_overrides_global_mode(tmp_path, monkeypatch):
    seen = []

    def fake_prepare(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            action="plan-extend", run_dir=tmp_path / "research" / kwargs["slug"],
            choices=(), broker_endpoint=None, lease_token=None, scratch_dir=None,
        )

    monkeypatch.setattr(batch_research, "prepare_run", fake_prepare)
    jobs = batch_research.prepare_jobs(
        ["topic\textend\tNew question"], project_dir=tmp_path, mode="fresh", dry_run=True, managed=True
    )
    assert jobs[0].mode == "extend"
    assert seen[0]["mode"] == "extend"


def test_load_env_file_populates_only_unset_keys(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "export EXA_API_KEY=exa-123\n"
        'ANTHROPIC_API_KEY="anthropic-xyz"\n'
        "ALREADY_SET=from-file\n"
        "MALFORMED_LINE\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ALREADY_SET", "from-env")

    loaded = batch_research.load_env_file(env)

    import os
    assert os.environ["EXA_API_KEY"] == "exa-123"           # export prefix stripped
    assert os.environ["ANTHROPIC_API_KEY"] == "anthropic-xyz"  # quotes stripped
    assert os.environ["ALREADY_SET"] == "from-env"          # pre-set value not overwritten
    assert set(loaded) == {"EXA_API_KEY", "ANTHROPIC_API_KEY"}
    assert "MALFORMED_LINE" not in os.environ


def test_recover_stranded_bible_pulls_completed_run_from_scratch(tmp_path):
    library = tmp_path / "Deeper_Research"
    run_dir = library / "my-topic"
    run_dir.mkdir(parents=True)
    (run_dir / "scope.json").write_text("{}", encoding="utf-8")  # partial run dir
    # a completed workrun stranded in transaction scratch (broker died before publish)
    workrun = library / ".transactions" / "session-abc" / "scratch" / "workrun"
    (workrun / "sections").mkdir(parents=True)
    (workrun / "RESEARCH-BIBLE_my-topic.md").write_text("# Bible\n", encoding="utf-8")
    (workrun / "sections" / "01.md").write_text("body", encoding="utf-8")
    # an index_library copy that must NOT be preferred over the primary workrun
    idx = library / ".transactions" / "session-abc" / "scratch" / "index_library" / "my-topic"
    idx.mkdir(parents=True)
    (idx / "RESEARCH-BIBLE_my-topic.md").write_text("# stale copy\n", encoding="utf-8")

    job = SimpleNamespace(run_dir=run_dir, slug="my-topic")
    recovered = batch_research.recover_stranded_bible(job, emit=lambda *_: None)

    assert recovered is True
    assert (run_dir / "RESEARCH-BIBLE_my-topic.md").read_text(encoding="utf-8") == "# Bible\n"
    assert (run_dir / "sections" / "01.md").is_file()


def test_prepare_jobs_unmanaged_is_the_default_and_uses_no_broker(tmp_path):
    jobs = batch_research.prepare_jobs(["Why unmanaged?"], tmp_path)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.managed is False
    assert job.run_dir == tmp_path / job.slug
    assert job.run_dir.is_dir()                 # created, ready for --run-dir writes
    assert job.broker_endpoint is None and job.lease_token is None and job.scratch_dir is None


def test_prepare_jobs_unmanaged_skips_a_completed_run(tmp_path):
    run_dir = tmp_path / "done-slug"
    run_dir.mkdir()
    (run_dir / "RESEARCH-BIBLE_done-slug.md").write_text("x" * 3000, encoding="utf-8")
    jobs = batch_research.prepare_jobs(["done-slug\tAlready finished?"], tmp_path)
    assert jobs == []                            # resumable: finished runs are skipped


def test_unmanaged_invocation_targets_run_dir_with_an_unmanaged_prompt(tmp_path):
    job = batch_research.prepare_jobs(["Ship it?"], tmp_path)[0]
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    inv = batch_research.build_invocation("codex", job, skill_root)
    assert inv.argv[:4] == ["codex", "exec", "-C", str(job.run_dir)]   # run dir, not scratch
    assert inv.cwd == job.run_dir
    assert "UNMANAGED" in inv.stdin_text and "no run_manager" in inv.stdin_text
    assert "publish every artifact" not in inv.stdin_text and "read-only" not in inv.stdin_text
    assert str(job.run_dir) in inv.stdin_text
