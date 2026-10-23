# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Sealed source-only capability; observed command history never becomes free execution."""

from __future__ import annotations

import copy
import json
import re
import time
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.agent_roles import roles_for_stage
from firefly_forge.cli_command_budget import retained_command_history
from firefly_forge.journal import digest
from firefly_forge.policies import policy_commands
from firefly_forge.provider_executable_adoption import executable_identity
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.source_edit_cli_policy import SOURCE_EDIT_LAUNCH
from firefly_forge.source_edit_models import PROTOCOL, READ_TOOLS, SourceEditContinuationInput, SourceReadRequest
from firefly_forge.source_edit_qualification import policy_digest
from firefly_forge.store import identifier
from firefly_forge.tool_broker import frozen_policy

Data = dict[str, Any]


def clock_ms() -> int:
    return int(time.time() * 1000)


def stable_cut(cut: Data) -> Data:
    # Native source edits/index preparation are allowed; Git identity and authority are not.
    return {key: value for key, value in cut.items() if key not in {"source_digest", "index_digest"}}


async def source_edit_scope(
    recoveries: Any, run: Data, *, archive_cut: list[Data] | None = None, configuration_proof: Data | None = None
) -> Data:
    if run.get("agent") != "codex":
        raise ValueError("Source-only continuation requires the qualified official Codex adapter")
    workspace = recoveries.engine.workspaces / run["id"]
    try:
        (workspace / ".codex").lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("Project-local Codex autoload configuration prevents source-only qualification")
    roles = roles_for_stage(run.get("role_snapshot") or {}, "implement")
    granted = {tool for role in roles for tool in role["allowed_tools"]}
    if not set(READ_TOOLS) <= granted:
        raise ValueError("Source readers must already belong to the frozen implementation roles")
    settings = recoveries.store.configuration.load_snapshot(run["config_revision"])
    executable = executable_identity(settings.get("FORGE_CODEX_EXECUTABLE", ""), recoveries.engine.workspaces)
    from firefly_forge.source_edit_qualification import profile_seal, qualified_profile

    profile = qualified_profile(executable, settings.get("FORGE_CODEX_MODEL", ""))
    qualification = profile_seal(profile)
    from firefly_forge.source_edit_filesystem_policy import build_named_permissions, source_private_roots

    private_roots = source_private_roots(recoveries.store.configuration.directory)
    permissions_digest = digest(build_named_permissions(workspace, private_roots))
    timeout = float(settings.get("FORGE_PROVIDER_TIMEOUT", "1800"))
    if not 0 < timeout <= 86400:
        raise ValueError("The original provider requires a finite bounded deadline")
    if configuration_proof is None:
        from firefly_forge.source_edit_cli_policy import qualify_configuration

        configuration_proof = await qualify_configuration(
            executable, timeout, profile=profile, workspace=workspace, private_roots=private_roots
        )
    if (
        configuration_proof.get("version") != 5
        or configuration_proof.get("profile") != qualification
        or set(configuration_proof)
        != {"version", "profile", "configuration_digest", "permissions_digest", "managed_execution_conflict"}
        or configuration_proof.get("permissions_digest") != permissions_digest
        or not isinstance(configuration_proof.get("configuration_digest"), str)
        or re.fullmatch(r"[a-f0-9]{64}", configuration_proof["configuration_digest"]) is None
        or configuration_proof.get("managed_execution_conflict") is not False
        or executable_identity(settings.get("FORGE_CODEX_EXECUTABLE", ""), recoveries.engine.workspaces) != executable
    ):
        raise ValueError("The qualified native build or profile-bound configuration proof changed")
    observed = await retained_command_history(recoveries.store, run["id"], archive_cut=archive_cut)
    native = len(await recoveries.journal.sandbox_executions(run["id"]))
    final = len(run.get("commands", [])) + len(policy_commands(run.get("policy_snapshot", {}), run["backend"]))
    limit = validate_sandbox_snapshot(run["sandbox_snapshot"]).command_limit
    remaining = limit - observed["cli_commands"] - native
    if not observed["cli_invocations"] or not 1 <= final <= 20 or remaining < final:
        raise ValueError("The original combined budget cannot cover source-only final verification")
    policy = frozen_policy(run.get("tool_policy_snapshot"))
    return {
        "protocol": PROTOCOL,
        "cli_policy_digest": policy_digest(),
        "qualification": qualification,
        "configuration_proof": configuration_proof,
        "tools": list(READ_TOOLS),
        "native_utilities": [
            "clock.curr_time",
            "list_mcp_resources",
            "list_mcp_resource_templates",
            "read_mcp_resource",
        ],
        "native_resources": {"server": "forge_coding", "resources": 0, "templates": 0, "read": "not_found"},
        "roles_digest": digest(run["role_snapshot"]),
        "tool_policy_digest": policy["digest"],
        "calls_limit": min(30, policy["settings"]["calls_limit"]),
        "per_tool_limit": min(10, policy["settings"]["per_tool_limit"]),
        "max_result_bytes": policy["settings"]["max_result_bytes"],
        "provider_timeout": timeout,
        "executable": executable,
        **observed,
        "native_commands": native,
        "command_limit": limit,
        "final_checks": final,
        "remaining_commands": remaining,
        "interactive_commands": 0,
        "channel": {"transport": "streamable_http", "host": "127.0.0.1", "bind_port": 0},
    }


async def source_edit_receipt(
    recoveries: Any, run: Data, *, task_key: str | None = None, qualify: bool = False
) -> Data:
    """Validate native history, current authority and the exact existing task, never a boolean marker."""
    marker = run.get("source_edit_continuation") or {}
    async with recoveries.store._unit():
        entries = await recoveries._entries(run["id"])
    event = next(
        (
            e
            for e in entries
            if e["kind"] == "workspace_reconciliation_receipt" and e["idempotency_key"] == marker.get("request_id")
        ),
        None,
    )
    if event is None or event["digest"] != marker.get("receipt_digest"):
        raise ValueError("Source-only continuation has no sealed native activation")
    receipt: Data = copy.deepcopy(event["payload"])
    body = SourceEditContinuationInput.model_validate(receipt["request"])
    candidate = receipt["candidate"]
    if (
        candidate.get("source_edit", {}).get("protocol") != PROTOCOL
        or body.candidate_digest != candidate["digest"]
        or candidate["digest"] != digest({k: v for k, v in candidate.items() if k != "digest"})
    ):
        raise ValueError("Source-only activation does not match its original review")
    intent = next(
        (
            e
            for e in entries
            if e["kind"] == "workspace_reconciliation_intent" and e["idempotency_key"] == body.request_id
        ),
        None,
    )
    if (
        intent is None
        or intent["digest"] != receipt.get("intent_digest")
        or any(intent["payload"][key] != receipt[key] for key in ("request", "actor", "candidate", "previous_task"))
    ):
        raise ValueError("Source-only activation lost its durable original intent")
    actor = receipt["actor"]
    async with recoveries._locked(run["id"], actor) as row:
        await recoveries.store.assert_ownership()
        current = row.data
        task = await recoveries.journal._task(run["id"], "implement")
        attempt = candidate["previous_task"]["attempt"]
        expected_key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{attempt}").hex
        if (
            task.attempt != attempt
            or current.get("source_edit_continuation") != marker
            or (task_key is not None and task_key != expected_key)
            or (
                task_key is not None
                and (
                    current.get("status") != "developing"
                    or task.status != "running"
                    or task.data.get("idempotency_key") != task_key
                )
            )
            or task.status not in {"queued", "running"}
            or current.get("active_task") != "implement"
            or receipt.get("expires_at_ms", 0) <= clock_ms()
        ):
            raise ValueError("The exact source-edit task or activation is no longer current")
        if stable_cut(recoveries._cut(current)) != stable_cut(receipt["completed_cut"]):
            raise ValueError("Source-edit provider, Git identity or execution authority changed")
        scope = await source_edit_scope(
            recoveries,
            current,
            archive_cut=candidate["source_edit"]["archive_cut"],
            configuration_proof=None if qualify else candidate["source_edit"]["configuration_proof"],
        )
        await recoveries.store.assert_ownership()
        if stable_cut(recoveries._cut(current)) != stable_cut(receipt["completed_cut"]):
            raise ValueError("Source-edit execution authority changed during native policy qualification")
        from firefly_forge.source_budget_allowances import apply_current_budget_scope

        scope = apply_current_budget_scope(recoveries.store, current, entries, scope)
        # Native final checks may spend the original remaining reserve, never increase it.
        if set(scope) != set(candidate["source_edit"]):
            raise ValueError("The source-only scope has an unsupported capability field")
        fixed = [key for key in scope if key not in {"native_commands", "remaining_commands"}]
        if any(scope[key] != candidate["source_edit"][key] for key in fixed):
            raise ValueError("Source-edit retained command history or capability changed")
        return receipt


async def assert_source_edit_dispatch(recoveries: Any, run: Data, task_key: str | None) -> None:
    receipt = await source_edit_receipt(recoveries, run, task_key=task_key, qualify=True)
    current = await recoveries.store.get_run(run["id"])
    if recoveries._cut(current)["source_digest"] != receipt["completed_cut"]["source_digest"]:
        raise ValueError("The sealed source changed before the source-only provider launch")
    launch = SOURCE_EDIT_LAUNCH.get()
    if (
        not isinstance(launch, dict)
        or launch.get("activation") != run.get("source_edit_continuation")
        or launch.get("permissions_digest")
        != receipt["candidate"]["source_edit"]["configuration_proof"]["permissions_digest"]
    ):
        raise ValueError("Source-only dispatch requires its sealed native CLI channel")
    server = launch["server"]
    if (
        tuple(server.tool_names) != READ_TOOLS
        or server.source_callback is None
        or not server.accepting
        or not server.endpoint
        or server.expires_at_ms <= clock_ms()
        or server.expires_at_ms > receipt["expires_at_ms"]
        or receipt["candidate"]["source_edit"]["cli_policy_digest"] != policy_digest()
    ):
        raise ValueError("The source-only CLI policy or bounded channel changed")


class SourceEditInvocation:
    def __init__(self, recoveries: Any, run_id: str, task_key: str) -> None:
        self.recoveries, self.run_id, self.task_key = recoveries, run_id, task_key

    async def _capability_receipt(self, run: Data, *, before_dispatch: bool) -> None:
        r = self.recoveries
        receipt = await source_edit_receipt(r, run, task_key=None if before_dispatch else self.task_key)
        if not before_dispatch:
            return
        current = await r.store.get_run(self.run_id)
        task = await r.journal._task(self.run_id, "implement")
        attempt = receipt["candidate"]["previous_task"]["attempt"]
        key = uuid5(NAMESPACE_URL, f"forge:{self.run_id}:implement:{attempt}").hex
        reservation = current.get("provider_recovery") or {}
        if (
            self.task_key != key
            or task.status != "queued"
            or task.attempt != attempt
            or task.data.get("idempotency_key") != key
            or task.data.get("provider_recovery_id") != receipt["request"]["request_id"]
            or reservation.get("status") != "reserved"
            or reservation.get("task_key") != key
            or reservation.get("attempt") != attempt
            or reservation.get("request_id") != receipt["request"]["request_id"]
            or reservation.get("actor") != receipt["actor"]
            or current.get("source_edit_continuation") != run.get("source_edit_continuation")
        ):
            raise ValueError("Capability projection requires the exact queued source recovery reservation")

    async def capabilities(self, *, before_dispatch: bool = False) -> Data:
        from firefly_forge.source_budget_allowances import effective_source_read_limits
        from firefly_forge.source_tool_budget import source_budget_snapshot
        from firefly_forge.tool_broker import ToolReconciliationRequired

        r = self.recoveries
        async with r.store._unit():
            run = await r.store.get_run(self.run_id)
            await self._capability_receipt(run, before_dispatch=before_dispatch)
            broker = r.engine.tool_broker
            if broker is None:
                raise ValueError("Source capability review requires the original durable tool broker")
            rows = await broker.records.for_run(identifier(self.run_id))
            if any(row.status in {"pending", "unknown"} for row in rows):
                raise ToolReconciliationRequired(
                    "Unresolved source tools require reconciliation before capability review"
                )
            limits, _authority = await effective_source_read_limits(r.store, r.journal, run, rows)
            value = source_budget_snapshot(rows, run["tool_policy_snapshot"]["settings"], per_tool=limits)
            await self._capability_receipt(await r.store.get_run(self.run_id), before_dispatch=before_dispatch)
            return value

    async def run(self, request: SourceReadRequest) -> Data:
        request = SourceReadRequest.model_validate(request.model_dump(exclude_unset=True))
        r = self.recoveries
        receipt = await source_edit_receipt(r, await r.store.get_run(self.run_id), task_key=self.task_key)
        workspace = r.engine.workspaces / self.run_id
        run = await r.store.get_run(self.run_id)
        cut = r._cut(run)
        broker = r.engine.tool_broker
        if broker is None:
            raise ValueError("Source readers require the original durable native tool broker")

        async def guard(_result: Data | None = None) -> Any:
            await source_edit_receipt(r, await r.store.get_run(self.run_id), task_key=self.task_key)
            await r.store.assert_ownership()
            current = await r.store.get_run(self.run_id)

            def unchanged() -> None:
                if r._cut(current) != cut:
                    raise ValueError("The current source cut changed during reader admission")

            unchanged()
            return unchanged

        async def read() -> Data:
            await guard()
            from firefly_forge.workspace_tools import WorkspaceTools

            component = (run.get("component_snapshot") or {}).get("path", ".")
            scope = workspace / component
            tools = WorkspaceTools(
                scope,
                repository_root=workspace,
                max_file_bytes=max(1, receipt["candidate"]["source_edit"]["max_result_bytes"] - 2048),
            )
            value: Data
            if request.tool == "list_files":
                value = {"files": await tools.list_files()}
            elif request.tool == "read_file":
                value = {"path": request.path, "content": await tools.read_file(request.path)}
            else:
                value = await tools.search_text(request.text, request.path, request.max_results)
            await guard()
            return {**value, "source_digest": cut["source_digest"]}

        return cast(
            Data,
            await broker.invoke(
                self.run_id,
                "implement",
                request.tool,
                request.request_id,
                request.model_dump(exclude_unset=True, exclude={"tool", "request_id"}),
                read,
                source={
                    "base_revision": run["base_revision"],
                    "head": receipt["candidate"]["plan"]["base_revision"],
                    "source_tree": cut["source_digest"],
                },
                configuration=run["source_edit_continuation"],
                validate_admission=guard,
                validate_result=guard,
                validate_receipt=guard,
            ),
        )


async def _assert_source_administrator(store: Any, actor: Data) -> None:
    from pyfly.data.relational.sqlalchemy import LockMode

    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    async with store._unit():
        user = await ForgeIdentityUserRepository().find_by_id(
            identifier(actor["user_id"]), lock=LockMode.PESSIMISTIC_WRITE
        )
        if (
            user is None
            or not user.enabled
            or digest({"issuer": user.issuer, "subject": user.subject}) != actor["identity_digest"]
            or not await ProjectAccess(store).authorize_actor(actor, None, "members")
        ):
            raise AccessDenied("The source-edit administrator is no longer current")


async def assert_source_final_budget(store: Any, journal: Any, run: Data, task_id: str, execution_id: str) -> None:
    """Charge observed historical CLI calls without fabricating native runner receipts."""
    if task_id != "verify":
        raise ValueError("Source-only continuation reserves commands for independent final checks only")
    await store.assert_ownership()
    async with store._unit():
        rows = await journal.records.find_for_run(identifier(run["id"]))
        entries = [{**copy.deepcopy(row.data), "digest": row.digest} for row in rows]
    if len(entries) > 10000 or len(json.dumps(entries)) > 16 * 1024 * 1024 or not journal.verify_chain(entries):
        raise ValueError("Final source-only verification requires its intact journal")
    marker = run["source_edit_continuation"]
    event = next(
        (
            e
            for e in entries
            if e["kind"] == "workspace_reconciliation_receipt" and e["idempotency_key"] == marker.get("request_id")
        ),
        None,
    )
    if event is None or event["digest"] != marker.get("receipt_digest"):
        raise ValueError("Final verification has no sealed source-edit activation")
    receipt = event["payload"]
    SourceEditContinuationInput.model_validate(receipt["request"])
    if receipt.get("expires_at_ms", 0) <= clock_ms():
        raise ValueError("The original source-edit activation deadline expired")
    scope = receipt["candidate"]["source_edit"]
    from firefly_forge.source_edit_qualification import profile_seal, qualified_profile

    settings = store.configuration.load_snapshot(run["config_revision"])
    if profile_seal(qualified_profile(scope["executable"], settings.get("FORGE_CODEX_MODEL", ""))) != scope.get(
        "qualification"
    ):
        raise ValueError("The final source-only verification capability profile changed")
    await _assert_source_administrator(store, receipt["actor"])
    history = await retained_command_history(store, run["id"], archive_cut=scope["archive_cut"])
    records = await journal.sandbox_executions(run["id"])
    configuration = validate_sandbox_snapshot(run["sandbox_snapshot"])
    from firefly_forge.verification_models import verification_scope

    if (
        scope["protocol"] != PROTOCOL
        or history["cli_commands"] != scope["cli_commands"]
        or configuration.command_limit != scope["command_limit"]
        or verification_scope(run) != receipt["candidate"]["cut"]["scope_digest"]
        or digest(run.get("commands", [])) != receipt["candidate"]["cut"]["commands_digest"]
    ):
        raise ValueError("Final source-only verification changed its original authority or command cut")
    if (
        not any(row["execution_id"] == execution_id for row in records)
        and len(records) + history["cli_commands"] >= configuration.command_limit
    ):
        raise ValueError("The original combined CLI and native command budget is exhausted")
    await store.assert_ownership()
    await _assert_source_administrator(store, receipt["actor"])
