"""Public lifecycle CLI and the sole concrete dispatcher for managed run mutations."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from enum import IntEnum
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_fs import RootedFS
from scripts.run_layout import (
    FilesystemCapabilities,
    LayoutError,
    LayoutKind,
    RunLayout,
    capabilities_for_dry_run,
    filesystem_safe_timestamp,
    portable_collision_key,
    probe_filesystem,
    resolve_project_root,
    safe_relpath,
    slugify_v1,
)
from scripts.run_state import (
    LifecycleError,
    ResumePlan,
    RunMetadata,
    make_state_guard,
    record_stage,
    resume_plan,
    seal_run,
    transition_status,
    validate_legacy_completion,
    validate_seal,
)
from scripts.run_transactions import (
    BrokerProtocolError,
    LeaseConflict,
    LocalBrokerClient,
    BrokerStop,
    RecoveryOutcome,
    RunLease,
    TransactionConflict,
    broker_request,
    create_skeleton_transaction,
    recover_creation,
    start_local_broker,
)


class Exit(IntEnum):
    OK = 0
    INVALID_LAYOUT = 10
    MODE_REQUIRED = 11
    COLLISION = 12
    UNSAFE_INHERITANCE = 13
    LOCKED = 14
    FROZEN_PARENT = 15
    TRANSACTION_REQUIRED = 16
    INVALID_ARGUMENT = 17
    FAILED = 18


class ManagerError(RuntimeError):
    def __init__(self, exit_code: Exit, message: str, *, details: Mapping[str, Any] | None = None):
        self.exit_code = exit_code
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True)
class PrepareResult:
    action: str
    run_dir: Path | None
    choices: tuple[str, ...] = ()
    resume_plan: ResumePlan | None = None
    lease_id: str | None = None
    lease_token: str | None = None
    lease_keeper_pid: int | None = None
    renewed_until: str | None = None
    broker_endpoint: str | None = None
    scratch_dir: Path | None = None
    project_dir: Path | None = None
    library_dir: Path | None = None
    slug: str | None = None
    classification: str | None = None
    layout_version: int | None = None
    schema_version: int | None = None
    write_probe_pending: bool = False
    transaction_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "run_dir": str(self.run_dir) if self.run_dir is not None else None,
            "choices": list(self.choices),
            "resume_plan": asdict(self.resume_plan) if self.resume_plan is not None else None,
            "lease_id": self.lease_id,
            "lease_token": self.lease_token,
            "lease_keeper_pid": self.lease_keeper_pid,
            "renewed_until": self.renewed_until,
            "broker_endpoint": self.broker_endpoint,
            "scratch_dir": str(self.scratch_dir) if self.scratch_dir is not None else None,
            "project_dir": str(self.project_dir) if self.project_dir is not None else None,
            "library_dir": str(self.library_dir) if self.library_dir is not None else None,
            "slug": self.slug,
            "classification": self.classification,
            "layout_version": self.layout_version,
            "schema_version": self.schema_version,
            "write_probe_pending": self.write_probe_pending,
            "transaction_id": self.transaction_id,
        }


# Task 5 fills this table with helper-specific managed entry points.  Values are
# (module, function, exact typed argument names).
MANAGED_HELPERS: dict[str, tuple[str, str, frozenset[str]]] = {
    "scope": ("scripts.scope", "managed_scope", frozenset({"topic", "scope", "use_llm"})),
    "slice-search": ("scripts.managed_helpers", "managed_slice", frozenset({"topic", "resume", "max_retrieval_usd", "fresh_since", "only_slice", "query", "add_slice"})),
    "agent-scout": ("scripts.managed_helpers", "managed_agent_scout", frozenset({"topic", "scope", "max_agent_usd", "max_seconds", "start_only"})),
    "fetch-fulltext": ("scripts.managed_helpers", "managed_fetch_fulltext", frozenset({"min_chars", "max_bytes", "max_chars", "timeout"})),
    "citation-chase": ("scripts.managed_helpers", "managed_citation_chase", frozenset({"topic", "max_seeds", "max_candidates", "forward", "openalex_call_ceiling"})),
    "coverage-audit": ("scripts.managed_helpers", "managed_coverage_audit", frozenset({"topic", "max_audit_rounds", "audit_usd", "use_matrix", "current_year"})),
    "deepen-questions": ("scripts.managed_helpers", "managed_deepen", frozenset({"round2_file", "single_question", "bucket", "max_retrieval_usd"})),
    "ingest-local": ("scripts.managed_helpers", "managed_ingest_local", frozenset({"files", "title", "slug", "year"})),
}


def _helper_schemas() -> dict[str, frozenset[str]]:
    return {helper: definition[2] for helper, definition in MANAGED_HELPERS.items()}


def _collision_paths(library: Path, slug: str) -> tuple[Path, ...]:
    if not library.is_dir():
        return ()
    key = portable_collision_key(slug)
    return tuple(
        sorted(
            (entry for entry in library.iterdir() if not entry.name.startswith(".") and portable_collision_key(entry.name) == key),
            key=lambda path: path.name.casefold(),
        )
    )


@dataclass(frozen=True)
class _Collision:
    path: Path
    classification: str
    layout: RunLayout | None
    choices: tuple[str, ...]
    reason: str | None = None


def _classify_collision(paths: tuple[Path, ...]) -> _Collision:
    if len(paths) != 1:
        return _Collision(paths[0], "ambiguous", None, ("fresh", "cancel"), "multiple portable aliases exist")
    path = paths[0]
    try:
        layout = RunLayout.open(path)
    except LayoutError as exc:
        return _Collision(path, exc.state, None, ("fresh", "cancel"), exc.detail)
    if layout.kind is LayoutKind.V2:
        metadata = RunMetadata.load(layout)
        if metadata.frozen_for_derivation or metadata.status == "frozen":
            return _Collision(path, "frozen", layout, ("extend", "fresh", "cancel"))
        return _Collision(path, metadata.status, layout, ("resume", "extend", "fresh", "cancel"))
    legacy_complete = validate_legacy_completion(layout).ok
    return _Collision(
        path,
        "legacy-complete" if legacy_complete else "legacy-incomplete",
        layout,
        ("resume", "extend", "fresh", "cancel"),
    )


def _create_and_publish(library: Path, component: str, question: str) -> tuple[Path, str]:
    transaction = create_skeleton_transaction(library, component, question=question)
    # The slug lease uses the portable collision key. Recheck after acquiring it so a
    # non-cooperating writer cannot introduce a case/Unicode alias between planning and publication.
    aliases = _collision_paths(library, component)
    if aliases:
        transaction.lease.release(transaction.lease.owner.token)
        raise TransactionConflict(f"portable alias appeared before publication: {aliases[0]}")
    return transaction.publish(), transaction.transaction_id


def _fresh_component(library: Path, base_slug: str) -> str:
    timestamp = filesystem_safe_timestamp()
    reserved = f"-{timestamp}-999999"
    base = slugify_v1(base_slug, reserved_suffix=reserved)
    for retry in range(1, 1_000_000):
        suffix = f"-{timestamp}" if retry == 1 else f"-{timestamp}-{retry}"
        candidate = f"{base}{suffix}"
        if not _collision_paths(library, candidate):
            return candidate
    raise ManagerError(Exit.COLLISION, "unable to reserve a fresh sibling name")


def _launch_broker(library: Path, run_dir: Path, action: str) -> tuple[LocalBrokerClient, Path]:
    session_id = str(uuid.uuid4())
    scratch = library / ".transactions" / f"session-{session_id}" / "scratch"
    scratch.mkdir(parents=True)
    broker = start_local_broker(
        library,
        run_dir.name,
        operation=action,
        ttl_seconds=300,
        dispatcher_module="scripts.run_manager",
        dispatcher_name="_broker_dispatch",
        context={"run_dir": str(run_dir), "scratch_dir": str(scratch), "session_id": session_id},
        allowed_helpers=_helper_schemas(),
        detached=True,
    )
    return broker, scratch


def _managed_result(
    *,
    action: str,
    run_dir: Path,
    project: Path | None,
    library: Path,
    transaction_id: str | None,
    plan: ResumePlan | None = None,
) -> PrepareResult:
    try:
        broker, scratch = _launch_broker(library, run_dir, action)
    except (BrokerProtocolError, LeaseConflict) as exc:
        raise ManagerError(Exit.LOCKED, str(exc)) from exc
    return PrepareResult(
        action,
        run_dir,
        resume_plan=plan,
        lease_id=broker.owner.lease_id,
        lease_token=broker.owner.token,
        lease_keeper_pid=broker.owner.keeper_pid,
        renewed_until=broker.owner.renewed_until,
        broker_endpoint=str(broker.socket_path),
        scratch_dir=scratch,
        project_dir=project,
        library_dir=library,
        slug=run_dir.name,
        classification="v2",
        layout_version=2,
        schema_version=1,
        transaction_id=transaction_id,
    )


def prepare_run(
    *,
    question: str,
    slug: str | None = None,
    project_dir: os.PathLike[str] | str | None = None,
    library_dir: os.PathLike[str] | str | None = None,
    output_root: os.PathLike[str] | str | None = None,
    mode: str | None = None,
    launch_dir: os.PathLike[str] | str | None = None,
    dry_run: bool = False,
) -> PrepareResult:
    if not isinstance(question, str) or not question.strip():
        raise ManagerError(Exit.INVALID_ARGUMENT, "question must be nonempty")
    if mode not in {None, "resume", "extend", "fresh", "cancel"}:
        raise ManagerError(Exit.INVALID_ARGUMENT, f"unsupported collision mode: {mode}")
    resolved = resolve_project_root(
        project_dir=project_dir,
        library_dir=library_dir,
        output_root=output_root,
        launch_dir=launch_dir,
    )
    library = resolved.library
    base_slug = slugify_v1(slug or question)
    collisions = _collision_paths(library, base_slug)
    collision = _classify_collision(collisions) if collisions else None

    if collision is not None and mode is None:
        return PrepareResult(
            "mode-required",
            collision.path,
            collision.choices,
            project_dir=resolved.project,
            library_dir=library,
            slug=base_slug,
            classification=collision.classification,
            write_probe_pending=dry_run,
        )
    if mode == "cancel":
        return PrepareResult(
            "cancelled",
            None,
            project_dir=resolved.project,
            library_dir=library,
            slug=base_slug,
            classification=collision.classification if collision else None,
            write_probe_pending=dry_run,
        )

    if collision is not None and mode == "resume":
        if "resume" not in collision.choices:
            code = Exit.FROZEN_PARENT if collision.classification == "frozen" else Exit.INVALID_LAYOUT
            raise ManagerError(code, f"resume is unavailable for {collision.classification}")
        assert collision.layout is not None
        if collision.layout.kind is LayoutKind.V2:
            metadata = RunMetadata.load(collision.layout)
            if metadata.status == "complete" or metadata.sealed:
                checked = validate_seal(collision.layout)
                if not checked.ok:
                    raise ManagerError(Exit.INVALID_LAYOUT, "completed run has an invalid seal", details={"errors": checked.errors})
                return PrepareResult(
                    "complete-noop",
                    collision.path,
                    collision.choices,
                    project_dir=resolved.project,
                    library_dir=library,
                    slug=base_slug,
                    classification="complete",
                    layout_version=2,
                    schema_version=metadata.schema_version,
                )
        elif validate_legacy_completion(collision.layout).ok:
            return PrepareResult(
                "complete-noop",
                collision.path,
                collision.choices,
                project_dir=resolved.project,
                library_dir=library,
                slug=base_slug,
                classification="legacy-complete",
            )
        if dry_run:
            plan = resume_plan(collision.layout)
            return PrepareResult(
                "plan-resume",
                collision.path,
                collision.choices,
                plan,
                project_dir=resolved.project,
                library_dir=library,
                slug=base_slug,
                classification=collision.classification,
                layout_version=2 if collision.layout.kind is LayoutKind.V2 else 1,
                schema_version=1,
                write_probe_pending=True,
            )
        probe_filesystem(library)
        plan = resume_plan(collision.layout)
        return _managed_result(
            action="resumed",
            run_dir=collision.path,
            project=resolved.project,
            library=library,
            transaction_id=None,
            plan=plan,
        )

    if collision is not None and mode == "extend":
        if "extend" not in collision.choices:
            raise ManagerError(Exit.UNSAFE_INHERITANCE, f"extension is unavailable for {collision.classification}")
        from scripts.run_extension import ExtensionError, ExtensionPlan, prepare_extension
        try:
            extension = prepare_extension(collision.path, question.strip(), dry_run=dry_run)
        except ExtensionError as exc:
            raise ManagerError(Exit.UNSAFE_INHERITANCE, str(exc)) from exc
        if dry_run:
            assert isinstance(extension, PrepareResult)
            return PrepareResult(
                "plan-extend",
                extension.run_dir,
                collision.choices,
                project_dir=resolved.project,
                library_dir=library,
                slug=extension.slug,
                classification=collision.classification,
                layout_version=2,
                schema_version=1,
                write_probe_pending=True,
            )
        assert isinstance(extension, ExtensionPlan)
        prepared = extension.prepared
        return PrepareResult(
            "extended",
            extension.child,
            collision.choices,
            lease_id=prepared.lease_id,
            lease_token=prepared.lease_token,
            lease_keeper_pid=prepared.lease_keeper_pid,
            renewed_until=prepared.renewed_until,
            broker_endpoint=prepared.broker_endpoint,
            scratch_dir=prepared.scratch_dir,
            project_dir=resolved.project,
            library_dir=library,
            slug=extension.child.name,
            classification="extension",
            layout_version=2,
            schema_version=1,
            transaction_id=prepared.transaction_id,
        )

    if dry_run:
        capabilities = capabilities_for_dry_run(library)
        action = "plan-fresh" if collision is not None or mode == "fresh" else "plan-create"
        planned = library / (f"{base_slug}-<UTC-microseconds>" if action == "plan-fresh" else base_slug)
        return PrepareResult(
            action,
            planned,
            collision.choices if collision else (),
            project_dir=resolved.project,
            library_dir=library,
            slug=base_slug,
            classification=collision.classification if collision else "new",
            layout_version=2,
            schema_version=1,
            write_probe_pending=capabilities.write_probe_pending,
        )

    library.mkdir(parents=True, exist_ok=True)
    probe_filesystem(library)
    component = _fresh_component(library, base_slug) if collision is not None or mode == "fresh" else base_slug
    try:
        run_dir, transaction_id = _create_and_publish(library, component, question.strip())
    except (TransactionConflict, LeaseConflict) as exc:
        raise ManagerError(Exit.COLLISION, str(exc)) from exc
    return _managed_result(
        action="fresh" if component != base_slug else "created",
        run_dir=run_dir,
        project=resolved.project,
        library=library,
        transaction_id=transaction_id,
    )


def _destination_allowed(stage: str, relative: str, layout: RunLayout) -> bool:
    path = safe_relpath(relative)
    value = path.as_posix()
    stage = stage.casefold()
    if stage == "round0":
        return value == "Process/scope.json"
    if stage in {"round1", "retrieval", "evidence_gate"}:
        return value == "Process/retrieval_ledger.json" or value.startswith(
            ("Process/round1/", "Sources/Extracted/")
        )
    if stage in {"round2", "synthesis"}:
        return value.startswith("Process/round2/")
    if stage in {"round2_5", "deepening"}:
        return value.startswith("Process/round2_5/")
    if stage in {"round3", "integration"}:
        return value.startswith(("Process/round3/", "Sections/"))
    if stage in {"round4", "verification", "citation_verifier", "adversary"}:
        return value.startswith("Process/round4/")
    if stage == "export":
        if value in {
            "Sources/bibliography.md",
            "Sources/bibliography.bib",
            "Sources/claims.jsonl",
            "Process/export_manifest.json",
        }:
            return True
        return len(path.parts) == 1 and path.suffix.casefold() in {".md", ".html"} and "bible" in path.name.casefold()
    return False


def _broker_dispatch(request: Mapping[str, Any], context: Mapping[str, Any], lease: RunLease) -> Any:
    layout = RunLayout.open(context["run_dir"])
    scratch = RootedFS(Path(context["scratch_dir"]))
    managed = RootedFS(layout.run_root, lease_token=lease.owner.token, state_guard=make_state_guard(layout, lease=lease))
    action = request["action"]
    if action == "publish-artifact":
        destination = request["logical_destination"]
        if not _destination_allowed(request["stage"], destination, layout):
            raise BrokerProtocolError(f"destination {destination!r} is not allowed for stage {request['stage']!r}")
        source = request["scratch_name"]
        data = scratch.read_bytes(source)
        digest = hashlib.sha256(data).hexdigest()
        if digest != request["sha256"] or len(data) != request["size"]:
            raise BrokerProtocolError("scratch artifact does not match its declared digest and size")
        copied = managed.reflink_or_copy_from(scratch, source, destination)
        return {"path": destination, "sha256": copied, "size": len(data)}
    if action == "record-stage":
        manifest = request["manifest"]
        allowed = {
            "inputs",
            "outputs",
            "dependencies",
            "tool",
            "config_fingerprint",
            "provider_identity",
            "started_at",
        }
        unknown = set(manifest) - allowed
        if unknown:
            raise BrokerProtocolError(f"unknown record-stage fields: {sorted(unknown)}")
        metadata = RunMetadata.load(layout)
        if metadata.status == "failed":
            transition_status(layout, "incomplete")
        recorded = record_stage(
            layout,
            request["stage"],
            inputs=manifest.get("inputs", []),
            outputs=manifest.get("outputs", []),
            dependencies=manifest.get("dependencies", []),
            tool=manifest.get("tool", request["stage"]),
            config_fingerprint=manifest.get("config_fingerprint", ""),
            provider_identity=manifest.get("provider_identity"),
            started_at=manifest.get("started_at"),
        )
        return recorded.to_dict()
    if action == "invoke-helper":
        helper_id = request["helper_id"]
        if helper_id not in MANAGED_HELPERS:
            raise BrokerProtocolError("helper is not registered in the manager")
        module_name, function_name, _ = MANAGED_HELPERS[helper_id]
        function = getattr(importlib.import_module(module_name), function_name)
        from scripts.helper_runtime import broker_managed_context
        with broker_managed_context():
            return function(layout=layout, fs=managed, typed_args=dict(request["args"]))
    if action == "export":
        try:
            function = getattr(importlib.import_module("scripts.export"), "managed_export")
        except (ImportError, AttributeError) as exc:
            raise BrokerProtocolError("managed export is not installed") from exc
        from scripts.helper_runtime import broker_managed_context
        with broker_managed_context():
            return function(layout=layout, fs=managed, typed_args=dict(request["options"]))
    if action == "finalize":
        return seal_run(layout, lease=lease)
    if action == "mark-failed":
        metadata = RunMetadata.load(layout)
        if metadata.status == "incomplete":
            updated = transition_status(layout, "failed")
            return updated.to_dict()
        if metadata.status == "failed":
            return metadata.to_dict()
        raise LifecycleError("only an incomplete run can be marked failed")
    raise BrokerProtocolError(f"manager dispatcher cannot execute {action!r}")


def status_run(run: os.PathLike[str] | str) -> dict[str, Any]:
    layout = RunLayout.open(run)
    result: dict[str, Any] = {
        "run_dir": str(layout.run_root),
        "layout": layout.kind.value,
        "layout_version": 2 if layout.kind is LayoutKind.V2 else 1,
    }
    if layout.kind is LayoutKind.V2:
        metadata = RunMetadata.load(layout)
        result.update(
            status=metadata.status,
            sealed=metadata.sealed,
            frozen=metadata.frozen_for_derivation,
            generation=metadata.generation,
            run_id=metadata.run_id,
            resume_plan=asdict(resume_plan(layout)) if not metadata.sealed and not metadata.frozen_for_derivation else None,
        )
    else:
        result.update(
            status="complete" if validate_legacy_completion(layout).ok else "incomplete",
            sealed=False,
            frozen=False,
            resume_plan=asdict(resume_plan(layout)),
        )
    return result


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(type(value).__name__)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, default=_json_default, ensure_ascii=False, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    prepare = subcommands.add_parser("prepare")
    prepare.add_argument("--question", required=True)
    prepare.add_argument("--slug")
    prepare.add_argument("--project-dir")
    prepare.add_argument("--library-dir")
    prepare.add_argument("--output-root")
    prepare.add_argument("--mode", choices=("resume", "extend", "fresh", "cancel"))
    prepare.add_argument("--dry-run", action="store_true")
    prepare.add_argument("--json", action="store_true")

    status = subcommands.add_parser("status")
    status.add_argument("run_dir")
    status.add_argument("--json", action="store_true")

    for name in ("invoke-helper", "publish-artifact", "record-stage", "export", "finalize", "mark-failed"):
        command = subcommands.add_parser(name)
        command.add_argument("--broker-endpoint", required=True)
        command.add_argument("--lease-token", required=True)
        command.add_argument("--json", action="store_true")
        command.add_argument("--reply-timeout", type=float, default=None,
                             help="seconds to wait for the broker's reply (default: run_transactions.BROKER_REPLY_TIMEOUT, "
                                  "the broker's helper deadline + 300 s). Raise it when another long helper may be queued "
                                  "ahead of this one or when the broker's helper deadline was raised — a client that quits "
                                  "before the broker does retries into a second dispatch")
        if name == "invoke-helper":
            command.add_argument("--helper", required=True)
            command.add_argument("--args-json", default="{}")
        elif name == "publish-artifact":
            command.add_argument("--scratch-file", required=True)
            command.add_argument("--scratch-dir")
            command.add_argument("--logical-path", required=True)
            command.add_argument("--sha256", required=True)
            command.add_argument("--size", required=True, type=int)
            command.add_argument("--stage", required=True)
        elif name == "record-stage":
            command.add_argument("--stage", required=True)
            command.add_argument("--manifest-json", required=True)
        elif name == "export":
            command.add_argument("--options-json", default="{}")
        elif name == "mark-failed":
            command.add_argument("--reason", required=True)

    lease = subcommands.add_parser("lease")
    lease_commands = lease.add_subparsers(dest="lease_command", required=True)
    for name in ("renew", "release"):
        command = lease_commands.add_parser(name)
        command.add_argument("--broker-endpoint", required=True)
        command.add_argument("--lease-token", required=True)
        command.add_argument("--json", action="store_true")

    recover = subcommands.add_parser("recover")
    recover.add_argument("--library-dir", required=True)
    recover.add_argument("--transaction-id", required=True)
    recover.add_argument("--json", action="store_true")
    migrate = subcommands.add_parser("migrate")
    migrate.add_argument("path", help="legacy run, research library, or project")
    migrate.add_argument("--destination-library")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--json", action="store_true")
    migration_recover = subcommands.add_parser("migration-recover")
    migration_recover.add_argument("library")
    migration_recover.add_argument("--mode", choices=("continue", "abort"), required=True)
    migration_recover.add_argument("--json", action="store_true")
    rollback = subcommands.add_parser("rollback-migration")
    rollback.add_argument("run_dir")
    rollback.add_argument("--json", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(list(argv) if argv is not None else None)
        if arguments.command == "prepare":
            result = prepare_run(
                question=arguments.question,
                slug=arguments.slug,
                project_dir=arguments.project_dir,
                library_dir=arguments.library_dir,
                output_root=arguments.output_root,
                mode=arguments.mode,
                dry_run=arguments.dry_run,
            )
            _emit(result.to_dict())
            return int(Exit.MODE_REQUIRED if result.action == "mode-required" else Exit.OK)
        if arguments.command == "status":
            _emit(status_run(arguments.run_dir))
            return int(Exit.OK)
        if arguments.command == "recover":
            outcome = recover_creation(arguments.library_dir, arguments.transaction_id)
            _emit(asdict(outcome))
            return int(Exit.OK)
        if arguments.command == "migrate":
            from scripts.run_migration import apply_migration, discover_targets, plan_migration
            targets = discover_targets(arguments.path, destination_library=arguments.destination_library)
            plans = [plan_migration(target.source, target.destination_library) for target in targets]
            if arguments.dry_run:
                _emit({"dry_run": True, "plans": [plan.to_dict() for plan in plans]})
            else:
                migrated = [str(apply_migration(plan)) for plan in plans]
                _emit({"dry_run": False, "migrated": migrated})
            return int(Exit.OK)
        if arguments.command == "migration-recover":
            from scripts.run_migration import recover_migration
            _emit({"outcomes": recover_migration(arguments.library, mode=arguments.mode)})
            return int(Exit.OK)
        if arguments.command == "rollback-migration":
            from scripts.run_migration import rollback_migration
            _emit({"restored": str(rollback_migration(arguments.run_dir))})
            return int(Exit.OK)
        if arguments.command == "lease":
            result = broker_request(
                arguments.broker_endpoint,
                arguments.lease_token,
                arguments.lease_command,
            )
            _emit(result)
            return int(Exit.OK)

        payload: dict[str, Any] = {}
        if arguments.command == "invoke-helper":
            payload = {"helper_id": arguments.helper, "args": json.loads(arguments.args_json)}
        elif arguments.command == "publish-artifact":
            scratch_path = Path(arguments.scratch_file)
            if arguments.scratch_dir:
                scratch_name = scratch_path.resolve().relative_to(Path(arguments.scratch_dir).resolve()).as_posix()
            else:
                scratch_name = scratch_path.name
            payload = {
                "scratch_name": scratch_name,
                "logical_destination": arguments.logical_path,
                "sha256": arguments.sha256,
                "size": arguments.size,
                "stage": arguments.stage,
            }
        elif arguments.command == "record-stage":
            payload = {"stage": arguments.stage, "manifest": json.loads(arguments.manifest_json)}
        elif arguments.command == "export":
            payload = {"options": json.loads(arguments.options_json)}
        elif arguments.command == "mark-failed":
            payload = {"reason": arguments.reason}
        timeout_kw = {"reply_timeout": arguments.reply_timeout} if getattr(arguments, "reply_timeout", None) is not None else {}
        result = broker_request(
            arguments.broker_endpoint,
            arguments.lease_token,
            arguments.command,
            **timeout_kw,
            **payload,
        )
        _emit(result)
        return int(Exit.OK)
    except ManagerError as exc:
        _emit({"error": str(exc), "exit": int(exc.exit_code), **exc.details})
        return int(exc.exit_code)
    except BrokerStop as exc:
        # the broker stopped (lease lost, helper deadline): the run may be in an
        # ambiguous state — distinct from a validation rejection
        _emit({"error": str(exc), "exit": int(Exit.FAILED), "broker_stopped": True})
        return int(Exit.FAILED)
    except (ValueError, json.JSONDecodeError, LayoutError, LifecycleError, BrokerProtocolError) as exc:
        _emit({"error": str(exc), "exit": int(Exit.INVALID_ARGUMENT)})
        return int(Exit.INVALID_ARGUMENT)
    except Exception as exc:  # fail closed with a stable nonzero result
        _emit({"error": str(exc), "exit": int(Exit.FAILED)})
        return int(Exit.FAILED)


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
