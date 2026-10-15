# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real failed commands must bound only subsequent receipt-backed coding proposals."""

import asyncio
import sys

import pytest
from test_execution import seed_repository

from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.learning_configuration import configure_learning
from firefly_forge.learning_models import LearningPolicy
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.process import checked
from firefly_forge.providers import AgentResult


async def failed_run(store, tmp_path, *, limit=1, enabled=True, operation="build"):
    source = await seed_repository(tmp_path / "source")
    (source / "pyproject.toml").write_text(
        '[project]\nname="owned-stock"\nversion="0.1.0"\ndependencies=["pyfly==26.10.6"]\n'
    )
    (source / "product.py").write_text("stock = 3\n")
    await checked(["git", "add", "."], source)
    await checked(["git", "commit", "-m", "Declare owned source and framework"], source)
    configure_learning(store.configuration, LearningPolicy(max_repeated_attempts=limit, enabled=enabled))
    repository = await store.add_repository(RepositoryInput(name="Owned stock", url=str(source)))
    return await store.create_run(
        RunInput(
            repository_id=repository["id"],
            operation=operation,
            brief="Set stock to five without changing its independent check",
            skills=[],
            frontend="none",
            commands=[Command(argv=[sys.executable, "-c", "from product import stock; assert stock == 5"])],
        )
    )


class NoProgressWorker:
    def __init__(self):
        self.dispatched = 0

    async def execute(self, workspace, *args):
        self.dispatched += 1
        (workspace / "product.py").write_text("stock = 4\n")
        return AgentResult("Owned deterministic incomplete stock implementation", "fixture")


async def test_frozen_limit_one_holds_next_coding_proposal_after_real_failed_verification(store, tmp_path):
    # Removing the proposal boundary must redispatch the coding edge after this exact native failure.
    run = await failed_run(store, tmp_path)
    worker = NoProgressWorker()
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal)
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    entries = await journal.entries(run["id"])
    assert worker.dispatched == result["implementation_rounds"] == 1
    assert result["status"] == "failed"
    receipts = [entry for entry in entries if entry["kind"] == "receipt" and entry["task_id"] == "verify"]
    assert len(receipts) == 1 and receipts[0]["payload"]["checks"][0]["exit_code"] == 1
    decisions = [entry for entry in entries if entry["kind"] == "repair_proposal"]
    assert len(decisions) == 1, result.get("error")
    assert decisions[0]["payload"]["status"] == "held"
    assert decisions[0]["payload"]["verified_failures"] == decisions[0]["payload"]["effective_limit"] == 1
    assert decisions[0]["payload"]["verification_receipt"]["digest"] == receipts[0]["digest"]
    assert receipts[0]["sequence"] < decisions[0]["sequence"]
    assert Journal.verify_chain(entries)
    assert not any(entry["task_id"] == "review" and entry["kind"] == "receipt" for entry in entries)


@pytest.mark.parametrize("legacy", [False, True])
async def test_policy_off_or_missing_snapshot_preserves_existing_two_round_safety_stop(store, tmp_path, legacy):
    run = await failed_run(store, tmp_path, enabled=False)
    if legacy:
        await store.update(run["id"], learning_snapshot=None)
    worker = NoProgressWorker()
    journal = Journal(store)
    await Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert worker.dispatched == result["implementation_rounds"] == 2
    assert result["status"] == "failed" and "No progress" in result["error"]
    entries = await journal.entries(run["id"])
    decision = next(e["payload"] for e in entries if e["kind"] == "repair_proposal")
    assert decision["status"] == "not_applied" and decision["verified_failures"] == 0
    assert len([e for e in entries if e["kind"] == "receipt" and e["task_id"] == "verify"]) == 2


async def test_configured_three_never_relaxes_existing_two_round_no_progress_stop(store, tmp_path):
    run = await failed_run(store, tmp_path, limit=3)
    worker = NoProgressWorker()
    journal = Journal(store)
    await Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert worker.dispatched == 2 and "No progress" in result["error"]
    decision = next(e["payload"] for e in await journal.entries(run["id"]) if e["kind"] == "repair_proposal")
    assert decision["effective_limit"] == 2 and decision["verified_failures"] == 1


async def test_unknown_external_intent_remains_reconciliation_before_any_negative_proposal(store, tmp_path):
    run = await failed_run(store, tmp_path)
    worker = NoProgressWorker()
    journal = Journal(store)

    class UnknownExternalEffect(Engine):
        async def _repair_proposal(self, identifier, workspace, secrets, next_attempt):
            await self.journal.intent(identifier, "publish-owned-fixture", payload={"effect": "unknown"})
            await super()._repair_proposal(identifier, workspace, secrets, next_attempt)

    await UnknownExternalEffect(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    result = await store.get_run(run["id"])
    entries = await journal.entries(run["id"])
    assert worker.dispatched == 1
    assert result["status"] == "needs_reconciliation"
    assert not any(e["kind"] == "repair_proposal" and e["payload"]["status"] == "held" for e in entries)
    assert any(e["kind"] == "receipt" and e["task_id"] == "verify" for e in entries)


async def test_available_proposal_retry_after_stop_counts_actual_verification_once(store, tmp_path):
    run = await failed_run(store, tmp_path, limit=2)
    journal = Journal(store)

    class RepairingWorker(NoProgressWorker):
        async def execute(self, workspace, *args):
            result = await super().execute(workspace, *args)
            if self.dispatched == 2:
                (workspace / "product.py").write_text("stock = 5\n")
            return result

    class LostProposalResponse(Engine):
        interrupted = False

        async def _repair_proposal(self, *args):
            await super()._repair_proposal(*args)
            if not self.interrupted:
                self.interrupted = self.stopping = True
                raise asyncio.CancelledError()

    worker = RepairingWorker()
    engine = LostProposalResponse(store, tmp_path / "jobs", lambda _: worker, journal=journal)
    with pytest.raises(asyncio.CancelledError):
        await engine.execute_run(run["id"])
    await journal.recover()
    await journal.resume(run["id"])
    engine.stopping = False
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded", result.get("error")
    assert worker.dispatched == 2
    entries = await journal.entries(run["id"])
    decisions = [e for e in entries if e["kind"] == "repair_proposal"]
    assert len(decisions) == 1 and decisions[0]["payload"]["verified_failures"] == 1
    assert [
        e["payload"]["checks"][0]["exit_code"] for e in entries if e["kind"] == "receipt" and e["task_id"] == "verify"
    ] == [1, 0]
    assert Journal.verify_chain(entries)


@pytest.mark.parametrize("changed", ["source", "declared_dependency"])
async def test_changed_owned_context_does_not_reuse_old_failure_cap_and_still_runs_fresh_qa(store, tmp_path, changed):
    run = await failed_run(store, tmp_path)
    journal = Journal(store)

    class RepairingWorker(NoProgressWorker):
        async def execute(self, workspace, *args):
            result = await super().execute(workspace, *args)
            if self.dispatched == 2:
                (workspace / "product.py").write_text("stock = 5\n")
            return result

    class ChangedContext(Engine):
        async def _repair_proposal(self, identifier, workspace, secrets, next_attempt):
            if changed == "source":
                (workspace / "README.md").write_text("Owned changed source assumption\n")
            else:
                path = workspace / "pyproject.toml"
                path.write_text(path.read_text().replace("26.10.6", "26.10.5"))
            await super()._repair_proposal(identifier, workspace, secrets, next_attempt)

    worker = RepairingWorker()
    await ChangedContext(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded", result.get("error")
    assert worker.dispatched == 2
    entries = await journal.entries(run["id"])
    decision = next(e["payload"] for e in entries if e["kind"] == "repair_proposal")
    assert decision["status"] == "not_applied" and decision["verified_failures"] == 0
    assert [
        e["payload"]["checks"][0]["exit_code"] for e in entries if e["kind"] == "receipt" and e["task_id"] == "verify"
    ] == [1, 0]


async def test_unobserved_framework_version_cannot_establish_repair_cap(store, tmp_path):
    run = await failed_run(store, tmp_path)

    class UndeclaredWorker(NoProgressWorker):
        async def execute(self, workspace, *args):
            result = await super().execute(workspace, *args)
            (workspace / "pyproject.toml").write_text('[project]\nname="owned-stock"\nversion="0.1.0"\n')
            return result

    worker = UndeclaredWorker()
    journal = Journal(store)
    await Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert worker.dispatched == 2 and "No progress" in result["error"]
    decision = next(e["payload"] for e in await journal.entries(run["id"]) if e["kind"] == "repair_proposal")
    assert decision["status"] == "not_applied" and decision["reason"] == "version_drift"


@pytest.mark.parametrize("kind", ["rate_limit", "quota", "authentication", "transient"])
async def test_typed_provider_operational_failure_is_paused_without_negative_proposal(store, tmp_path, kind):
    from firefly_forge.providers import ProviderLimitError

    run = await failed_run(store, tmp_path)
    journal = Journal(store)

    class OperationalWorker:
        async def execute(self, *args):
            raise ProviderLimitError("Owned operational fixture", kind=kind, retry_after=1)

    await Engine(store, tmp_path / "jobs", lambda _: OperationalWorker(), journal=journal).execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "paused"
    entries = await journal.entries(run["id"])
    assert not any(
        e["kind"] == "repair_proposal" or (e["kind"] == "receipt" and e["task_id"] == "verify") for e in entries
    )


async def test_actual_check_timeout_without_completion_receipt_does_not_create_negative_proposal(store, tmp_path):
    run = await failed_run(store, tmp_path)
    await store.update(
        run["id"], commands=[{"argv": [sys.executable, "-c", "import time; time.sleep(5)"], "timeout": 1}]
    )
    worker = NoProgressWorker()
    journal = Journal(store)
    await Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    assert worker.dispatched == 1
    assert not any(
        e["kind"] == "repair_proposal" or (e["kind"] == "receipt" and e["task_id"] == "verify")
        for e in await journal.entries(run["id"])
    )


async def test_known_operational_retry_reuses_proposal_without_counting_failed_provider_dispatch(store, tmp_path):
    from firefly_forge.providers import ProviderLimitError

    run = await failed_run(store, tmp_path, limit=2)
    journal = Journal(store)

    class RateLimitedRepair:
        dispatched = 0

        async def execute(self, workspace, *args):
            self.dispatched += 1
            if self.dispatched == 2:
                raise ProviderLimitError("Owned temporary repair rate limit", kind="rate_limit", retry_after=1)
            (workspace / "product.py").write_text("stock = 4\n" if self.dispatched == 1 else "stock = 5\n")
            return AgentResult("Owned stock repair", "fixture")

    worker = RateLimitedRepair()
    engine = Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal)
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "paused"
    await journal.resume(run["id"])
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded", result.get("error")
    assert worker.dispatched == 3 and result["implementation_rounds"] == 2
    entries = await journal.entries(run["id"])
    decisions = [e for e in entries if e["kind"] == "repair_proposal"]
    assert len(decisions) == 1 and decisions[0]["payload"]["verified_failures"] == 1
    assert [
        e["payload"]["checks"][0]["exit_code"] for e in entries if e["kind"] == "receipt" and e["task_id"] == "verify"
    ] == [1, 0]


async def test_unsupported_large_dependency_evidence_never_loses_actual_failed_qa_receipt(store, tmp_path):
    run = await failed_run(store, tmp_path)
    journal = Journal(store)

    class LargeDeclarationWorker(NoProgressWorker):
        async def execute(self, workspace, *args):
            result = await super().execute(workspace, *args)
            path = workspace / "pyproject.toml"
            path.write_text(
                '[project]\nname="owned-stock"\nversion="0.1.0"\ndependencies=["pyfly==26.10.6"]\n#' + "x" * 1_000_001
            )
            return result

    worker = LargeDeclarationWorker()
    await Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    result = await store.get_run(run["id"])
    entries = await journal.entries(run["id"])
    assert worker.dispatched == 2 and "No progress" in result["error"]
    assert len([e for e in entries if e["kind"] == "receipt" and e["task_id"] == "verify"]) == 2
    assert not any(e["kind"] == "repair_proposal" and e["payload"]["status"] == "held" for e in entries)


async def test_corrupted_verification_read_cannot_establish_repair_cap_and_native_records_remain_intact(
    store, tmp_path
):
    run = await failed_run(store, tmp_path)

    class CorruptReadJournal(Journal):
        corrupt = False

        async def entries(self, run_id):
            entries = await super().entries(run_id)
            if self.corrupt:
                receipt = next(e for e in reversed(entries) if e["kind"] == "receipt" and e["task_id"] == "verify")
                receipt["payload"]["source_tree"] = "f" * 40
            return entries

    journal = CorruptReadJournal(store)
    worker = NoProgressWorker()

    class CorruptRead(Engine):
        async def _repair_proposal(self, *args):
            self.journal.corrupt = True
            await super()._repair_proposal(*args)

    await CorruptRead(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "needs_reconciliation", result.get("error")
    assert worker.dispatched == 1
    journal.corrupt = False
    entries = await journal.entries(run["id"])
    assert Journal.verify_chain(entries)
    assert not any(e["kind"] == "repair_proposal" for e in entries)


async def test_durable_held_proposal_survives_lost_response_without_dispatch_or_duplicate_on_resume(store, tmp_path):
    run = await failed_run(store, tmp_path)
    journal = Journal(store)
    worker = NoProgressWorker()

    class LostHeldResponse(Engine):
        async def _repair_proposal(self, *args):
            try:
                await super()._repair_proposal(*args)
            except ValueError:
                self.stopping = True
                raise asyncio.CancelledError() from None

    engine = LostHeldResponse(store, tmp_path / "jobs", lambda _: worker, journal=journal)
    with pytest.raises(asyncio.CancelledError):
        await engine.execute_run(run["id"])
    await journal.recover()
    await journal.resume(run["id"])
    engine.stopping = False
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and worker.dispatched == 1
    assert "proposal cap is exhausted" in result["error"]
    decisions = [e for e in await journal.entries(run["id"]) if e["kind"] == "repair_proposal"]
    assert len(decisions) == 1 and decisions[0]["payload"]["status"] == "held"


async def test_every_independent_check_finishes_before_cap_holds_the_next_coding_proposal(store, tmp_path):
    run = await failed_run(store, tmp_path)
    marker = tmp_path / "owned-diagnostic-marker"
    await store.update(
        run["id"],
        commands=[
            {"argv": [sys.executable, "-c", "from product import stock; assert stock == 5"], "timeout": 30},
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('checked')",
                ],
                "timeout": 30,
            },
        ],
    )
    journal = Journal(store)
    worker = NoProgressWorker()
    await Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    assert marker.read_text() == "checked" and worker.dispatched == 1
    entries = await journal.entries(run["id"])
    qa = next(e for e in entries if e["kind"] == "receipt" and e["task_id"] == "verify")
    assert [check["exit_code"] for check in qa["payload"]["checks"]] == [1, 0]
    assert next(e for e in entries if e["kind"] == "repair_proposal")["sequence"] > qa["sequence"]


async def test_test_only_failure_retains_independent_qa_without_any_coding_proposal(store, tmp_path):
    run = await failed_run(store, tmp_path, operation="test")
    worker = NoProgressWorker()
    journal = Journal(store)
    await Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal).execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and worker.dispatched == 0
    entries = await journal.entries(run["id"])
    assert not any(e["kind"] == "repair_proposal" for e in entries)
    qa = next(e for e in entries if e["kind"] == "receipt" and e["task_id"] == "verify")
    assert qa["payload"]["checks"][0]["exit_code"] == 1


async def test_native_firefly_function_model_worker_is_not_reinvoked_after_verified_failure_cap(store, tmp_path):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.providers import ConfiguredProvider

    run = await failed_run(store, tmp_path)
    journal = Journal(store)
    requests = []

    def model(messages, info):
        requests.append(messages)
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == "write_file" for p in returned):
            return ModelResponse(parts=[ToolCallPart("write_file", {"path": "product.py", "content": "stock = 4\n"})])
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Owned incomplete stock implementation"})]
        )

    class NativeWorker(ConfiguredProvider):
        async def execute(self, workspace, brief, backend, frontend, emit):
            summary, usage = await execute_agentic(
                workspace,
                brief,
                backend,
                frontend,
                emit,
                model=FunctionModel(model),
                runtime_context=self.runtime_context,
            )
            return AgentResult(summary, "native-function-fixture", usage)

    worker = NativeWorker()
    engine = Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal)
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and result["implementation_rounds"] == 1
    assert len(requests) == 2
    assert (engine.workspaces / run["id"] / "product.py").read_text() == "stock = 4\n"
    entries = await journal.entries(run["id"])
    qa = next(e for e in entries if e["kind"] == "receipt" and e["task_id"] == "verify")
    assert qa["payload"]["checks"][0]["exit_code"] == 1
    assert next(e for e in entries if e["kind"] == "repair_proposal")["payload"]["status"] == "held"
