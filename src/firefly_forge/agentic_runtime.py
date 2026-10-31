# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native Agent execution with capability-scoped workspace file tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Any

from pydantic import BaseModel, StrictInt, TypeAdapter, ValidationError

from firefly_forge.agent_roles import WorkerFeedback
from firefly_forge.configuration import redact_secrets
from firefly_forge.diagnostic_recipes import PythonProfileRequest, PythonTraceRequest
from firefly_forge.php_diagnostics import PHPProfileRequest
from firefly_forge.profiles import build_instruction
from firefly_forge.prompts import prompt_text
from firefly_forge.qa import BrowserJourney, QAReference, QAReport, bounded_image_history, qa_instructions
from firefly_forge.runner_monitor_models import MonitorStartInput, MonitorWaitInput
from firefly_forge.search_tools import SearchProviderError
from firefly_forge.secrets import contains_known_credential, redact_environment
from firefly_forge.tool_broker import ToolReconciliationRequired
from firefly_forge.work_duration_models import effective_work_timeout
from firefly_forge.worker_evidence_query import WorkerEvidenceReadInput
from firefly_forge.workspace_tools import ToolRuntimeContext
from firefly_forge.workspace_tools import WorkspaceTools as WorkspaceTools


class RuntimeReport(BaseModel):
    summary: str
    worker_feedback: WorkerFeedback | None = None
    qa_report: QAReport | None = None


async def execute_agentic(
    workspace: Path,
    brief: str,
    backend: str,
    frontend: str,
    emit: Callable[[str, str], Awaitable[None]],
    *,
    model: Any = None,
    allow_output_token_cap: bool = True,
    settings: dict[str, str] | None = None,
    runtime_context: ToolRuntimeContext | None = None,
) -> tuple[str, dict[str, Any]]:
    from pydantic_ai import PromptedOutput, ToolOutput
    from pydantic_ai.capabilities import AbstractCapability, ProcessHistory
    from pydantic_ai.messages import BinaryContent, ToolCallPart, ToolReturn, UserPromptPart
    from pydantic_ai.tools import RunContext, ToolDefinition
    from pydantic_ai.tools import Tool as PydanticTool
    from pydantic_ai.usage import UsageLimits
    from pyfly.agentic.agents import Agent
    from pyfly.agentic.config import AgenticConfig
    from pyfly.agentic.exceptions import ToolError
    from pyfly.agentic.models import ModelOptions
    from pyfly.agentic.tools import agent_tool as native_agent_tool

    from firefly_forge.agentic_checkpoint_invocation import current_checkpoint_invocation
    from firefly_forge.configuration import validate_checkpoints

    selected_settings = (
        settings
        if settings is not None
        else {
            name: os.environ[name]
            for name in ("FORGE_AGENTIC_CHECKPOINTS", "FORGE_BRIDGE_COMPLETION_RECOVERY")
            if name in os.environ
        }
    )
    validate_checkpoints(selected_settings, provider="bridge" if not allow_output_token_cap else "agentic")
    invocation = (
        current_checkpoint_invocation() if selected_settings.get("FORGE_AGENTIC_CHECKPOINTS") == "required" else None
    )
    if selected_settings.get("FORGE_AGENTIC_CHECKPOINTS") == "required" and invocation is None:
        raise ValueError("Required checkpoint execution needs a native checkpoint invocation")
    context = runtime_context or ToolRuntimeContext(workspace, workspace)
    tools = WorkspaceTools(context.scope_root, repository_root=context.repository_root)
    evidence_records: dict[str, dict[str, Any]] = {}
    observed_states: set[str] = set()
    expected_states: set[str] = set()
    actual_image_ids: set[str] = set()
    attached_ids: set[str] = set()
    approved_hashes: set[str] = set()
    approved_reference_ids: dict[str, str] = {}

    def safe_data(value: Any) -> Any:
        if isinstance(value, ToolReturn):
            return ToolReturn(safe_data(value.return_value), content=safe_data(value.content))
        if isinstance(value, str):
            return redact_secrets(redact_environment(value), context.credential_registry())
        if isinstance(value, dict):
            return {safe_data(key): safe_data(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe_data(item) for item in value]
        return value

    async def admit_tool_arguments(arguments: Any) -> None:
        if invocation is not None:
            await invocation.guard()
            invocation.remaining()
        if context.credential_admission is not None:
            await context.credential_admission(arguments)
        if invocation is not None:
            await invocation.guard()
        if contains_known_credential(arguments, context.credential_registry()):
            raise ValueError("Native tool arguments cannot contain configured credentials")

    def guarded(handler: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(handler)
        async def invoke(*args: Any, **kwargs: Any) -> Any:
            arguments = [
                [arg.model_dump(mode="json") if isinstance(arg, BaseModel) else arg for arg in args],
                {
                    key: value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                    for key, value in kwargs.items()
                },
            ]
            await admit_tool_arguments(arguments)
            try:
                result = await handler(*args, **kwargs)
                if context.credential_admission is not None:
                    public = result.return_value if isinstance(result, ToolReturn) else result
                    await context.credential_admission(public)
                return safe_data(result)
            except SearchProviderError as exc:
                message = safe_data(str(exc))
                if context.credential_admission is not None:
                    try:
                        await context.credential_admission(message)
                    except ValueError:
                        message = "Provider error details withheld by credential admission"
                raise SearchProviderError(exc.provider, exc.kind, message, status=exc.status) from None
            except (ValueError, OSError) as exc:
                if context.credential_admission is not None:
                    try:
                        await context.credential_admission(str(exc))
                    except ValueError:
                        raise ValueError("Native tool error details withheld by credential admission") from None
                raise ValueError(safe_data(str(exc))) from None

        return invoke

    async def source_manifest() -> dict[str, str]:
        return await tools.source_manifest()

    monitor_source_before = await source_manifest() if context.process_monitor is not None else {}

    async def start_process(request: MonitorStartInput) -> dict[str, Any]:
        if context.process_monitor is None:
            raise ValueError("Owned host process monitoring is not configured")
        return await context.process_monitor.start(request)

    async def process_status(monitor_id: str) -> dict[str, Any]:
        if context.process_monitor is None:
            raise ValueError("Owned host process monitoring is not configured")
        return await context.process_monitor.status(monitor_id)

    async def process_logs(monitor_id: str) -> dict[str, Any]:
        if context.process_monitor is None:
            raise ValueError("Owned host process monitoring is not configured")
        return await context.process_monitor.logs(monitor_id)

    async def wait_process(request: MonitorWaitInput) -> dict[str, Any]:
        if context.process_monitor is None:
            raise ValueError("Owned host process monitoring is not configured")
        return await context.process_monitor.wait(request)

    async def cancel_process(monitor_id: str) -> dict[str, Any]:
        if context.process_monitor is None:
            raise ValueError("Owned host process monitoring is not configured")
        return await context.process_monitor.cancel(monitor_id)

    async def run_command(argv: list[str], timeout: int | None) -> dict[str, Any]:
        from firefly_forge.sandbox_models import bounded_argv

        if context.command_executor is None:
            raise ValueError("Command execution is not configured")
        if not 1 <= len(argv) <= 64:
            raise ValueError("Command arguments exceed the tool bounds")
        bounded_argv(argv)
        before = await source_manifest()
        result = await context.command_executor(argv, timeout)
        after = await source_manifest()
        tools.files_written.update(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        return {"exit_code": result.exit_code, "output": result.output}

    async def execute_command(argv: list[str], timeout: int = 300) -> dict[str, Any]:
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("Command arguments or timeout exceed the tool bounds")
        return await run_command(argv, timeout)

    async def execute_command_v2(argv: list[str], timeout: StrictInt | None = None) -> dict[str, Any]:
        assert context.work_duration is not None
        return await run_command(argv, effective_work_timeout(context.work_duration.timeout_seconds, timeout))

    async def browser_check(
        path: str = "/", width: int = 1440, height: int = 900, theme: str = "light"
    ) -> dict[str, Any]:

        if context.browser_runner is None:
            raise ValueError("Browser execution is not configured")
        return await context.browser_runner(path, width, height, theme)

    async def profile_python(request: PythonProfileRequest) -> dict[str, Any]:

        if context.python_profile is None or context.command_executor is None:
            raise ValueError("Isolated Python profiling is not configured")
        return await context.python_profile(request)

    async def trace_python(request: PythonTraceRequest) -> dict[str, Any]:

        if context.python_trace is None or context.command_executor is None:
            raise ValueError("Isolated Python tracing is not configured")
        return await context.python_trace(request)

    async def profile_php(request: PHPProfileRequest) -> dict[str, Any]:

        if context.php_profile is None or context.command_executor is None:
            raise ValueError("Isolated PHP profiling is not configured")
        return await context.php_profile(request)

    async def browser_journey(plan: BrowserJourney) -> Any:

        if context.browser_journey is None:
            raise ValueError("Browser journey execution is not configured")
        resolved_reference = (
            await context.reference_resolver(plan)
            if plan.reference_id and context.reference_resolver is not None
            else None
        )
        evidence = await context.browser_journey(plan)
        expected_states.update(state.id for state in plan.expected_states)
        report = safe_data(evidence.report)
        images = list(evidence.images)
        actual_image_ids.update(image.metadata["evidence_id"] for image in images)
        observed_states.update(state["state_id"] for state in report.get("states", []) if state.get("success"))
        reference_status = "reference_not_reviewed"
        if resolved_reference is not None:
            reference, image = resolved_reference
            if reference.artifact_id != plan.reference_id:
                raise ValueError("Reference resolver returned a different approved artifact")
            if (reference.width, reference.height, reference.theme, reference.zoom) != (
                plan.width,
                plan.height,
                plan.theme,
                plan.zoom,
            ) or reference.state_id not in {image.metadata["state_id"] for image in images}:
                raise ValueError("Reference does not match the journey state, viewport, theme and zoom")
            if hashlib.sha256(image.data).hexdigest() != reference.sha256:
                raise ValueError("Resolved approved reference bytes differ from their frozen raster SHA256")
            images.append(image)
            approved_hashes.add(reference.sha256)
            approved_reference_ids[reference.sha256] = image.metadata["evidence_id"]
            reference_status = "matched_reference_attached"
        elif plan.reference_id and context.reference_loader is not None:
            candidate = next((item for item in context.qa_references if item["artifact_id"] == plan.reference_id), None)
            if candidate is not None:
                reference = QAReference.model_validate(candidate)
                if (reference.width, reference.height, reference.theme, reference.zoom) == (
                    plan.width,
                    plan.height,
                    plan.theme,
                    plan.zoom,
                ) and reference.state_id in {image.metadata["state_id"] for image in images}:
                    image = await context.reference_loader(reference.artifact_id)
                    if hashlib.sha256(image.data).hexdigest() != reference.sha256:
                        raise ValueError("Approved reference image bytes do not match the frozen SHA256")
                    images.append(image)
                    approved_hashes.add(reference.sha256)
                    approved_reference_ids[reference.sha256] = image.metadata["evidence_id"]
                    reference_status = "matched_reference_attached"
        if len(images) > 5 or sum(len(image.data) for image in images) > 6_000_000:
            raise ValueError("Browser image attachments exceeded the five-image or 6 MB request limit")
        vision = context.image_capability.get("status") == "verified"
        content: list[Any] = []
        for image in images:
            metadata = safe_data(image.metadata)
            if not image.data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Browser evidence must contain actual PNG image bytes")
            if hashlib.sha256(image.data).hexdigest() != metadata.get("screenshot_sha256"):
                raise ValueError("Browser screenshot does not match its evidence SHA256")
            identifier = metadata["evidence_id"]
            evidence_records[identifier] = metadata
            content.append(prompt_text("tools/image-evidence", metadata=json.dumps(metadata, separators=(",", ":"))))
            if vision:
                content.append(
                    BinaryContent(
                        image.data, media_type="image/png", identifier=identifier, vendor_metadata={"detail": "auto"}
                    )
                )
            else:
                content.append(prompt_text("tools/image-unavailable"))
        report.update(
            image_mode="vision" if vision else "dom_only",
            reference_status=reference_status if vision else "reference_not_reviewed",
        )
        return ToolReturn(report, content=content)

    async def image_history(messages: list[Any]) -> list[Any]:
        bounded = bounded_image_history(messages)
        attached_ids.clear()
        text = []
        for message in bounded:
            for part in message.parts:
                content = getattr(part, "content", None)
                arguments = getattr(part, "args", None)
                if isinstance(content, (str, dict)):
                    text.append(content)
                elif isinstance(content, list):
                    text.extend(item for item in content if isinstance(item, str))
                if isinstance(arguments, (str, dict)):
                    text.append(arguments)
                if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
                    attached_ids.update(item.identifier for item in part.content if isinstance(item, BinaryContent))
        if context.credential_admission is not None:
            await context.credential_admission(text)
        return bounded

    async def internet_search(
        query: str, max_results: int = 5, refresh: bool = False, refresh_request_id: str | None = None
    ) -> dict[str, Any]:

        if context.internet_search is None:
            raise ValueError("Internet search is not configured")
        if refresh or refresh_request_id is not None:
            if context.internet_search_refresh is None:
                raise ValueError("This captured runtime does not support explicit search refresh")
            return await context.internet_search_refresh(query, max_results, refresh, refresh_request_id)
        return await context.internet_search(query, max_results)

    async def knowledge_lookup(
        query: str, refresh: bool = False, refresh_request_id: str | None = None
    ) -> dict[str, Any]:

        if context.knowledge_lookup is None:
            raise ValueError("Knowledge lookup is not configured")
        return await context.knowledge_lookup(query, refresh, refresh_request_id)

    async def external_mcp(connector: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:

        if context.external_mcp is None:
            raise ValueError("External MCP execution is not configured")
        return await context.external_mcp(connector, tool, arguments)

    async def read_source(path: str) -> str:

        try:
            content = await tools.read_file(path)
            return "SHA256: " + hashlib.sha256(content.encode("utf-8")).hexdigest() + "\n" + content
        except (ValueError, FileNotFoundError, UnicodeDecodeError) as exc:
            return f"Unavailable file {path}: {exc}. Use list_files to find existing source paths."

    async def write_source(path: str, content: str) -> str:

        result = await tools.write_file(path, content)
        await emit("file", result)
        return result

    async def read_run_evidence(**arguments: Any) -> dict[str, Any]:
        try:
            request = WorkerEvidenceReadInput.model_validate(arguments)
        except ValidationError:
            return {"status": "invalid_query"}
        if context.read_run_evidence is None:
            raise ValueError("Retained run evidence is not configured")
        query = request.query.model_dump(mode="json", exclude_unset=True)
        query["limit"] = request.query.limit
        return await context.read_run_evidence(query)

    handlers: dict[str, Callable[..., Any]] = {
        "list_files": tools.list_files_page,
        "read_file": read_source,
        "write_file": write_source,
        "search_text": tools.search_text,
        "edit_lines": tools.edit_lines,
        "git_diff": tools.git_diff,
    }
    if context.read_run_evidence is not None:
        handlers["read_run_evidence"] = read_run_evidence
    if context.command_executor is not None:
        handlers["execute_command"] = execute_command if context.work_duration is None else execute_command_v2
    monitor_handlers: dict[str, Callable[..., Any]] = {
        "start_process": start_process,
        "process_status": process_status,
        "process_logs": process_logs,
        "wait_process": wait_process,
        "cancel_process": cancel_process,
    }
    monitor_grants = set(context.allowed_tools or ()) & monitor_handlers.keys()
    if monitor_grants:
        if context.process_monitor is None or "execute_command" not in (context.allowed_tools or ()):
            raise ValueError("Process monitoring is not configured or available within the granted runner")
        if invocation is not None:
            raise ValueError("Process monitoring is unavailable for required-checkpoint invocations")
        handlers.update(monitor_handlers)
    if context.browser_runner is not None:
        handlers["browser_check"] = browser_check
    if context.browser_journey is not None:
        handlers["browser_journey"] = browser_journey
    if context.internet_search is not None:
        handlers["internet_search"] = internet_search
    if context.external_mcp is not None:
        handlers["external_mcp"] = external_mcp
    if context.knowledge_lookup is not None:
        handlers["knowledge_lookup"] = knowledge_lookup
    if (
        context.python_profile is not None
        and context.command_executor is not None
        and {"execute_command", "profile_python"}.issubset(context.allowed_tools or ())
    ):
        handlers["profile_python"] = profile_python
    if (
        context.python_trace is not None
        and context.command_executor is not None
        and {"execute_command", "trace_python"}.issubset(context.allowed_tools or ())
    ):
        handlers["trace_python"] = trace_python
    if (
        context.php_profile is not None
        and context.command_executor is not None
        and {"execute_command", "profile_php"}.issubset(context.allowed_tools or ())
    ):
        handlers["profile_php"] = profile_php
    unknown = (context.allowed_tools or frozenset()) - {
        "list_files",
        "read_file",
        "write_file",
        "search_text",
        "edit_lines",
        "git_diff",
        "execute_command",
        "start_process",
        "process_status",
        "process_logs",
        "wait_process",
        "cancel_process",
        "browser_check",
        "browser_journey",
        "internet_search",
        "external_mcp",
        "knowledge_lookup",
        "read_run_evidence",
        "profile_python",
        "trace_python",
        "profile_php",
    }
    if unknown:
        raise ValueError("Native tool grant contains an unknown tool")
    if context.allowed_tools is not None:
        handlers = {name: handler for name, handler in handlers.items() if name in context.allowed_tools}

    settings = (
        settings
        if settings is not None
        else {
            name: os.environ[name]
            for name in ("FORGE_AGENTIC_MODEL", "FORGE_AGENTIC_MAX_TOKENS", "FORGE_PROVIDER_TIMEOUT")
            if name in os.environ
        }
    )
    selected_model = model or settings.get("FORGE_AGENTIC_MODEL", "openai-responses:gpt-6-luna")
    # Explicit modes keep the SDK request contract static across checkpointed turns.
    output_spec: Any = ToolOutput(RuntimeReport) if allow_output_token_cap else PromptedOutput(RuntimeReport)
    from contextlib import nullcontext

    from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary
    from firefly_forge.bridge_completion_scope import bridge_completion_scope

    boundary = (
        AgenticBoundary(
            lambda: {"files_written": sorted(tools.files_written)},
            restore=invocation.restored.checkpoint if invocation and invocation.restored else None,
        )
        if invocation is not None
        else None
    )
    if invocation is not None and invocation.restored is not None:
        tools.files_written.update(invocation.restored.checkpoint.state["files_written"])
    native_scope = (
        invocation.attach(boundary, brief, backend, frontend, context, settings)
        if invocation is not None and boundary is not None
        else None
    )

    def agent_tool(name: str, handler: Callable[..., Any]) -> Any:
        tool = native_agent_tool(
            name,
            description=prompt_text(
                "tools/" + name,
                version="1.0.1" if name in {"list_files", "search_text", "read_run_evidence"} else "1.0.0",
            ),
            auto_register=False,
        )(guarded(handler))
        if name == "read_run_evidence":
            # Ordinary tool results preserve checkpoint history without introducing SDK retry state.
            strict_tool = PydanticTool.from_schema(
                tool.execute,
                name=name,
                description=tool.description,
                json_schema=WorkerEvidenceReadInput.model_json_schema(),
            )

            async def retained_return_schema(ctx: RunContext[Any], definition: ToolDefinition) -> ToolDefinition:
                from dataclasses import replace

                return replace(definition, return_schema={"type": "object", "additionalProperties": True})

            strict_tool.prepare = retained_return_schema
            return strict_tool
        return tool

    class EvidenceQueryValidation(AbstractCapability[Any]):
        async def before_tool_validate(
            self,
            ctx: RunContext[Any],
            *,
            call: ToolCallPart,
            tool_def: ToolDefinition,
            args: str | dict[str, Any],
        ) -> str | dict[str, Any]:
            if call.tool_name == tool_def.name == "read_run_evidence" and isinstance(args, str):
                try:
                    TypeAdapter(dict[str, Any]).validate_json(args, strict=True)
                except ValidationError:
                    await admit_tool_arguments(args)
                    return {"query": None}
            return args

    from firefly_forge.agentic_tool_custody import (
        EFFECTS,
        AgenticToolCapability,
        current_agentic_tool_custody,
    )

    custody = current_agentic_tool_custody()
    captured_tools = frozenset(handlers) & EFFECTS.keys()
    agent: Agent[None, RuntimeReport] = Agent(
        name="forge-builder",
        model=selected_model,
        instructions=build_instruction("", backend, frontend)
        + ("\n" + qa_instructions() if "browser_journey" in handlers else "")
        + (
            prompt_text(
                "implementation/reference-descriptors",
                references=json.dumps(safe_data(context.qa_references), separators=(",", ":")),
            )
            if context.qa_references
            else ""
        )
        + prompt_text("implementation/tool-rules"),
        output_type=output_spec,
        auto_register=False,
        config=AgenticConfig(default_temperature=None, rate_limit_max_retries=0),
        model_options=(
            ModelOptions(max_tokens=int(settings.get("FORGE_AGENTIC_MAX_TOKENS", "16384")))
            if allow_output_token_cap
            else None
        ),
        tools=[agent_tool(name, handler) for name, handler in handlers.items()],
        capabilities=[
            ProcessHistory(image_history),
            *([AgenticToolCapability(custody, captured_tools)] if custody is not None else []),
            *([EvidenceQueryValidation()] if "read_run_evidence" in handlers else []),
            *([boundary] if boundary is not None else []),
        ],
    )
    await emit("provider", "Firefly Agentic is generating source with confined workspace tools")
    try:
        with (
            boundary.invocation() if boundary is not None else nullcontext(),
            bridge_completion_scope(native_scope) if native_scope is not None else nullcontext(),
        ):
            if invocation is not None and invocation.restored is not None:
                result = await agent.run(
                    **invocation.restored.checkpoint.resume_kwargs(), timeout=invocation.remaining()
                )
            else:
                result = await agent.run(
                    brief,
                    timeout=invocation.remaining()
                    if invocation is not None
                    else context.work_duration.timeout_seconds
                    if context.work_duration is not None
                    else float(settings.get("FORGE_PROVIDER_TIMEOUT", "1800")),
                    usage_limits=UsageLimits(request_limit=100, tool_calls_limit=300),
                )
    except ToolError as exc:
        # Native tool wrappers retain the typed cause; text cannot authorize a supervisor transition.
        cause: BaseException = exc
        seen: set[int] = set()
        while isinstance(cause, ToolError) and cause.__cause__ is not None and id(cause) not in seen:
            seen.add(id(cause))
            cause = cause.__cause__
            if isinstance(cause, (SearchProviderError, ToolReconciliationRequired)):
                raise cause from None
        raise
    finally:
        if context.process_monitor is not None:
            await asyncio.shield(context.process_monitor.close())
            after = await source_manifest()
            tools.files_written.update(
                path
                for path in monitor_source_before.keys() | after.keys()
                if monitor_source_before.get(path) != after.get(path)
            )
    if custody is not None:
        await custody.assert_effects_settled()
    if context.credential_admission is not None:
        await context.credential_admission(result.output.model_dump(mode="json"))
    if invocation is not None:
        await invocation.guard()
        invocation.remaining()
    feedback = result.output.worker_feedback
    retained_source = None
    if not tools.files_written and (feedback is None or feedback.disposition not in {"blocked", "clarification"}):
        if invocation is None:
            raise RuntimeError("Firefly Agentic returned without writing any project source")
        from firefly_forge.retained_verified_source import prepare_retained_source

        try:
            retained_source = await prepare_retained_source(
                invocation.store, invocation.journal, await invocation.store.get_run(invocation.run["id"]), workspace
            )
        except ValueError as error:
            raise RuntimeError("Firefly Agentic returned without writing any project source") from error
    usage = result.usage
    recorded_usage = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "requests": usage.requests,
        "files_written": sorted(tools.files_written),
    }
    if retained_source is not None:
        recorded_usage["retained_source"] = retained_source.proof.model_dump(mode="json")
    if feedback is not None:
        recorded_usage["worker_feedback"] = feedback.model_dump(mode="json")
    if result.output.qa_report is not None:
        qa = result.output.qa_report
        if not set(qa.reviewed_states) <= observed_states:
            raise ValueError("QA report claims states without successful actual browser evidence")
        if not expected_states - observed_states <= set(qa.not_reviewed):
            raise ValueError("QA report omits declared states not reached by the actual browser journey")
        if qa.mode == "vision" and not attached_ids:
            raise ValueError("QA report claims image inspection without verified attached images")
        if qa.mode == "vision" and not set(qa.reviewed_states) <= {
            evidence_records[identifier]["state_id"] for identifier in attached_ids & actual_image_ids
        }:
            raise ValueError("QA report claims visual inspection of a state without its attached actual screenshot")
        if not set(qa.reference_sha256) <= approved_hashes or (qa.reference_sha256 and qa.mode != "vision"):
            raise ValueError("QA report claims an unmatched approved reference")
        if any(approved_reference_ids[sha] not in attached_ids for sha in qa.reference_sha256):
            raise ValueError("QA report claims current inspection of a reference no longer attached")
        for finding in qa.findings:
            if not set(finding.evidence_ids) <= evidence_records.keys():
                raise ValueError("QA finding refers to unknown evidence")
            if qa.mode == "vision" and not set(finding.evidence_ids) <= attached_ids:
                raise ValueError("QA finding claims current visual inspection of images no longer attached")
            if any(
                evidence_records[identifier].get("state_id") != finding.state_id
                or evidence_records[identifier].get("journey_id") != finding.journey_id
                for identifier in finding.evidence_ids
            ):
                raise ValueError("QA finding does not match its actual journey and state evidence")
        recorded_usage["qa_report"] = qa.model_dump(mode="json")
        recorded_usage["browser_evidence"] = list(evidence_records.values())
    recorded_usage["agentic_tool_custody"] = {
        "version": 1,
        "status": "captured" if custody is not None else "unavailable",
        "captured_tools": sorted(captured_tools) if custody is not None else [],
        "uncaptured_tools": sorted(set(handlers) - captured_tools) if custody is not None else sorted(handlers),
        "reason": None if custody is not None else "native_factory_invocation_missing",
    }
    recorded_usage["runtime_report"] = result.output.model_dump(mode="json")
    return result.output.summary, recorded_usage
