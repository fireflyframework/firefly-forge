"""Read-only diagnosis against frozen Phase A; controlled SDK and memory store."""

import copy
import json
from types import SimpleNamespace

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import activated, controlled_source_snapshot, result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_bridge_inflight_cancel import RUN


async def observe_boundary(
    tmp_path, monkeypatch, checkpoint_due, *, feedback=None, configure=None, expected_boundary=True, unknown=False
):
    env = await activated(tmp_path, monkeypatch)
    env.store.execution_ownership_token = object()
    env.store.run.data["commands"] = [{"argv": ["true"], "timeout": 30}]
    posts, observed = [], []
    env.posts = posts
    if configure is not None:
        await configure(env)

    def transport(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
        posts.append(request.content)
        assert len(posts) <= 2, "No replacement completion is permitted"
        if unknown and len(posts) == 2:
            raise httpx2.ReadError("Controlled unknown original completion", request=request)
        return httpx2.Response(
            200,
            json=result(tool=True)
            if len(posts) == 1
            else result(
                json.dumps({"summary": "Known normal terminal report", "worker_feedback": feedback, "qa_report": None})
            ),
        )

    async def snapshot(*args, **kwargs):
        if getattr(env, "fail_snapshot", False):
            raise ValueError("Controlled source snapshot rejection")
        return ["service.py"], controlled_source_snapshot("Controlled normal terminal source"), "c" * 40

    async def stop(stage):
        task = next(t for t in (await env.journal.plan(RUN))["tasks"] if t["id"] == "implement")
        observed.append(
            {
                "stage": stage,
                "run": copy.deepcopy(await env.store.get_run(RUN)),
                "task": copy.deepcopy(task),
                "entries": [copy.deepcopy(row.data) for row in env.journal.records.rows],
            }
        )
        raise ValueError("Controlled diagnosis stops before any Git or verification effect")

    async def automatic(*args, **kwargs):
        if checkpoint_due:
            await stop("paused_git_entry" if kwargs.get("paused") else "automatic_git_entry")

    async def verification(*args, **kwargs):
        await stop("verification_command_entry")

    env.model.client._client._transport = httpx2.MockTransport(transport)
    env.model.client._client._mounts = {}
    monkeypatch.setattr(env.engine, "_snapshot", snapshot)
    monkeypatch.setattr(env.engine, "_project_command", verification)

    async def hold_paused(*args, **kwargs):
        pass

    env.engine.git_deliveries = SimpleNamespace(automatic=automatic, hold_paused=hold_paused)
    try:
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.engine.execute_run(RUN)
        assert len(posts) == 2
        if expected_boundary:
            assert len(observed) == 1, (await env.store.get_run(RUN)).get("error")
        else:
            assert not observed
        final = (
            observed[0]
            if observed
            else {
                "run": copy.deepcopy(await env.store.get_run(RUN)),
                "task": next(t for t in (await env.journal.plan(RUN))["tasks"] if t["id"] == "implement"),
                "entries": [copy.deepcopy(row.data) for row in env.journal.records.rows],
            }
        )
        final["environment"] = env
        return final
    finally:
        await env.model.client.close()


@pytest.mark.parametrize("checkpoint_due", [True, False])
async def test_native_normal_report_settlement_precedes_git_and_qa(tmp_path, monkeypatch, checkpoint_due):
    observed = await observe_boundary(tmp_path, monkeypatch, checkpoint_due)
    assert observed["run"]["usage"]["runtime_report"] == {
        "summary": "Known normal terminal report",
        "worker_feedback": None,
        "qa_report": None,
    }
    assert not observed["run"].get("bridge_completion_hold"), (
        "Missing native normal-report settlement at " + observed["stage"]
    )
    assert observed["task"]["status"] == "done"
    acceptance = next(e for e in observed["entries"] if e["kind"] == "receipt" and e["task_id"] == "implement")
    receipt = next(e for e in observed["entries"] if e["kind"] == "terminal_completion_receipt")
    assert receipt["payload"]["producer"] == "native_implementation_receipt"
    assert receipt["payload"]["proof"]["acceptance_record_digest"] == acceptance["digest"]
    assert receipt["sequence"] == acceptance["sequence"] + 1
    assert not any(e["kind"] == "worker_feedback" for e in observed["entries"])


def blocked_feedback():
    return dict(
        id="native-blocked",
        role_id="implementer",
        task_id="implement",
        attempt=1,
        disposition="blocked",
        summary="A known native decision is required",
        evidence_paths=[],
        request=dict(
            id="native-question",
            kind="other",
            question="Keep this value?",
            context="Controlled original output",
            choices=["Keep", "Review"],
        ),
    )


async def test_native_blocked_feedback_is_settled_in_its_actual_pause_writer(tmp_path, monkeypatch):
    observed = await observe_boundary(tmp_path, monkeypatch, True, feedback=blocked_feedback())
    assert observed["stage"] == "paused_git_entry"
    assert observed["run"]["status"] == "paused"
    assert observed["run"]["worker_request"]["status"] == "pending"
    assert not observed["run"].get("bridge_completion_hold")
    accepted = next(e for e in observed["entries"] if e["kind"] == "worker_feedback")
    receipt = next(e for e in observed["entries"] if e["kind"] == "terminal_completion_receipt")
    assert receipt["payload"]["producer"] == "native_worker_feedback"
    assert receipt["payload"]["proof"]["acceptance_record_digest"] == accepted["digest"]
    assert receipt["sequence"] == accepted["sequence"] + 1
    assert observed["task"]["status"] == "paused"


async def test_changed_lease_during_terminal_preparation_cannot_accept_source(tmp_path, monkeypatch):
    async def configure(env):
        admit = env.store.admit_credentials

        async def changed(value):
            await admit(value)
            if isinstance(value, dict) and "report" in value and "checkpoint" in value:
                env.store.execution_ownership_token = object()

        env.store.admit_credentials = changed

    observed = await observe_boundary(tmp_path, monkeypatch, True, configure=configure, expected_boundary=False)
    assert observed["run"].get("bridge_completion_hold")
    assert not any(e["kind"] == "receipt" and e["task_id"] == "implement" for e in observed["entries"])


@pytest.mark.parametrize("kind", ["receipt", "worker_feedback"])
async def test_native_terminal_duplicate_requires_the_exact_original_payload(tmp_path, monkeypatch, kind):
    observed = await observe_boundary(
        tmp_path, monkeypatch, True, feedback=blocked_feedback() if kind == "worker_feedback" else None
    )
    env = observed["environment"]
    accepted = next(e for e in observed["entries"] if e["kind"] == kind and e["task_id"] == "implement")
    payload = copy.deepcopy(accepted["payload"])
    if kind == "receipt":
        checkpoint = {"base_revision": observed["run"]["base_revision"], "source_tree": "c" * 40}
        assert await env.journal.receipt(RUN, accepted["idempotency_key"], payload, checkpoint=checkpoint) == accepted
        payload["source_tree"] = "d" * 40
        with pytest.raises(ValueError):
            await env.journal.receipt(RUN, accepted["idempotency_key"], payload, checkpoint=checkpoint)
    else:
        from firefly_forge.agent_roles import WorkerFeedback

        assert await env.journal.worker_feedback(RUN, WorkerFeedback.model_validate(payload)) == accepted
        payload["summary"] = "Changed accepted output"
        with pytest.raises(ValueError):
            await env.journal.worker_feedback(RUN, WorkerFeedback.model_validate(payload))
    assert len([r for r in env.journal.records.rows if r.kind == "terminal_completion_receipt"]) == 1
    assert len(env.posts) == 2


@pytest.mark.parametrize("feedback", [False, True])
@pytest.mark.parametrize("failure", ["settlement_insert", "hold_projection"])
async def test_native_acceptance_and_hold_projection_rollback_together(tmp_path, monkeypatch, feedback, failure):
    async def configure(env):
        append = env.journal._append

        async def fail_insert(run, task, key, kind, payload):
            if kind == "terminal_completion_receipt" and failure == "settlement_insert":
                raise RuntimeError("Controlled native receipt insert failure")
            return await append(run, task, key, kind, payload)

        monkeypatch.setattr(env.journal, "_append", fail_insert)
        save = env.store.run_records.save

        async def fail_save(row):
            await save(row)
            if failure == "hold_projection" and (row.data.get("bridge_completion_settlement") or {}).get("producer"):
                raise RuntimeError("Controlled native hold projection failure")

        monkeypatch.setattr(env.store.run_records, "save", fail_save)

    observed = await observe_boundary(
        tmp_path,
        monkeypatch,
        True,
        feedback=blocked_feedback() if feedback else None,
        configure=configure,
        expected_boundary=False,
    )
    assert observed["run"].get("bridge_completion_hold")
    assert observed["task"]["status"] != "done"
    assert not any(e["kind"] in {"terminal_completion_receipt", "worker_feedback"} for e in observed["entries"])
    assert not any(e["kind"] == "receipt" and e["task_id"] == "implement" for e in observed["entries"])


@pytest.mark.parametrize("change", ["source", "task_attempt", "context", "credential", "cancellation", "authority"])
async def test_changed_current_admission_never_accepts_prepared_terminal_output(tmp_path, monkeypatch, change):
    from types import MethodType

    from firefly_forge.bridge_completions import BridgeCompletions
    from firefly_forge.project_access import AccessDenied
    from firefly_forge.store import Store

    async def configure(env):
        env.revoked = False
        env.store.reject_credentials = MethodType(Store.reject_credentials, env.store)
        execution = BridgeCompletions._execution

        async def authorize(service, run, actor):
            if env.revoked:
                raise AccessDenied("Controlled current authority revoked")
            return await execution(service, run, actor)

        monkeypatch.setattr(BridgeCompletions, "_execution", authorize)
        admit = env.store.admit_credentials

        async def changed(value):
            await admit(value)
            if not isinstance(value, dict) or "report" not in value or "checkpoint" not in value:
                return
            if change == "source":
                env.store.run.data["source_tree"] = "d" * 40
            elif change == "task_attempt":
                next(t for t in env.journal.tasks.rows if t.task_id == "implement").attempt = 2
            elif change == "context":
                env.principal_context.set(env.principal)
            elif change == "credential":
                env.store.credential_registry = lambda: {"NATIVE_TEST_SECRET": "Known normal terminal report"}
            elif change == "cancellation":
                env.store.run.data["cancel_request"] = {"status": "requested"}
            else:
                env.revoked = True

        env.store.admit_credentials = changed

    try:
        observed = await observe_boundary(tmp_path, monkeypatch, True, configure=configure, expected_boundary=False)
        assert observed["run"].get("bridge_completion_hold")
        assert not any(e["kind"] == "terminal_completion_receipt" for e in observed["entries"])
        assert not any(e["kind"] == "receipt" and e["task_id"] == "implement" for e in observed["entries"])
    finally:
        from firefly_forge.project_access import CURRENT_PRINCIPAL

        CURRENT_PRINCIPAL.set(None)


async def test_unknown_HTTP_completion_never_reaches_native_acceptance(tmp_path, monkeypatch):
    observed = await observe_boundary(tmp_path, monkeypatch, True, unknown=True, expected_boundary=False)
    assert observed["run"].get("bridge_completion_hold")
    assert not any(e["kind"] == "terminal_completion_receipt" for e in observed["entries"])
    assert len([e for e in observed["entries"] if e["kind"] == "agent_consume"]) == 1


async def test_snapshot_failure_cannot_turn_known_output_into_success(tmp_path, monkeypatch):
    async def configure(env):
        env.fail_snapshot = True

    observed = await observe_boundary(tmp_path, monkeypatch, True, configure=configure, expected_boundary=False)
    assert observed["run"].get("bridge_completion_hold")
    assert not any(e["kind"] == "terminal_completion_receipt" for e in observed["entries"])
    assert not any(e["kind"] == "receipt" and e["task_id"] == "implement" for e in observed["entries"])


async def test_sanitized_report_mismatch_refuses_original_HTTP_output(tmp_path, monkeypatch):
    async def configure(env):
        update = env.store.update

        async def redact_report(run, **fields):
            if fields.get("usage", {}).get("runtime_report"):
                fields["usage"]["runtime_report"]["summary"] = "Different redacted summary"
            return await update(run, **fields)

        monkeypatch.setattr(env.store, "update", redact_report)

    observed = await observe_boundary(tmp_path, monkeypatch, True, configure=configure, expected_boundary=False)
    assert observed["run"].get("bridge_completion_hold")
    assert not any(e["kind"] == "terminal_completion_receipt" for e in observed["entries"])


async def test_native_receipt_reducer_validates_its_actual_acceptance_row(tmp_path, monkeypatch):
    from firefly_forge.native_terminal_completion_models import validate_native_receipt

    observed = await observe_boundary(tmp_path, monkeypatch, True)
    entries = observed["entries"]
    receipt = next(e for e in entries if e["kind"] == "terminal_completion_receipt")
    assert validate_native_receipt(receipt, entries).producer == "native_implementation_receipt"
    altered = copy.deepcopy(entries)
    next(e for e in altered if e["kind"] == "receipt" and e["task_id"] == "implement")["payload"]["source_tree"] = (
        "d" * 40
    )
    with pytest.raises(ValueError):
        validate_native_receipt(receipt, altered)
    changed = copy.deepcopy(receipt)
    changed["payload"]["producer"] = "native_worker_feedback"
    with pytest.raises(ValueError):
        validate_native_receipt(changed, entries)


async def test_committed_terminal_receipt_is_reused_after_crash_without_model_reentry(tmp_path, monkeypatch):
    observed = await observe_boundary(tmp_path, monkeypatch, True)
    env = observed["environment"]
    before = (env.workspace / "service.py").stat().st_mtime_ns

    async def no_model(*args, **kwargs):
        raise AssertionError("A committed native result cannot reenter a provider")

    monkeypatch.setattr(env.provider, "execute", no_model)
    with models.override_allow_model_requests(False):
        await env.engine.execute_run(RUN)
    run = await env.store.get_run(RUN)
    assert "cannot reenter" not in run.get("error", "")
    assert not run.get("bridge_completion_hold")
    assert len([r for r in env.journal.records.rows if r.kind == "terminal_completion_receipt"]) == 1
    assert (env.workspace / "service.py").stat().st_mtime_ns == before
    assert len(env.posts) == 2


async def test_preexisting_successful_receipt_can_settle_without_replaying_consumed_output(tmp_path, monkeypatch):
    observed = await observe_boundary(tmp_path, monkeypatch, True)
    env = observed["environment"]
    terminal = next(e for e in observed["entries"] if e["kind"] == "terminal_completion_receipt")
    accepted = next(e for e in observed["entries"] if e["kind"] == "receipt" and e["task_id"] == "implement")
    env.journal.records.rows[:] = [r for r in env.journal.records.rows if r.sequence < terminal["sequence"]]
    env.store.run.data.update(
        status="developing", bridge_completion_hold=copy.deepcopy(terminal["payload"]["observed_hold"])
    )
    env.store.run.status = "developing"
    env.store.run.data.pop("bridge_completion_settlement", None)
    await env.journal.receipt(
        RUN,
        accepted["idempotency_key"],
        accepted["payload"],
        checkpoint={"base_revision": observed["run"]["base_revision"], "source_tree": "c" * 40},
    )
    assert not (await env.store.get_run(RUN)).get("bridge_completion_hold")
    assert len([r for r in env.journal.records.rows if r.kind == "receipt" and r.task_id == "implement"]) == 1
    assert len([r for r in env.journal.records.rows if r.kind == "terminal_completion_receipt"]) == 1
    assert len(env.posts) == 2


async def test_unknown_tool_outcome_stays_independently_held_before_terminal_acceptance(tmp_path, monkeypatch):
    async def configure(env):
        async def unresolved(*args):
            return len(env.posts) == 2

        env.engine.tool_broker = SimpleNamespace(unresolved=unresolved)

    observed = await observe_boundary(tmp_path, monkeypatch, True, configure=configure, expected_boundary=False)
    assert observed["run"].get("bridge_completion_hold")
    assert "unresolved" in observed["run"].get("error", "")
    assert not any(e["kind"] == "terminal_completion_receipt" for e in observed["entries"])


async def test_later_stage_receipt_stays_inside_its_owned_unit_after_terminal_settlement(tmp_path, monkeypatch):
    observed = await observe_boundary(tmp_path, monkeypatch, True)
    env = observed["environment"]
    accepted = next(e for e in observed["entries"] if e["kind"] == "receipt" and e["task_id"] == "implement")
    async with env.store._unit():
        intent = await env.journal.intent(RUN, "verify", 1)
        record = await env.journal.receipt(RUN, intent["idempotency_key"], {"checks": []})
        assert record["task_id"] == "verify" and record["payload"] == {"checks": []}
    checkpoint = {"base_revision": observed["run"]["base_revision"], "source_tree": "c" * 40}
    async with env.store._unit():
        with pytest.raises(ValueError, match="no active caller transaction"):
            await env.journal.receipt(RUN, accepted["idempotency_key"], accepted["payload"], checkpoint=checkpoint)
    assert len([r for r in env.journal.records.rows if r.kind == "terminal_completion_receipt"]) == 1
    assert len(env.posts) == 2
