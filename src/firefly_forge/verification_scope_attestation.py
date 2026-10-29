# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fresh, private-input-bound scope for declared local build verification."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.journal import digest
from firefly_forge.private_identity import private_identity
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.secrets import runtime_environment
from firefly_forge.source_catalog import _json
from firefly_forge.verification_dependencies import dependency_fingerprint
from firefly_forge.verification_scope import SANDBOX_EXEC, build_check_kind, build_profile, wrap_build_argv
from firefly_forge.verification_source_preparation import verification_catalog_root

Data = dict[str, Any]
Authority = Callable[[], Awaitable[None]]
_HEX = r"^[a-f0-9]{64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class BuildCheckV1(_Strict):
    command_index: int = Field(ge=1)
    kind: Literal["python_static", "node_build"]
    argv: list[str] = Field(min_length=1)
    wrapped_argv: list[str] = Field(min_length=4)


class VerificationScopeV1(_Strict):
    version: Literal[1] = 1
    kind: Literal["verification_build_scope"] = "verification_build_scope"
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    handoff_intent_digest: str = Field(pattern=_HEX)
    source_digest: str = Field(pattern=_HEX)
    commands_digest: str = Field(pattern=_HEX)
    scope_digest: str = Field(pattern=_HEX)
    runtime_contract: str = Field(pattern=_HEX)
    dependency_digest: str = Field(pattern=_HEX)
    environment_digest: str = Field(pattern=_HEX)
    credential_digest: str = Field(pattern=_HEX)
    prerequisite_digest: str = Field(pattern=_HEX)
    wrapper_digest: str = Field(pattern=_HEX)
    declaration_digest: str = Field(pattern=_HEX)
    dependency_roots: list[str] = Field(min_length=1)
    scratch: str
    checks: list[BuildCheckV1] = Field(min_length=1)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Scope version requires an exact integer")
        return value


def scope_record(run: Data, intent: Data, entries: list[Data]) -> VerificationScopeV1 | None:
    records = [e for e in entries if e["kind"] == "verification_build_scope"]
    for record in records:
        if VerificationScopeV1.model_validate(record["payload"]).run_id != run["id"]:
            raise ValueError("Build scope belongs to another run")
    if not records:
        return None
    handoffs = [e for e in entries if e["kind"] == "verification_handoff_intent" and e["payload"]["id"] == intent["id"]]
    if len(handoffs) != 1:
        raise ValueError("Build scope requires one immutable handoff intent")
    matching = [e for e in records if e["payload"].get("handoff_intent_digest") == handoffs[0]["digest"]]
    if not matching:
        return None
    if len(matching) != 1:
        raise ValueError("Build scope requires one unique witness")
    record, handoff = matching[0], handoffs[0]
    scope = VerificationScopeV1.model_validate(record["payload"])
    if (
        scope.run_id != run["id"]
        or scope.source_digest != intent["source_digest"]
        or scope.commands_digest != intent["commands_digest"]
        or scope.scope_digest != intent["scope_digest"]
        or scope.declaration_digest != intent.get("reusable_build_declaration_digest")
        or record["sequence"] <= handoff["sequence"]
        or len({c.command_index for c in scope.checks}) != len(scope.checks)
    ):
        raise ValueError("Build scope changed its frozen handoff boundary")
    for check in scope.checks:
        if (
            check.command_index > len(run["commands"])
            or check.argv != run["commands"][check.command_index - 1]["argv"]
            or check.wrapped_argv
            != wrap_build_argv(
                check.argv,
                build_profile(
                    Path(intent["workspace"]),
                    [Path(p) for p in scope.dependency_roots],
                    Path(scope.scratch) / str(check.command_index),
                ),
            )
        ):
            raise ValueError("Build wrapper must preserve the exact frozen original command")
    return scope


def mapped_argv(run: Data, intent: Data, entries: list[Data], index: int) -> list[str]:
    scope = scope_record(run, intent, entries)
    selected = next((c for c in scope.checks if c.command_index == index + 1), None) if scope else None
    return selected.wrapped_argv if selected else run["commands"][index]["argv"]


def _roots(checks: list[BuildCheckV1], workspace: Path, environment: dict[str, str]) -> list[Path]:
    roots: set[Path] = {SANDBOX_EXEC}
    for check in checks:
        executable = Path(check.argv[0])
        if not executable.is_absolute():
            found = shutil.which(check.argv[0], path=environment.get("PATH"))
            if found is None:
                raise ValueError("A reusable build command requires its available explicit executable")
            executable = Path(found)
        roots.add(executable)
        if check.kind == "python_static":
            prefix = executable.parent.parent
            roots.add(
                prefix / "pyvenv.cfg" if (prefix / "pyvenv.cfg").is_file() else executable.resolve().parent.parent
            )
            if (prefix / "lib").is_dir():
                roots.add(prefix / "lib")
            # The interpreter's own library is a separate input when a venv uses a linked binary.
            roots.add(executable.resolve().parent.parent)
        else:
            roots.add(executable.resolve().parent.parent)
            node = shutil.which("node", path=environment.get("PATH"))
            if node is None:
                raise ValueError("A reusable Node build requires its current PATH-selected Node executable")
            roots.add(Path(node).resolve().parent.parent)
            roots.add(workspace / check.argv[2] / "node_modules")
    return sorted(roots | {p.resolve(strict=True) for p in roots})


def _prerequisite_files(environment: dict[str, str]) -> list[Path]:
    # These may contain credentials: bind their private bytes without granting build checks read access.
    return sorted(
        {
            Path(value)
            for name, value in environment.items()
            if name.endswith(("_FILE", "_PATH")) and Path(value).is_absolute() and Path(value).is_file()
        }
    )


@dataclass(frozen=True)
class PreparedBuildScope:
    witness: VerificationScopeV1
    _measure: Callable[[], Awaitable[VerificationScopeV1]]

    async def revalidate(self) -> None:
        if await self._measure() != self.witness:
            raise ValueError("Verification build inputs changed after their fresh scope witness")


async def prepare_build_scope(
    store: Any,
    journal: Any,
    engine: Any,
    run: Data,
    intent: Data,
    handoff: Data,
    current: Authority,
) -> PreparedBuildScope | None:
    config = validate_sandbox_snapshot(run["sandbox_snapshot"])
    if sys.platform != "darwin" or config.mode != "host" or config.pool_binding is not None:
        return None
    # Only this explicit retained-final path may create reusable local-build evidence.
    if not (run.get("invalid_read_continuity") or {}).get("request_id"):
        return None
    from firefly_forge.verification_models import verification_build_declarations

    declared = {item.command_digest for item in verification_build_declarations(run)}
    checks = [
        BuildCheckV1(
            command_index=index + 1, kind=kind, argv=command["argv"], wrapped_argv=wrap_build_argv(command["argv"])
        )
        for index, command in enumerate(run["commands"])
        if digest(command) in declared
        and (kind := build_check_kind(command["argv"], Path(intent["workspace"]))) is not None
    ]
    if not checks:
        return None
    root = verification_catalog_root(store.path, run["id"])
    scratch = root / ("build-scratch-" + intent["id"])
    for folder in (
        scratch,
        *(scratch / str(c.command_index) for c in checks),
        *(scratch / str(c.command_index) / part for c in checks for part in ("home", "tmp")),
    ):
        folder.mkdir(mode=0o700, exist_ok=True)
        from firefly_forge.source_catalog import _directory

        descriptor = _directory(folder, private=True)
        os.close(descriptor)
    key = private_identity(root / "build-scope.key", lambda: secrets.token_bytes(32), size=32)

    def sealed(value: Any) -> str:
        return hmac.new(key, _json(value), hashlib.sha256).hexdigest()

    async def measure() -> VerificationScopeV1:
        await current()
        environment = runtime_environment()
        roots = _roots(checks, Path(intent["workspace"]), environment)
        dependencies = await dependency_fingerprint(roots, root, journal.source_catalog_budget, current)
        bound_checks = [
            c.model_copy(
                update={
                    "wrapped_argv": wrap_build_argv(
                        c.argv, build_profile(Path(intent["workspace"]), roots, scratch / str(c.command_index))
                    )
                }
            )
            for c in checks
        ]
        prerequisite_files = await dependency_fingerprint(
            _prerequisite_files(environment), root, journal.source_catalog_budget, current
        )
        registry = engine.sandbox.credential_registry()
        settings = store.configuration.load_snapshot(run["config_revision"])
        value = VerificationScopeV1(
            run_id=run["id"],
            handoff_intent_digest=handoff["digest"],
            source_digest=intent["source_digest"],
            commands_digest=intent["commands_digest"],
            scope_digest=intent["scope_digest"],
            runtime_contract=runtime_contract(),
            dependency_digest=sealed(dependencies),
            environment_digest=sealed(
                {
                    **environment,
                    "HOME": "per-command-private-scratch",
                    "TMPDIR": "per-command-private-scratch",
                    "MYPY_CACHE_DIR": "per-command-private-scratch/mypy-cache",
                    "RUFF_CACHE_DIR": "per-command-private-scratch/ruff-cache",
                    "npm_config_cache": "per-command-private-scratch/npm-cache",
                }
            ),
            credential_digest=sealed(registry),
            prerequisite_digest=sealed(
                dict(
                    configuration=settings,
                    neutral_file_inputs=prerequisite_files,
                    sandbox=run["sandbox_snapshot"],
                    execution_actor=run.get("execution_actor"),
                    policy=run.get("policy_snapshot"),
                    tools=run.get("tool_policy_snapshot"),
                    verification_adoption=run.get("verification_adoption"),
                )
            ),
            declaration_digest=intent["reusable_build_declaration_digest"],
            wrapper_digest=digest(
                dict(
                    executable_sha256=hashlib.sha256(SANDBOX_EXEC.read_bytes()).hexdigest(),
                    profiles=[c.wrapped_argv[2] for c in bound_checks],
                    platform=os.uname(),
                )
            ),
            dependency_roots=[str(p) for p in roots],
            scratch=str(scratch),
            checks=bound_checks,
        )
        await current()
        return value

    return PreparedBuildScope(await measure(), measure)


async def save_build_scope(journal: Any, intent: Data, prepared: PreparedBuildScope, current: Authority) -> Data:
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.verification_models import check_verification_authority

    await prepared.revalidate()
    payload = prepared.witness.model_dump(mode="json")
    async with journal.store._unit():
        await journal._run(payload["run_id"])
        await journal.store.assert_ownership()
        run = await journal.store.get_run(payload["run_id"])
        await ProjectAccess(journal.store).authorize_execution(run)
        check_verification_authority(journal.store, run, intent)
        entries = await journal.entries(run["id"])
        existing = [
            e
            for e in entries
            if e["kind"] == "verification_build_scope"
            and e["payload"].get("handoff_intent_digest") == payload["handoff_intent_digest"]
        ]
        if existing:
            if len(existing) != 1 or _json(existing[0]["payload"]) != _json(payload):
                raise ValueError("Retained build scope cannot be replaced")
            return cast(Data, existing[0])
        if any(
            e["kind"] == "sandbox_intent"
            and e["payload"].get("parent_attempt") == intent["parent_attempt"]
            and e["payload"].get("ordinal") in intent["ordinals"]
            for e in entries
        ):
            raise ValueError("Build scope must be recorded before any handoff command")
        result = await journal._append(
            run["id"], "implement", intent["id"] + ":build-scope", "verification_build_scope", payload
        )
        return cast(Data, result)
