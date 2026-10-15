# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json
import sys

import pytest
from test_execution import seed_repository

from firefly_forge.assurance import Assurance, ReviewReport, progress_record
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.providers import AgentResult


@pytest.mark.parametrize(
    "value",
    [
        {
            "verdict": "approved",
            "summary": "Ok",
            "findings": [{"severity": "high", "summary": "Broken", "evidence": "quote"}],
        },
        {"verdict": "changes_required", "summary": "Fix it", "findings": []},
        {
            "verdict": "approved",
            "summary": "Ok",
            "findings": [{"severity": "low", "summary": "Advice", "evidence": "quote"}] * 31,
        },
    ],
)
def test_strict_review_refuses_inconsistent_or_unbounded_findings(value):
    with pytest.raises(ValueError):
        ReviewReport.model_validate(value)


def test_progress_guard_is_based_on_actual_tree_and_failing_checks():
    failures = [{"argv": ["make", "check"], "exit_code": 1, "output": "Failure at 10:32"}]
    first = progress_record([], 1, "a" * 40, failures)
    with pytest.raises(ValueError, match="No progress"):
        progress_record(first, 2, "a" * 40, [{**failures[0], "output": "Failure at 10:33"}])
    assert len(progress_record(first, 2, "b" * 40, failures)) == 2


async def generated_run(store, tmp_path, review_agent="codex"):
    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Inventory", url=str(source)))
    return await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="Build inventory",
            assurance={"review_agent": review_agent},
            skills=[],
            commands=[Command(argv=[sys.executable, "-c", "from product import value; assert value == 7"])],
        )
    )


class Builder:
    async def execute(self, workspace, brief, backend, frontend, emit):
        (workspace / "product.py").write_text("value = 7\n")
        return AgentResult("Built", "fixture")


class Reviewer:
    calls = 0

    async def execute(self, workspace, brief, backend, frontend, emit):
        self.calls += 1
        assert (workspace / "source/product.py").read_text() == "value = 7\n"
        (workspace / "review.json").write_text(
            json.dumps({"verdict": "approved", "summary": "Verified source reviewed", "findings": []})
        )
        return AgentResult("Review completed", "fixture")


async def test_independent_review_runs_on_copy_and_has_write_ahead_receipt(store, tmp_path):
    run = await generated_run(store, tmp_path)
    journal = Journal(store)
    reviewer = Reviewer()
    engine = Engine(store, tmp_path / "jobs", lambda _: Builder(), journal=journal)
    engine.assurance = Assurance(store, tmp_path / "reviews", journal=journal, provider_factory=lambda _: reviewer)
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded", result.get("error")
    assert result["review"]["status"] == "approved" and reviewer.calls == 1
    assert not (engine.workspaces / run["id"] / "review.json").exists()
    entries = await journal.entries(run["id"])
    assert any(e["task_id"] == "review" and e["kind"] == "receipt" for e in entries)


async def test_reviewer_cannot_mutate_reviewed_source_or_deliver_original_changes(store, tmp_path):
    run = await generated_run(store, tmp_path)

    class MutatingReviewer(Reviewer):
        async def execute(self, workspace, *args):
            result = await super().execute(workspace, *args)
            (workspace / "source/product.py").write_text("value = 99\n")
            return result

    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: Builder(), journal=journal)
    engine.assurance = Assurance(
        store, tmp_path / "reviews", journal=journal, provider_factory=lambda _: MutatingReviewer()
    )
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and not result["artifacts"]
    assert "review" in result["error"].lower()
    assert (engine.workspaces / run["id"] / "product.py").read_text() == "value = 7\n"


async def test_unknown_review_outcome_is_not_replayed_on_resume(store, tmp_path):
    run = await generated_run(store, tmp_path)

    class InterruptedReviewer(Reviewer):
        async def execute(self, *args):
            self.calls += 1
            raise RuntimeError("Lost transport response")

    reviewer = InterruptedReviewer()
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: Builder(), journal=journal)
    engine.assurance = Assurance(store, tmp_path / "reviews", journal=journal, provider_factory=lambda _: reviewer)
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "needs_reconciliation"
    await journal.recover()
    await journal.resume(run["id"])
    await engine.execute_run(run["id"])
    assert reviewer.calls == 1
    assert (await store.get_run(run["id"]))["status"] == "needs_reconciliation"


async def test_unchanged_repairs_stop_before_budget_is_used_up(store, tmp_path):
    run = await generated_run(store, tmp_path, review_agent=None)
    await store.update(
        run["id"], repair_budget=3, commands=[{"argv": [sys.executable, "-c", "raise SystemExit(1)"], "timeout": 30}]
    )

    class NoProgress(Builder):
        calls = 0

        async def execute(self, *args):
            self.calls += 1
            return await super().execute(*args)

    provider = NoProgress()
    engine = Engine(store, tmp_path / "jobs", lambda _: provider, journal=Journal(store))
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and "No progress" in result["error"]
    assert provider.calls == result["implementation_rounds"] == 2


async def test_quota_resume_and_handoff_preserve_original_attempt_budget(store, tmp_path, monkeypatch):
    from firefly_forge.providers import ProviderLimitError

    run = await generated_run(store, tmp_path, review_agent=None)
    await store.update(run["id"], repair_budget=1)

    class PausedRepair(Builder):
        calls = 0

        async def execute(self, workspace, *args):
            self.calls += 1
            if self.calls == 2:
                raise ProviderLimitError("Credits exhausted", kind="quota")
            (workspace / "product.py").write_text(f"value = {self.calls}\n")
            return AgentResult("Implemented", "fixture")

    provider = PausedRepair()
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: provider, journal=journal)
    await engine.execute_run(run["id"])
    paused = await store.get_run(run["id"])
    assert paused["status"] == "paused" and paused["implementation_rounds"] == 1
    monkeypatch.setenv("FORGE_REPAIR_ATTEMPTS", "3")
    await journal.resume(run["id"], agent="claude")
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and result["repair_budget"] == 1
    assert result["implementation_rounds"] == 2 and provider.calls == 3
    with pytest.raises(ValueError, match="Only a paused"):
        await journal.resume(run["id"])


async def test_source_tamper_after_approved_review_blocks_checkpoint(store, tmp_path):
    run = await generated_run(store, tmp_path)

    class TamperingEngine(Engine):
        async def _checkpoint_commit(self, identifier, workspace):
            (workspace / "product.py").write_text("value = 99\n")
            await super()._checkpoint_commit(identifier, workspace)

    journal = Journal(store)
    engine = TamperingEngine(store, tmp_path / "jobs", lambda _: Builder(), journal=journal)
    engine.assurance = Assurance(store, tmp_path / "reviews", journal=journal, provider_factory=lambda _: Reviewer())
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["review"]["status"] == "approved"
    assert result["status"] == "failed" and not result["artifacts"]


async def test_artifacts_use_immutable_verified_tree_not_live_working_files(store, tmp_path):
    import zipfile

    run = await generated_run(store, tmp_path, review_agent=None)
    engine = Engine(store, tmp_path / "jobs", lambda _: Builder())
    await engine.execute_run(run["id"])
    saved = await store.get_run(run["id"])
    assert saved["status"] == "succeeded", saved.get("error")
    workspace = engine.workspaces / run["id"]
    (workspace / "product.py").write_text("value = 99\n")
    await engine._artifact(run["id"], workspace, saved["changed_files"], saved["diff"])
    with zipfile.ZipFile(engine.workspaces / (run["id"] + ".zip")) as archive:
        assert archive.read("product.py") == b"value = 7\n"


@pytest.mark.parametrize(
    "report",
    [
        {
            "verdict": "approved",
            "summary": "Fine",
            "findings": [{"severity": "low", "summary": "Note", "evidence": "x"}] * 31,
        },
        {
            "verdict": "changes_required",
            "summary": "Fix source",
            "findings": [
                {
                    "severity": "high",
                    "path": "product.py",
                    "line": 1,
                    "summary": "Wrong number",
                    "evidence": "value = 7",
                }
            ],
        },
        {
            "verdict": "approved",
            "summary": "Fine",
            "findings": [{"severity": "low", "path": "product.py", "summary": "Note", "evidence": "fictional source"}],
        },
    ],
)
async def test_review_validation_or_changes_required_prevents_delivery(store, tmp_path, report):
    run = await generated_run(store, tmp_path)

    class InvalidReviewer(Reviewer):
        async def execute(self, workspace, *args):
            (workspace / "review.json").write_text(json.dumps(report))
            return AgentResult("Reviewed", "fixture")

    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: Builder(), journal=journal)
    engine.assurance = Assurance(
        store, tmp_path / "reviews", journal=journal, provider_factory=lambda _: InvalidReviewer()
    )
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and not result["artifacts"]


async def test_reviewer_cannot_modify_original_workspace(store, tmp_path):
    run = await generated_run(store, tmp_path)
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: Builder(), journal=journal)

    class EscapingReviewer(Reviewer):
        async def execute(self, *args):
            result = await super().execute(*args)
            (engine.workspaces / run["id"] / "product.py").write_text("value = 99\n")
            return result

    engine.assurance = Assurance(
        store, tmp_path / "reviews", journal=journal, provider_factory=lambda _: EscapingReviewer()
    )
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and not result["artifacts"]


async def test_quota_review_resumes_without_recoding_or_rechecking(store, tmp_path):
    from firefly_forge.providers import ProviderLimitError

    run = await generated_run(store, tmp_path)

    class QuotaReviewer(Reviewer):
        async def execute(self, *args):
            if self.calls == 0:
                self.calls += 1
                raise ProviderLimitError("Credits exhausted", kind="quota")
            return await super().execute(*args)

    reviewer = QuotaReviewer()
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: Builder(), journal=journal)
    engine.assurance = Assurance(store, tmp_path / "reviews", journal=journal, provider_factory=lambda _: reviewer)
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "paused"
    await journal.resume(run["id"])
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded" and reviewer.calls == 2
    assert result["implementation_rounds"] == 1 and len(result["verification"]) == 1


async def test_completed_review_receipt_is_reused_after_interrupted_checkpoint(store, tmp_path):
    import asyncio

    run = await generated_run(store, tmp_path)

    class InterruptedCheckpoint(Engine):
        interrupted = False

        async def _checkpoint_commit(self, identifier, workspace):
            if not self.interrupted:
                self.interrupted = True
                self.stopping = True
                raise asyncio.CancelledError()
            await super()._checkpoint_commit(identifier, workspace)

    journal = Journal(store)
    reviewer = Reviewer()
    engine = InterruptedCheckpoint(store, tmp_path / "jobs", lambda _: Builder(), journal=journal)
    engine.assurance = Assurance(store, tmp_path / "reviews", journal=journal, provider_factory=lambda _: reviewer)
    with pytest.raises(asyncio.CancelledError):
        await engine.execute_run(run["id"])
    await journal.recover()
    await journal.resume(run["id"])
    engine.stopping = False
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded" and reviewer.calls == 1
    assert len(result["verification"]) == 1 and result["implementation_rounds"] == 1


async def test_reviewer_cannot_change_git_metadata_without_changing_head(store, tmp_path):
    run = await generated_run(store, tmp_path)

    class GitMutator(Reviewer):
        async def execute(self, workspace, *args):
            result = await super().execute(workspace, *args)
            with (workspace / ".git/config").open("a") as config:
                config.write("\n[alias]\n fake = status\n")
            return result

    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: Builder(), journal=journal)
    engine.assurance = Assurance(store, tmp_path / "reviews", journal=journal, provider_factory=lambda _: GitMutator())
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and not result["artifacts"]


async def test_interrupted_verification_does_not_consume_another_implementation_round(store, tmp_path):
    import asyncio

    run = await generated_run(store, tmp_path, review_agent=None)
    await store.update(run["id"], repair_budget=1)

    class RepairingBuilder(Builder):
        calls = 0

        async def execute(self, workspace, *args):
            self.calls += 1
            (workspace / "product.py").write_text(f"value = {7 if self.calls > 1 else 100}\n")
            return AgentResult("Built", "fixture")

    class LostVerification(Engine):
        interrupted = False

        async def _receipt(self, identifier, key, evidence, **kwargs):
            if evidence.get("checks") and not evidence.get("failed_checks") and not self.interrupted:
                self.interrupted = True
                self.stopping = True
                raise asyncio.CancelledError()
            await super()._receipt(identifier, key, evidence, **kwargs)

    journal = Journal(store)
    builder = RepairingBuilder()
    engine = LostVerification(store, tmp_path / "jobs", lambda _: builder, journal=journal)
    with pytest.raises(asyncio.CancelledError):
        await engine.execute_run(run["id"])
    await journal.recover()
    await journal.resume(run["id"])
    engine.stopping = False
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded", result.get("error")
    assert builder.calls == result["implementation_rounds"] == 2


async def test_interruption_after_repeated_failure_receipt_cannot_restart_no_progress_loop(store, tmp_path):
    import asyncio

    run = await generated_run(store, tmp_path, review_agent=None)
    await store.update(
        run["id"], repair_budget=3, commands=[{"argv": [sys.executable, "-c", "raise SystemExit(1)"], "timeout": 30}]
    )

    class NoProgress(Builder):
        calls = 0

        async def execute(self, *args):
            self.calls += 1
            return await super().execute(*args)

    class LostReceiptResponse(Engine):
        interrupted = False

        async def _receipt(self, identifier, key, evidence, **kwargs):
            await super()._receipt(identifier, key, evidence, **kwargs)
            if evidence.get("failed_checks") and evidence.get("implementation_round") == 2 and not self.interrupted:
                self.interrupted = True
                self.stopping = True
                raise asyncio.CancelledError()

    builder = NoProgress()
    journal = Journal(store)
    engine = LostReceiptResponse(store, tmp_path / "jobs", lambda _: builder, journal=journal)
    with pytest.raises(asyncio.CancelledError):
        await engine.execute_run(run["id"])
    await journal.recover()
    await journal.resume(run["id"])
    engine.stopping = False
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and "No progress" in result["error"]
    assert builder.calls == result["implementation_rounds"] == 2


async def test_reviewed_design_is_materialized_before_worker_without_svg_prompt_bloat(store, tmp_path):
    from test_product_design import definition, review

    from firefly_forge.collaboration import Collaboration
    from firefly_forge.product_design import ProductDesign, design_files

    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Inventory", url=str(source)))
    design = ProductDesign(store)
    product = definition()
    bundle = await design.preview(repository["id"], product)
    product = product.model_copy(update={"design_review": review(bundle)})
    await Collaboration(store).save_product(repository["id"], product)
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="Build inventory",
            skills=[],
            commands=[Command(argv=[sys.executable, "-c", "from product import value; assert value == 7"])],
        )
    )

    class DesignBuilder(Builder):
        async def execute(self, workspace, brief, *args):
            for path, content in design_files(bundle).items():
                actual = (workspace / path).read_text()
                assert json.loads(actual) == json.loads(content) if path.endswith(".json") else actual == content
            assert "docs/design/interaction-contract.json" in brief and "<svg" not in brief
            return await super().execute(workspace, brief, *args)

    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: DesignBuilder(), journal=journal)
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded", result.get("error")
    assert result["design_materialized_digest"] == bundle["digest"]
    plan = await journal.plan(run["id"])
    assert plan["design_reference"]["digest"] == bundle["digest"]
    assert plan["design_reference"]["paths"] == list(design_files(bundle))
