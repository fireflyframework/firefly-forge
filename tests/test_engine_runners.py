# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import sys

from test_role_execution import fixture_run

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.browser_runner import BrowserRunner
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RunInput
from firefly_forge.providers import AgentResult, ConfiguredProvider
from firefly_forge.runner_configuration import RunnerConfiguration, RunnerDefaults
from firefly_forge.sandbox import SandboxExecutor, SandboxReconciliationRequired
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.search_tools import SearchTools
from firefly_forge.workspace_tools import WorkspaceTools


async def test_journey_tool_is_wired_only_for_frozen_grant_with_real_callback(store, tmp_path):
    from firefly_forge.qa import BrowserJourney, JourneyEvidence
    from firefly_forge.workspace_tools import tool_catalog

    initial = await fixture_run(store, tmp_path)
    run = await store.create_run(
        RunInput(
            repository_id=initial["repository_id"],
            brief="Review the stock journey",
            agent="bridge",
            frontend="none",
            skills=[],
            roles=ProjectRoles(allowed_tools={"implementer": ["browser_journey"]}),
            commands=[Command(argv=[sys.executable, "-c", "print('checked')"])],
        ),
        prepared_tools=tool_catalog({"browser_journey": True}),
    )
    calls = []

    class Browser:
        async def journey(self, run_id, scope, plan, *, task_id):
            calls.append((run_id, scope, plan.journey_id, task_id))
            return JourneyEvidence({"states": []}, [])

    class Worker(ConfiguredProvider):
        async def execute(self, workspace, *args):
            assert self.runtime_context.browser_journey is not None
            assert self.runtime_context.browser_runner is None
            assert self.runtime_context.image_capability["status"] == "unverified"
            plan = BrowserJourney(
                journey_id="stock",
                name="Review stock",
                expected_states=[{"id": "ready", "expected": "Stock is visible"}],
            )
            await self.runtime_context.browser_journey(plan)
            await WorkspaceTools(workspace).write_file("product.py", "stock = 5\n")
            return AgentResult("Journey inspected with DOM evidence", "fixture")

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=Journal(store))
    engine.browser = Browser()
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded", result.get("error")
    assert len(calls) == 1 and calls[0][0] == run["id"] and calls[0][2:] == ("stock", "implement")


async def test_native_worker_uses_frozen_source_grants_and_runtime_scope(store, tmp_path):
    run = await fixture_run(store, tmp_path)
    await store.update(run["id"], agent="bridge")

    class Worker(ConfiguredProvider):
        async def execute(self, workspace, *args):
            assert self.runtime_context is not None
            assert self.runtime_context.scope_root == workspace
            assert self.runtime_context.repository_root == workspace
            assert "write_file" in self.runtime_context.allowed_tools
            assert "internet_search" not in self.runtime_context.allowed_tools
            tools = WorkspaceTools(self.runtime_context.scope_root)
            await tools.write_file("product.py", "stock = 5\n")
            return AgentResult("Applied frozen scoped source tools", "fixture")

    journal = Journal(store)
    await Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal).execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "succeeded"


async def test_worker_visual_findings_block_delivery_and_remain_persisted(store, tmp_path):
    run = await fixture_run(store, tmp_path)

    class Worker:
        async def execute(self, workspace, *args):
            (workspace / "product.py").write_text("stock = 5\n")
            return AgentResult(
                "Source implemented; button is covered",
                "fixture",
                {
                    "qa_report": {
                        "mode": "dom_only",
                        "summary": "Primary action is covered by the footer",
                        "findings": [
                            {
                                "severity": "high",
                                "journey_id": "stock",
                                "state_id": "ready",
                                "path": "/",
                                "expected": "Reserve action remains reachable",
                                "observed": "Footer covers the action",
                                "evidence_ids": ["actual-state"],
                            }
                        ],
                    }
                },
            )

    journal = Journal(store)
    await Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed", result.get("error")
    assert "Visual QA" in result["error"]
    assert result["usage"]["qa_report"]["findings"][0]["severity"] == "high"
    assert journal.verify_chain(await journal.entries(run["id"]))


async def test_verification_obeys_frozen_total_runner_command_budget(store, tmp_path):
    initial = await fixture_run(store, tmp_path)
    run = await store.create_run(
        RunInput(
            repository_id=initial["repository_id"],
            brief="Verify the retained stock source",
            operation="test",
            skills=[],
            frontend="none",
            commands=[Command(argv=[sys.executable, "-c", "print('checked')"]) for _ in range(2)],
            sandbox=SandboxInput(command_limit=1),
        )
    )
    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", journal=journal)
    await store.acquire_ownership()
    engine.sandbox = SandboxExecutor(store, journal)
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and "budget" in result["error"]
    executions = await journal.sandbox_executions(run["id"])
    assert len(executions) == 1
    assert executions[0]["effects"]["start"]["evidence"]["exit_code"] == 0
    assert journal.verify_chain(await journal.entries(run["id"]))


async def test_unknown_runner_outcome_requires_reconciliation_instead_of_failure(store, tmp_path):
    initial = await fixture_run(store, tmp_path)
    run = await store.create_run(
        RunInput(
            repository_id=initial["repository_id"],
            brief="Verify stock",
            operation="test",
            skills=[],
            commands=[Command(argv=[sys.executable, "-c", "print('check')"])],
        )
    )

    class LostReceipt:
        async def execute(self, *args, **kwargs):
            raise SandboxReconciliationRequired("Runner start receipt is missing")

    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", journal=journal)
    engine.sandbox = LostReceipt()
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "needs_reconciliation"
    assert "receipt" in result["error"]


async def test_opted_in_command_tool_is_frozen_and_uses_the_actual_stage_runner(store, tmp_path):
    initial = await fixture_run(store, tmp_path)
    journal = Journal(store)
    executor = SandboxExecutor(store, journal)
    search = SearchTools(tmp_path / "search")
    browser = BrowserRunner(executor, tmp_path / "browser")
    store.runners = RunnerConfiguration(store, executor, search, browser)
    settings = await store.runners.configure_repository(
        initial["repository_id"], RunnerDefaults(sandbox=SandboxInput(command_limit=3))
    )
    run = await store.create_run(
        RunInput(
            repository_id=initial["repository_id"],
            brief="Set stock to 5",
            agent="bridge",
            frontend="none",
            skills=[],
            roles=ProjectRoles(allowed_tools={"implementer": ["read_file", "write_file", "execute_command"]}),
            commands=[Command(argv=[sys.executable, "-c", "from product import stock; assert stock == 5"])],
        )
    )
    assert run["runner_config_revision"] == settings["revision"]
    await store.runners.configure_repository(
        initial["repository_id"], RunnerDefaults(sandbox=SandboxInput(command_limit=1))
    )

    class Worker(ConfiguredProvider):
        async def execute(self, workspace, *args):
            assert self.runtime_context.command_executor is not None
            result = await self.runtime_context.command_executor(
                [sys.executable, "-c", "from pathlib import Path; Path('product.py').write_text('stock = 5\\n')"], 30
            )
            assert result.exit_code == 0
            return AgentResult("Applied selected command tool", "fixture")

    await store.acquire_ownership()
    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
    engine.sandbox = executor
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "succeeded"
    assert {row["task_id"] for row in await journal.sandbox_executions(run["id"])} == {"implement", "verify"}
    assert journal.verify_chain(await journal.entries(run["id"]))
