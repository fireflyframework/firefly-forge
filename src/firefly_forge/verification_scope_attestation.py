# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fresh, private-input-bound scope for declared local build verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.agentic_checkpoint_invocation import runtime_contract
from firefly_forge.journal import digest
from firefly_forge.native_location_models import NativeLocationProjectionRecordV1
from firefly_forge.native_location_projection import current_native_location, project_native_argv
from firefly_forge.private_identity import private_identity
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.secrets import runtime_environment
from firefly_forge.source_catalog import _json, _private, _stamp
from firefly_forge.verification_dependencies import prepare_dependency_capture, prepare_public_source_cut
from firefly_forge.verification_scope import (
    SANDBOX_EXEC,
    build_check_kind,
    build_profile,
    fresh_build_check,
    wrap_build_argv,
)
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


class _ScopeFields(_Strict):
    version: int
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

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Scope version requires an exact integer")
        return value


class VerificationScopeV1(_ScopeFields):
    version: Literal[1] = 1
    checks: list[BuildCheckV1] = Field(min_length=1)


class BuildCheckV2(BuildCheckV1):
    physical_argv: list[str] = Field(min_length=1)


class VerificationScopeV2(_ScopeFields):
    version: Literal[2] = 2
    workspace: str
    native_location: NativeLocationProjectionRecordV1
    checks: list[BuildCheckV2] = Field(min_length=1)


VerificationScope = VerificationScopeV1 | VerificationScopeV2


def _scope(value: Data) -> VerificationScope:
    model = VerificationScopeV2 if type(value.get("version")) is int and value["version"] == 2 else VerificationScopeV1
    scope = model.model_validate(value)
    if _json(scope.model_dump(mode="json")) != _json(value):
        raise ValueError("Build scope differs from its exact typed native record")
    return scope


async def assert_scope_location(store: Any, scope: VerificationScope) -> None:
    location = await current_native_location(store)
    if isinstance(scope, VerificationScopeV2):
        if location is None or _json(location.record()) != _json(scope.native_location.model_dump(mode="json")):
            raise ValueError("Verification scope changed its authenticated native location")
        await location.assert_current()
    elif location is not None:
        raise ValueError("A predecessor verification scope cannot authorize a relocated runtime")


def scope_record(run: Data, intent: Data, entries: list[Data]) -> VerificationScope | None:
    records = [e for e in entries if e["kind"] == "verification_build_scope"]
    for record in records:
        if _scope(record["payload"]).run_id != run["id"]:
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
    scope = _scope(record["payload"])
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
    workspace = Path(intent["workspace"])
    location = None
    if isinstance(scope, VerificationScopeV2):
        location = scope.native_location.model_dump(mode="json")
        if intent.get("native_location") is not None and _json(intent["native_location"]) != _json(location):
            raise ValueError("Build scope changed its exact native handoff location record")
        workspace = Path(project_native_argv(location, run["id"], [intent["workspace"]])[0])
        if scope.workspace != str(workspace):
            raise ValueError("Build scope changed its reviewed physical workspace")
    for check in scope.checks:
        physical = project_native_argv(location, run["id"], check.argv) if location is not None else check.argv
        if (
            (isinstance(check, BuildCheckV2) and check.physical_argv != physical)
            or check.command_index > len(run["commands"])
            or check.argv != run["commands"][check.command_index - 1]["argv"]
            or check.wrapped_argv
            != wrap_build_argv(
                physical,
                build_profile(
                    workspace,
                    [Path(p) for p in scope.dependency_roots],
                    Path(scope.scratch) / str(check.command_index),
                    fresh_build=fresh_build_check(check.argv),
                ),
            )
        ):
            raise ValueError("Build wrapper must preserve the exact frozen original command")
    return scope


def mapped_argv(run: Data, intent: Data, entries: list[Data], index: int) -> list[str]:
    scope = scope_record(run, intent, entries)
    selected = next((c for c in scope.checks if c.command_index == index + 1), None) if scope else None
    if selected is not None:
        return selected.wrapped_argv
    logical = run["commands"][index]["argv"]
    if isinstance(scope, VerificationScopeV2):
        return project_native_argv(scope.native_location.model_dump(mode="json"), run["id"], logical)
    if intent.get("native_location") is not None:
        return project_native_argv(intent["native_location"], run["id"], logical)
    return cast(list[str], logical)


def _runtime_boundary_guard(
    workspace: Path,
    environment: dict[str, str],
    private_state: Path | None,
    runtime_roots: list[Path] | None = None,
) -> Callable[[Path, bool], None]:
    owners = {Path.home(), Path(environment.get("HOME", str(Path.home())))}
    boundaries = owners | {workspace} | ({private_state} if private_state is not None else set())
    private = {
        owner / name
        for owner in owners
        for name in (".ssh", ".codex", ".claude", ".aws", ".azure", ".kube", ".gnupg", ".config")
    }

    def aliases(path: Path) -> set[Path]:
        return {Path(os.path.abspath(path)), path.resolve()}

    def identity(path: Path) -> tuple[int, int] | None:
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino

    workspace_paths = aliases(workspace)
    state_paths = aliases(private_state) if private_state is not None else set()
    runtime_paths = {alias for root in runtime_roots or [] for alias in aliases(root)}
    npm_builtins: dict[Path, list[tuple[Path, list[int]]]] = {}

    def empty_npm_builtin(path: Path) -> bool:
        if path.name != ".npmrc" or path != path.resolve():
            return False
        if path not in npm_builtins:
            for executable in runtime_roots or []:
                if executable.name != "npm" or executable.parent.name != "bin":
                    continue
                prefix = executable.parent.parent
                package = prefix / "lib" / "node_modules" / "npm"
                cli, manifest, node = package / "bin" / "npm-cli.js", package / "package.json", prefix / "bin" / "node"
                if (
                    path != package / ".npmrc"
                    or cli not in (runtime_roots or [])
                    or prefix not in (runtime_roots or [])
                    or executable.resolve() != cli
                ):
                    continue
                captured: list[tuple[Path, list[int]]] = []
                for selected in (path, package, executable, cli, manifest, node):
                    info = selected.lstat()
                    if (
                        selected != executable
                        and selected != selected.resolve()
                        or info.st_uid not in {0, os.getuid()}
                        or getattr(info, "st_flags", 0) & getattr(stat, "SF_DATALESS", 0)
                        or (selected == package and not stat.S_ISDIR(info.st_mode))
                        or (selected not in {package, executable} and not stat.S_ISREG(info.st_mode))
                        or (selected != package and info.st_nlink != 1)
                        or (selected == path and info.st_size != 0)
                        or (selected == node and not info.st_mode & stat.S_IXUSR)
                    ):
                        return False
                    captured.append((selected, [*_stamp(info), getattr(info, "st_flags", 0)]))
                descriptor = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "r") as stream:
                    expected = dict(captured)[manifest]
                    opened = os.fstat(stream.fileno())
                    if [*_stamp(opened), getattr(opened, "st_flags", 0)] != expected:
                        raise ValueError("Admitted npm package identity changed before opening")
                    metadata = json.load(stream)
                    after = os.fstat(stream.fileno())
                    if [*_stamp(after), getattr(after, "st_flags", 0)] != expected:
                        raise ValueError("Admitted npm package identity changed while reading")
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("name") != "npm"
                    or not isinstance(metadata.get("bin"), dict)
                    or metadata["bin"].get("npm") != "bin/npm-cli.js"
                ):
                    return False
                npm_builtins[path] = captured
                break
        identities = npm_builtins.get(path)
        if identities is None:
            return False
        for selected, expected in identities:
            info = selected.lstat()
            if [*_stamp(info), getattr(info, "st_flags", 0)] != expected:
                raise ValueError("Admitted empty npm builtin runtime identity changed")
        return True

    def relative(path: Path, root: Path) -> Path | None:
        value, boundary = str(path), str(root)
        if value == boundary:
            return Path(".")
        if value.startswith(boundary + os.sep):
            return Path(value[len(boundary) + 1 :])
        folded_value, folded_boundary = value.casefold(), boundary.casefold()
        if folded_value != folded_boundary and not folded_value.startswith(folded_boundary + os.sep):
            return None
        prefix = Path(*path.parts[: len(root.parts)])
        prefix_id = identity(prefix)
        if prefix_id is None or prefix_id != identity(root):
            return None
        return path.relative_to(prefix)

    def private_name(path: Path, *, full: bool) -> bool:
        def selected(name: str) -> bool:
            return (
                _private(name)
                if full
                else (
                    name
                    in {
                        ".git",
                        ".codex",
                        ".claude",
                        ".forge",
                        ".ssh",
                        ".npmrc",
                        ".pypirc",
                        "credentials.json",
                        "id_rsa",
                        "id_ed25519",
                    }
                    or name == ".env"
                    or (name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"})
                )
            )

        if selected(path.name):
            return True
        folded = path.with_name(path.name.casefold())
        return selected(folded.name) and identity(path) is not None and identity(path) == identity(folded)

    def allowed(path: Path, root: bool) -> None:
        lexical = Path(os.path.abspath(path)) if root else path
        workspace_matches = [value for folder in workspace_paths if (value := relative(lexical, folder)) is not None]
        runtime_matches = [value for folder in runtime_paths if (value := relative(lexical, folder)) is not None]
        in_workspace = bool(workspace_matches)
        if not in_workspace and any(relative(lexical, folder) is not None for folder in state_paths):
            raise ValueError("Build runtime inputs cannot read private application state")
        in_runtime = bool(runtime_matches)
        full_private = in_workspace and runtime_roots is not None and not in_runtime
        matches = runtime_matches or workspace_matches
        anchor = None
        if matches:
            closest = min(matches, key=lambda item: len(item.parts))
            anchor = lexical.parents[len(closest.parts) - 1] if closest.parts else lexical
        for part in lexical.parents:
            if part == anchor:
                break
            if part.name and private_name(part, full=full_private):
                raise ValueError("Build runtime inputs cannot read private source or runtime configuration")
        if private_name(lexical, full=full_private) and (in_workspace or not empty_npm_builtin(lexical)):
            raise ValueError("Build runtime inputs cannot read private configuration")
        if not root:
            return
        # Root/target boundaries use current native identity; ordinary entries already
        # descend through the capture's no-follow directory descriptors.
        candidates = aliases(path)
        ancestors = {
            parent for boundary in boundaries for alias in aliases(boundary) for parent in (alias, *alias.parents)
        }
        private_roots = {alias for folder in private for alias in aliases(folder)}
        ancestor_ids = {native_id for parent in ancestors if (native_id := identity(parent)) is not None}
        private_ids = {native_id for folder in private_roots if (native_id := identity(folder)) is not None}
        parent_ids = {
            native_id
            for candidate in candidates
            for parent in (candidate, *candidate.parents)
            if (native_id := identity(parent)) is not None
        }
        state_ids = {native_id for folder in state_paths if (native_id := identity(folder)) is not None}
        workspace_ids = {native_id for folder in workspace_paths if (native_id := identity(folder)) is not None}
        if (
            any(candidate in ancestors or identity(candidate) in ancestor_ids for candidate in candidates)
            or any(candidate.is_relative_to(folder) for candidate in candidates for folder in private_roots)
            or bool(parent_ids & private_ids)
            or (bool(parent_ids & state_ids) and not parent_ids & workspace_ids)
        ):
            raise ValueError("Build runtime roots cannot grant private owner, source or state ancestors")

    for executable in runtime_roots or []:
        if executable.name == "npm" and executable.parent.name == "bin":
            builtin = executable.parent.parent / "lib" / "node_modules" / "npm" / ".npmrc"
            if builtin.exists():
                allowed(builtin, False)
    return allowed


def _roots(
    checks: list[BuildCheckV1], workspace: Path, environment: dict[str, str], private_state: Path | None = None
) -> list[Path]:
    allowed = _runtime_boundary_guard(workspace, environment, private_state)
    roots: set[Path] = {SANDBOX_EXEC}
    for check in checks:
        executable = Path(check.argv[0])
        if not executable.is_absolute():
            found = shutil.which(check.argv[0], path=environment.get("PATH"))
            if found is None or not Path(found).is_absolute():
                raise ValueError("A reusable build command requires its available explicit executable")
            executable = Path(found)
        roots.add(executable)
        if check.kind == "python_static":
            prefix = executable.parent.parent
            base = executable.resolve().parent.parent
            allowed(base, True)
            if not any(
                (library / "os.py").is_file() and (library / "encodings" / "__init__.py").is_file()
                for library in (base / "lib").glob("python[0-9]*")
            ):
                raise ValueError("A reusable Python check requires a recognized local runtime layout")
            roots.add(prefix / "pyvenv.cfg" if (prefix / "pyvenv.cfg").is_file() else base)
            if (prefix / "lib").is_dir():
                roots.add(prefix / "lib")
            if (prefix / "bin").is_dir():
                roots.add(prefix / "bin")
            # The interpreter's own library is a separate input when a venv uses a linked binary.
            roots.add(base)
        else:
            roots.add(executable.resolve().parent.parent)
            node = shutil.which("node", path=environment.get("PATH"))
            if node is None:
                raise ValueError("A reusable Node build requires its current PATH-selected Node executable")
            roots.add(Path(node).resolve().parent.parent)
            roots.add(workspace / check.argv[2] / "node_modules")
    aliases: set[Path] = set()
    pending, visited = list(roots), set()
    while pending:
        root = pending.pop()
        if root in visited:
            continue
        visited.add(root)
        component = Path(root.anchor)
        for part in root.parts[1:]:
            component /= part
            if component.is_symlink():
                aliases.add(component)
                target = component.readlink()
                target = target if target.is_absolute() else component.parent / target
                target = Path(os.path.normpath(target))
                if target not in visited:
                    pending.append(target)
                aliases.add(target)
    resolved = roots | aliases | {p.resolve(strict=True) for p in roots | aliases}
    for path in resolved:
        allowed(path, True)
    return sorted(resolved)


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
    witness: VerificationScope
    _measure: Callable[[], Awaitable[VerificationScope]]
    _fence: Callable[[], Awaitable[None]]
    _environment: Callable[[], dict[str, str]]

    def environment(self) -> dict[str, str]:
        return self._environment()

    async def fence(self) -> None:
        await self._fence()

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

    location = await current_native_location(store)
    location_record = location.record() if location is not None else None
    workspace = Path(
        location.project_argv(run["id"], [intent["workspace"]])[0] if location is not None else intent["workspace"]
    )
    declared = {item.command_digest for item in verification_build_declarations(run)}
    checks = [
        BuildCheckV1(
            command_index=index + 1, kind=kind, argv=command["argv"], wrapped_argv=wrap_build_argv(command["argv"])
        )
        for index, command in enumerate(run["commands"])
        if (digest(command) in declared or (declared and fresh_build_check(command["argv"])))
        and (kind := build_check_kind(command["argv"], workspace)) is not None
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

    fences: list[Callable[[], Awaitable[None]]] = []
    last_value: VerificationScope | None = None
    expected_generation: object | None = None
    witness: VerificationScope | None = None
    captured_environment: dict[str, str] = {}

    def environment_identity(environment: dict[str, str]) -> str:
        return sealed(
            {
                **environment,
                "HOME": "per-command-private-scratch",
                "TMPDIR": "per-command-private-scratch",
                "MYPY_CACHE_DIR": "per-command-private-scratch/mypy-cache",
                "RUFF_CACHE_DIR": "per-command-private-scratch/ruff-cache",
                "npm_config_cache": "per-command-private-scratch/npm-cache",
            }
        )

    def execution_environment() -> dict[str, str]:
        if witness is None or last_value != witness:
            raise ValueError("Build execution requires its exact prepared environment")
        return dict(captured_environment)

    async def fence() -> None:
        if witness is None:
            raise ValueError("Verification build scope has no prepared witness")
        await assert_scope_location(store, witness)

        def scalars() -> None:
            if (
                witness is None
                or last_value != witness
                or store.configuration.settings_generation is not expected_generation
                or environment_identity(runtime_environment()) != witness.environment_digest
                or sealed(engine.sandbox.credential_registry()) != witness.credential_digest
            ):
                raise ValueError("Verification scalar inputs changed before native acceptance")

        scalars()
        for assert_metadata in list(fences):
            await assert_metadata()
        scalars()
        await assert_scope_location(store, witness)

    async def measure() -> VerificationScope:
        nonlocal fences, last_value, expected_generation, captured_environment
        await current()
        generation = store.configuration.settings_generation
        environment = runtime_environment()
        if location is not None:
            await location.assert_current()
        physical_checks = [
            c.model_copy(update={"argv": project_native_argv(location_record, run["id"], c.argv)})
            if location_record is not None
            else c
            for c in checks
        ]
        roots = _roots(physical_checks, workspace, environment, Path(store.path).parent)
        dependencies = await prepare_dependency_capture(
            roots,
            root,
            journal.source_catalog_budget,
            current,
            validate_path=_runtime_boundary_guard(workspace, environment, Path(store.path).parent, roots),
        )
        bound_checks = []
        for logical, physical in zip(checks, physical_checks, strict=True):
            wrapped = wrap_build_argv(
                physical.argv,
                build_profile(
                    workspace, roots, scratch / str(logical.command_index), fresh_build=fresh_build_check(logical.argv)
                ),
            )
            fields = {**logical.model_dump(mode="json"), "wrapped_argv": wrapped}
            bound_checks.append(
                BuildCheckV2(**fields, physical_argv=physical.argv)
                if location_record is not None
                else BuildCheckV1(**fields)
            )
        prerequisite_files = await prepare_dependency_capture(
            _prerequisite_files(environment), root, journal.source_catalog_budget, current
        )
        public_source = await prepare_public_source_cut(workspace, root, journal.source_catalog_budget)
        registry = engine.sandbox.credential_registry()
        settings = store.configuration.load_snapshot(run["config_revision"])
        from firefly_forge.source_budget_allowances import admission_contract_digest

        fields = dict(
            run_id=run["id"],
            handoff_intent_digest=handoff["digest"],
            source_digest=intent["source_digest"],
            commands_digest=intent["commands_digest"],
            scope_digest=intent["scope_digest"],
            runtime_contract=runtime_contract(),
            dependency_digest=sealed(dependencies.digest),
            environment_digest=environment_identity(environment),
            credential_digest=sealed(registry),
            prerequisite_digest=sealed(
                dict(
                    configuration=settings,
                    admission_contract=admission_contract_digest(),
                    neutral_file_inputs=prerequisite_files.digest,
                    sandbox=run["sandbox_snapshot"],
                    execution_actor=run.get("execution_actor"),
                    policy=run.get("policy_snapshot"),
                    tools=run.get("tool_policy_snapshot"),
                    verification_adoption=run.get("verification_adoption"),
                    **({"native_location": location_record} if location_record is not None else {}),
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
            checks=[c.model_dump(mode="json") for c in bound_checks],
        )
        value: VerificationScope = (
            VerificationScopeV2(
                **fields,
                workspace=str(workspace),
                native_location=NativeLocationProjectionRecordV1.model_validate(location_record),
            )
            if location_record is not None
            else VerificationScopeV1(**fields)
        )
        await assert_scope_location(store, value)
        await current()
        if store.configuration.settings_generation is not generation:
            raise ValueError("Verification configuration changed during input capture")
        fences = [dependencies.assert_current, prerequisite_files.assert_current, public_source.assert_current]
        last_value = value
        expected_generation = generation
        captured_environment = dict(environment)
        return value

    witness = await measure()
    return PreparedBuildScope(witness, measure, fence, execution_environment)


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
        await prepared.fence()
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
        await prepared.fence()
        return cast(Data, result)
