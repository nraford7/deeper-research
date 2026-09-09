#!/usr/bin/env python3
"""Adaptive headless scheduler for independent deeper-research questions."""

from collections import deque
import argparse
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time

from scripts.run_layout import LEGACY_LIBRARY_DIRNAMES, LIBRARY_DIRNAME
from scripts.run_manager import ManagerError, prepare_run
from scripts.run_state import validate_completion


_SATURATION_RE = re.compile(
    r"\b429\b|rate[ _-]?limit|too many requests|too many (?:active|concurrent)|"
    r"concurren(?:cy|t sessions?)|provider capacity",
    re.IGNORECASE,
)
_EXPLICIT_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")


def load_env_file(path=None, keys=None):
    """Populate os.environ from a KEY=VALUE .env file for keys not already set.

    ~/.env commonly assigns keys WITHOUT `export`, so a plain `. ~/.env` leaves them as
    shell variables that this process — and the broker/worker children that inherit its
    environment — never see. That was the root cause of the 2026-09 batch failures: both
    Round-1 retrieval and the managed publish helper saw an empty EXA_API_KEY. Loading the
    file straight into os.environ here makes every child inherit the keys. Returns the list
    of names newly set.
    """
    path = Path(path) if path else Path.home() / ".env"
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if keys is not None and name not in keys:
            continue
        if name and name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded


def recover_stranded_bible(job, emit=print):
    """Recover a completed-but-unpublished Bible from transaction scratch into the run dir.

    When the managed broker/lease dies at publish time (2026-09 failures), the pipeline has
    already produced a full Bible under <library>/.transactions/<session>/scratch/.../ but
    validate_completion(run_dir) fails because nothing was sealed into the run dir. Rather
    than score a finished run 'failed' and strand the work, copy the completed workrun into
    the run dir. Returns True if a Bible markdown now exists in the run dir.
    """
    import shutil

    run_dir = Path(job.run_dir)
    slug = job.slug
    tx = run_dir.parent / ".transactions"
    if not tx.is_dir():
        return False
    hits = list(tx.glob(f"**/RESEARCH-BIBLE_{slug}.md"))
    if not hits:
        return False
    # prefer a primary workrun over an index_library copy, then the most complete tree
    hits.sort(key=lambda p: ("index_library" in p.parts, -len(p.parts)))
    src = hits[0].parent
    for item in src.iterdir():
        dest = run_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        except OSError as exc:
            emit(f"[batch] recover warning ({slug}): {exc}")
    return (run_dir / f"RESEARCH-BIBLE_{slug}.md").is_file()


def unmanaged_complete(run_dir, slug):
    """A finished unmanaged run has a non-trivial Bible written straight into the run dir.

    Unmanaged runs have no broker/metadata to validate against (that is the whole point),
    so completion is simply: the Bible markdown exists and is non-trivial. The HTML is a
    nice-to-have the exporter adds; its absence does not fail the run.
    """
    md = Path(run_dir) / f"RESEARCH-BIBLE_{slug}.md"
    try:
        return md.is_file() and md.stat().st_size > 2000
    except OSError:
        return False


def is_saturation_failure(returncode, log_text):
    """Return true only when a failed worker reports shared-capacity pressure."""

    return returncode != 0 and _SATURATION_RE.search(log_text) is not None


@dataclass(frozen=True)
class BatchJob:
    question: str
    slug: str
    run_dir: Path
    log_path: Path
    mode: str | None = None
    broker_endpoint: str | None = None
    lease_token: str | None = None
    scratch_dir: Path | None = None
    action: str = "planned"
    managed: bool = False


@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    cwd: Path
    stdin_text: str | None


@dataclass(frozen=True)
class BatchResult:
    total: int
    succeeded: int
    failed: int
    peak_running: int
    saturation_events: int
    final_target: int
    recovered: int = 0


@dataclass
class _RunningJob:
    job: BatchJob
    process: subprocess.Popen
    log_handle: object


def _question_slug(question):
    readable = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:35]
    readable = readable or "question"
    digest = hashlib.sha256(question.encode()).hexdigest()[:12]
    return f"{readable}-{digest}"


def prepare_jobs(
    questions,
    output_root=None,
    *,
    project_dir=None,
    library_dir=None,
    mode=None,
    dry_run=False,
    managed=False,
):
    """Plan all jobs before creating batch artifacts or launching workers.

    A line may be plain question text (stable hashed slug) or
    ``explicit-slug<TAB>question`` for resuming an existing named run directory.
    """

    if output_root is not None and library_dir is not None:
        raise ValueError("output_root and library_dir are aliases; pass only one")
    direct_library = library_dir if library_dir is not None else output_root
    captured_project = Path(project_dir or Path.cwd()).resolve() if direct_library is None else None
    _lib_names = {LIBRARY_DIRNAME.casefold(), *(n.casefold() for n in LEGACY_LIBRARY_DIRNAMES)}
    library = Path(direct_library).resolve() if direct_library is not None else (
        captured_project if captured_project.name.casefold() in _lib_names else captured_project / LIBRARY_DIRNAME
    )
    logs_dir = library / "_batch" / "logs"
    seen_questions = set()
    seen_slugs = {}
    unique_jobs = []
    for raw in questions:
        line = raw.strip()
        if not line:
            continue
        row_mode = mode
        fields = [part.strip() for part in line.split("\t")]
        if len(fields) == 3:
            explicit_slug, row_mode, question = fields
            if row_mode not in {"resume", "extend", "fresh", "cancel"}:
                raise ValueError(f"invalid collision mode: {row_mode!r}")
        elif len(fields) == 2:
            explicit_slug, question = fields
            if not _EXPLICIT_SLUG_RE.fullmatch(explicit_slug):
                raise ValueError(f"invalid explicit slug: {explicit_slug!r}")
            slug = explicit_slug
        elif len(fields) == 1:
            question = line
            slug = _question_slug(question)
        else:
            raise ValueError("batch rows must be question, slug<TAB>question, or slug<TAB>mode<TAB>question")
        if len(fields) == 3:
            if not _EXPLICIT_SLUG_RE.fullmatch(explicit_slug):
                raise ValueError(f"invalid explicit slug: {explicit_slug!r}")
            slug = explicit_slug
        if not question or question in seen_questions:
            continue
        prior_question = seen_slugs.get(slug)
        if prior_question is not None and prior_question != question:
            raise ValueError(
                f"explicit slug collision: {slug!r} names multiple questions")
        seen_questions.add(question)
        seen_slugs[slug] = question
        unique_jobs.append((slug, row_mode, question))
    jobs = []
    for slug, row_mode, question in unique_jobs:
        if not managed:
            # Unmanaged (default): worker writes straight into <library>/<slug>. No broker,
            # no lease, no scratch — the failure class those introduced (see SKILL.md). Skip
            # a run whose Bible already exists (resumable); `fresh` clears and re-runs.
            run_dir = library / slug
            bible = run_dir / f"RESEARCH-BIBLE_{slug}.md"
            if row_mode == "fresh" and run_dir.exists() and not dry_run:
                shutil.rmtree(run_dir)
            elif unmanaged_complete(run_dir, slug):
                continue  # already complete
            if not dry_run:
                run_dir.mkdir(parents=True, exist_ok=True)
            jobs.append(BatchJob(
                question, slug, run_dir, logs_dir / f"{slug}.log", row_mode,
                None, None, None, "planned", managed=False,
            ))
            continue
        try:
            prepared = prepare_run(
                question=question,
                slug=slug,
                project_dir=captured_project,
                library_dir=direct_library,
                mode=row_mode,
                dry_run=dry_run,
            )
        except ManagerError as exc:
            raise ValueError(str(exc)) from exc
        if prepared.action == "mode-required":
            raise ValueError(
                f"run {slug!r} already exists; choose one of: {', '.join(prepared.choices)}"
            )
        if prepared.action in {"cancelled", "complete-noop"} or prepared.run_dir is None:
            continue
        jobs.append(BatchJob(
            question,
            slug,
            prepared.run_dir,
            logs_dir / f"{slug}.log",
            row_mode,
            prepared.broker_endpoint,
            prepared.lease_token,
            prepared.scratch_dir,
            prepared.action,
            managed=True,
        ))
    return jobs


def _pipeline_prompt(adapter, job, skill_root):
    native = "Codex native subagents" if adapter == "codex" else "Claude native Agent subagents"
    if not job.managed:
        # Unmanaged (default): write straight into the run dir; no run_manager/broker/lease.
        return (
            f"Use the deeper-research skill at {skill_root} to run the FULL pipeline UNMANAGED "
            f"(no run_manager, no broker, no lease) for this question, writing ALL outputs directly "
            f"into {job.run_dir}. Invoke scripts as: PYTHONPATH={skill_root} python3 "
            f"{skill_root}/scripts/<name>.py --run-dir {job.run_dir} ...  Never write under {skill_root}. "
            f"Follow the benchmark-quickstart / legacy direct-script flow: scope.py -> slice_search.py -> "
            f"evidence_gate.py (must exit 0) -> fetch_fulltext.py -> citation_chase.py -> coverage_audit.py "
            f"-> re-run fetch_fulltext.py -> Round-2 synthesis subagent emitting the SIX EXACT headers to "
            f"{job.run_dir}/round2/synthesis.md -> deepen_questions.py -> Round-3 integration (section planner "
            f"+ isolated section subagents into {job.run_dir}/sections/ + dedup_bib.py) -> Round-4 "
            f"verify_citations.py --check-urls, lint_background.py, sweep_numbers.py, then a REFUTE adversary "
            f"on a provider whose family is INDEPENDENT from the synthesis family -> fix pass -> assemble "
            f"{job.run_dir}/RESEARCH-BIBLE_{job.slug}.md from the sections + bibliography and build its .html "
            f"with export.py standalone flags (--sections {job.run_dir}/sections --bibliography "
            f"{job.run_dir}/sections/bibliography.md --output-dir {job.run_dir}). Use {native} for coverage, "
            f"integration, and fixes. Headless: skip Stage-0 framing. Do not stop until "
            f"{job.run_dir}/RESEARCH-BIBLE_{job.slug}.md exists. QUESTION: {job.question}"
        )
    return (
        f"Use the deeper-research skill rooted at {skill_root} to run the FULL pipeline for "
        f"this question. Invoke every helper and dispatcher by its absolute path under "
        f"{skill_root}; keep all outputs under the writable run directory {job.run_dir} and "
        f"never write under {skill_root}. The managed run {job.run_dir} is read-only; write "
        f"working files only in {job.scratch_dir}, then publish every artifact through the "
        f"run manager broker at {job.broker_endpoint}. Use {native} for coverage, integration, and fixes. "
        "For Round 2, honor an explicit [defaults].synthesis executor; otherwise use a native "
        "strongest-available synthesis subagent. Use the strongest available role for "
        "integration and balanced/cheaper roles for bounded work. Headless: skip Stage-0 "
        f"framing. QUESTION: {job.question}"
    )


def build_invocation(adapter, job, skill_root, claude_runner=None):
    skill_root = Path(skill_root).resolve()
    prompt = _pipeline_prompt(adapter, job, skill_root)
    if adapter == "codex":
        return Invocation(
            [
                "codex", "exec", "-C", str(job.scratch_dir or job.run_dir), "--ephemeral",
                "--sandbox", "workspace-write",
                "-c", "sandbox_workspace_write.network_access=true",
                "--skip-git-repo-check", "-",
            ],
            job.scratch_dir or job.run_dir,
            prompt,
        )
    if adapter == "claude":
        if not claude_runner:
            raise ValueError("Claude batches require a pre-approved contained runner")
        allowed_tools = [
            "Agent",
            "Read",
            "Edit",
            "Write",
            "WebSearch",
            f"Bash(python3 {skill_root}/dispatch.py:*)",
            f"Bash(python3 {skill_root}/scripts/*:*)",
            "Bash(curl:*)",
        ]
        return Invocation(
            [*claude_runner, "claude", "-p", prompt, "--allowedTools", *allowed_tools],
            job.scratch_dir or job.run_dir,
            None,
        )
    raise ValueError(f"unsupported adapter: {adapter}")


def child_environment(adapter, source, skill_root=None, run_dir=None):
    env = dict(source)
    if adapter == "codex":
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
            env.pop(name, None)
    elif adapter == "claude":
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            env.pop(name, None)
        if skill_root is not None:
            env["DEEPER_RESEARCH_SKILL_ROOT"] = str(Path(skill_root).resolve())
        if run_dir is not None:
            env["DEEPER_RESEARCH_RUN_DIR"] = str(Path(run_dir).resolve())
    return env


@dataclass
class AdaptiveLimit:
    """Add one healthy slot per window, up to a caller-supplied ceiling."""

    initial: int = 2
    maximum: int = 8
    stability_window: float = 60.0
    backoff_seconds: float = 120.0
    now: float | None = None
    target: int = field(init=False)
    next_increase_at: float = field(init=False)
    blocked_until: float = field(init=False)

    def __post_init__(self):
        if not 1 <= self.initial <= self.maximum:
            raise ValueError("initial concurrency must be between 1 and maximum")
        if self.stability_window <= 0 or self.backoff_seconds < 0:
            raise ValueError("timing values must be positive")
        started_at = time.monotonic() if self.now is None else self.now
        self.target = self.initial
        self.next_increase_at = started_at + self.stability_window
        self.blocked_until = started_at

    def advance(self, now):
        if now < self.blocked_until:
            return self.target
        while self.target < self.maximum and now >= self.next_increase_at:
            self.target += 1
            self.next_increase_at += self.stability_window
        return self.target

    def on_saturation(self, now):
        self.target = max(1, self.target // 2)
        self.blocked_until = now + self.backoff_seconds
        self.next_increase_at = self.blocked_until + self.stability_window
        return self.target

    def can_launch(self, now):
        return now >= self.blocked_until


def run_batch(
    jobs,
    *,
    adapter,
    skill_root,
    limit,
    claude_runner=None,
    poll_interval=5.0,
    invocation_builder=build_invocation,
    source_env=None,
    emit=print,
    completion_validator=validate_completion,
):
    """Run pending jobs without killing active work when the adaptive target falls."""

    pending = deque(jobs)
    running = []
    succeeded = failed = saturation_events = peak_running = recovered = 0
    source_env = os.environ if source_env is None else source_env

    while pending or running:
        now = time.monotonic()
        limit.advance(now)

        for active in list(running):
            returncode = active.process.poll()
            if returncode is None:
                continue
            active.log_handle.close()
            running.remove(active)
            log_text = active.job.log_path.read_text(encoding="utf-8", errors="replace")
            if active.job.managed:
                try:
                    valid = completion_validator(active.job.run_dir).ok
                except Exception:
                    valid = False
            else:
                valid = unmanaged_complete(active.job.run_dir, active.job.slug)
            completed = returncode == 0 and valid
            if active.job.managed and returncode == 0 and not valid:
                # Managed worker exited clean but nothing was sealed into the run dir — most
                # often a dead broker/lease at publish time. Recover the finished Bible from
                # transaction scratch instead of scoring a completed run 'failed'.
                try:
                    if recover_stranded_bible(active.job, emit):
                        recovered += 1
                        completed = True
                        emit(
                            f"[batch] recovered {active.job.slug} from scratch "
                            f"(publish/broker failed; Bible copied into run dir, run left unsealed)"
                        )
                except Exception as exc:
                    emit(f"[batch] recovery attempt failed for {active.job.slug}: {exc}")
            if completed:
                succeeded += 1
                emit(f"[batch] completed {active.job.slug}")
            else:
                failed += 1
                emit(f"[batch] failed {active.job.slug} (exit {returncode})")
                if is_saturation_failure(returncode, log_text):
                    saturation_events += 1
                    new_target = limit.on_saturation(now)
                    emit(f"[batch] capacity pressure; target reduced to {new_target}")

        while pending and limit.can_launch(now) and len(running) < limit.target:
            job = pending.popleft()
            invocation = invocation_builder(adapter, job, skill_root, claude_runner)
            job.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = job.log_path.open("w", encoding="utf-8")
            try:
                process = subprocess.Popen(
                    invocation.argv,
                    cwd=invocation.cwd,
                    stdin=subprocess.PIPE if invocation.stdin_text is not None else subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=child_environment(
                        adapter,
                        source_env,
                        skill_root=skill_root,
                        run_dir=job.run_dir,
                    ),
                )
            except OSError as exc:
                log_handle.write(f"launch failed: {exc}\n")
                log_handle.close()
                failed += 1
                emit(f"[batch] failed to launch {job.slug}: {exc}")
                continue
            if invocation.stdin_text is not None:
                process.stdin.write(invocation.stdin_text)
                process.stdin.close()
            running.append(_RunningJob(job, process, log_handle))
            peak_running = max(peak_running, len(running))
            emit(f"[batch] started {job.slug} ({len(running)}/{limit.target})")

        if pending or running:
            time.sleep(poll_interval)

    return BatchResult(
        total=len(jobs),
        succeeded=succeeded,
        failed=failed,
        peak_running=peak_running,
        saturation_events=saturation_events,
        final_target=limit.target,
        recovered=recovered,
    )


def _default_skill_root(adapter):
    override = os.environ.get("DEEPER_RESEARCH_SKILL_ROOT")
    if override:
        return Path(override)
    runtime_dir = ".agents" if adapter == "codex" else ".claude"
    return Path.home() / runtime_dir / "skills" / "deeper-research"


def _parser():
    parser = argparse.ArgumentParser(
        description="Run independent deeper-research questions with adaptive concurrency."
    )
    parser.add_argument(
        "questions_file", type=Path,
        help="UTF-8 file with question or explicit-slug<TAB>question per line",
    )
    parser.add_argument("--adapter", choices=("codex", "claude"), required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help=(
            "legacy alias for a direct research library"
        ),
    )
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(),
                        help="project receiving research/<slug> (default: captured launch directory)")
    parser.add_argument("--library-dir", type=Path, help="direct research library")
    parser.add_argument("--mode", choices=("resume", "extend", "fresh", "cancel"))
    parser.add_argument("--new-question", help="override the question for a single extension row")
    parser.add_argument("--skill-root", type=Path)
    parser.add_argument("--initial-concurrency", type=int, default=2)
    parser.add_argument("--max-concurrency", type=int, default=8)
    parser.add_argument("--stability-window", type=float, default=60.0)
    parser.add_argument("--backoff-seconds", type=float, default=120.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--claude-runner",
        help="Pre-approved containment wrapper command; required for the Claude adapter",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--managed",
        action="store_true",
        help="Use the run-manager broker/lease pathway (atomic seal + isolation for a "
        "shared service). OFF by default: local runs write straight to <library>/<slug> "
        "with no broker/lease, avoiding the broker's failure class.",
    )
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.questions_file.is_file():
        parser.error(f"questions file not found: {args.questions_file}")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    skill_root = (args.skill_root or _default_skill_root(args.adapter)).resolve()
    if not skill_root.is_dir():
        parser.error(f"skill root not found: {skill_root}")
    claude_runner = shlex.split(args.claude_runner) if args.claude_runner else None
    if args.adapter == "claude" and not claude_runner:
        parser.error("--claude-runner is required for the Claude adapter")
    try:
        limit = AdaptiveLimit(
            initial=args.initial_concurrency,
            maximum=args.max_concurrency,
            stability_window=args.stability_window,
            backoff_seconds=args.backoff_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))

    questions = args.questions_file.read_text(encoding="utf-8").splitlines()
    try:
        if args.output_root and args.library_dir:
            parser.error("--output-root and --library-dir are aliases; pass only one")
        if args.new_question:
            if len([line for line in questions if line.strip()]) != 1:
                parser.error("--new-question requires exactly one input row")
            first = next(line for line in questions if line.strip())
            slug = first.split("\t", 1)[0] if "\t" in first else _question_slug(first)
            questions = [f"{slug}\t{args.mode or 'extend'}\t{args.new_question}"]
        jobs = prepare_jobs(
            questions,
            output_root=args.output_root,
            project_dir=args.project_dir,
            library_dir=args.library_dir,
            mode=args.mode,
            dry_run=args.dry_run,
            managed=args.managed,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(
        f"adaptive concurrency: initial={limit.initial} maximum={limit.maximum} "
        f"window={limit.stability_window:g}s backoff={limit.backoff_seconds:g}s"
    )
    print(f"pending questions: {len(jobs)}")
    if args.dry_run:
        for job in jobs:
            invocation = build_invocation(args.adapter, job, skill_root, claude_runner)
            print(f"{job.slug}: {shlex.join(invocation.argv)}")
        return 0
    if not jobs:
        return 0

    # Env preflight — the batch and its broker/worker children inherit THIS process's
    # environment. ~/.env keys assigned without `export` never reach here via `. ~/.env`,
    # which silently starved retrieval and the publish helper of EXA_API_KEY (2026-09
    # failures). Load ~/.env directly, then fail loudly if the retrieval key is still absent.
    if not os.environ.get("EXA_API_KEY"):
        loaded = load_env_file()
        if loaded:
            print(f"[batch] loaded {len(loaded)} key(s) from ~/.env into the environment")
    if not os.environ.get("EXA_API_KEY"):
        parser.error(
            "EXA_API_KEY is not set in this process's environment — retrieval and the publish "
            "helper will fail. Export it before launching:  set -a; . ~/.env; set +a"
        )
    provider_keys = [
        k for k in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY")
        if os.environ.get(k)
    ]
    if len(provider_keys) < 2:
        print(
            "[batch] WARNING: fewer than two LLM provider keys are set; the Round-4 "
            "independent-family adversary may be unavailable (fail-closed)."
        )

    result = run_batch(
        jobs,
        adapter=args.adapter,
        skill_root=skill_root,
        limit=limit,
        claude_runner=claude_runner,
        poll_interval=args.poll_interval,
    )
    print(
        f"batch complete: succeeded={result.succeeded} failed={result.failed} "
        f"recovered={result.recovered} peak={result.peak_running} final_target={result.final_target}"
    )
    if result.recovered:
        print(
            f"[batch] NOTE: {result.recovered} run(s) completed but their publish step failed; "
            "the Bible was recovered from scratch into the run dir. The managed run is unsealed "
            "(metadata not finalized) — re-run finalize if you need a sealed run."
        )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
