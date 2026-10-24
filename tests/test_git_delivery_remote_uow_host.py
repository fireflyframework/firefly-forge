# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Git/provider adapter phases with a strict public PyFly memory transaction."""

import copy
import hashlib
from contextlib import asynccontextmanager

import pytest
from pyfly.data.transaction import TransactionTemplate, current_unit_of_work
from pyfly.data.transaction.manager import TransactionCapabilities
from pyfly.data.transaction.unit_of_work import UnitOfWork
from test_git_delivery_lifecycle import remote_case as remote_case
from test_git_delivery_local import delivery_case as delivery_case
from test_git_delivery_local import git

from firefly_forge import scm
from firefly_forge.engine import Engine

RUN = "1" * 32
ACTOR = {"user_id": "4" * 32}


class MemoryManager:
    datasource = "git-remote-host"
    capabilities = TransactionCapabilities(backend="host-memory", supports_savepoints=False)

    def __init__(self, state):
        self.state, self.commits = state, 0

    async def begin(self, definition):
        unit = UnitOfWork(self, self.datasource, object(), definition=definition)
        unit.attributes["snapshot"] = copy.deepcopy(self.state)
        return unit

    async def commit(self, unit):
        self.commits += 1

    async def rollback(self, unit):
        self.state.clear()
        self.state.update(unit.attributes["snapshot"])

    async def release(self, unit):
        pass

    async def open_auto_unit(self, **kwargs):
        raise AssertionError("Explicit transactions only")

    async def create_savepoint(self, unit):
        raise AssertionError("No savepoint emulation")

    async def release_savepoint(self, unit, savepoint):
        raise AssertionError("No savepoint emulation")

    async def rollback_to_savepoint(self, unit, savepoint):
        raise AssertionError("No savepoint emulation")

    def resource_active(self, unit):
        return True

    def marks_rollback_only(self, unit, error):
        return False

    def is_disconnect(self, error):
        return False


@pytest.fixture
def strict_remote(remote_case, monkeypatch):
    c = remote_case
    manager = MemoryManager(c.state)

    @asynccontextmanager
    async def unit():
        if action := getattr(c, "before_owned", None):
            await action()
        async with TransactionTemplate(manager).transaction():
            yield None

    c.service.store.source_budget_admission_unit = unit
    append = c.service.journal.git_delivery_append

    async def owned_append(*args):
        assert current_unit_of_work() is not None, "Remote custody must commit in an owned transaction"
        return await append(*args)

    c.service.journal.git_delivery_append = owned_append
    client = c.service.http_client
    c.observed = []

    async def request(request):
        assert current_unit_of_work() is None, "Provider network I/O inside native UOW"
        assert any(e["kind"] == "git_delivery_remote_intent" for e in c.state["entries"])
        assert manager.commits > 0
        c.observed.append(request.method + " " + request.url.path)

    async def response(response):
        if action := getattr(c, "after_response", None):
            await action(response)

    c.service.http_client = lambda **kwargs: client(
        event_hooks={"request": [request], "response": [response]}, **kwargs
    )
    push = scm.push

    async def outside_push(*args, **kwargs):
        assert current_unit_of_work() is None, "Git remote I/O inside native UOW"
        assert any(e["kind"] == "git_delivery_remote_intent" for e in c.state["entries"])
        assert manager.commits > 0
        return await push(*args, **kwargs)

    monkeypatch.setattr(scm, "push", outside_push)
    return c


async def checkpoint(c):
    (c.root / "product.py").write_text("value = 2\n")
    return await c.service.checkpoint(RUN, request_id="a" * 32, expected_head=c.base, actor=ACTOR)


async def test_native_units_commit_intent_before_remote_io_then_confirm_draft(strict_remote):
    c = strict_remote
    receipt = await checkpoint(c)
    assert receipt["remote_status"] == "draft"
    assert git(c.remote, "rev-parse", "refs/heads/forge/public-unit") == receipt["revision"]
    assert [e["kind"] for e in c.state["entries"]].count("git_delivery_remote_receipt") == 1
    assert c.observed and current_unit_of_work() is None


async def test_final_ready_provider_io_stays_outside_owned_confirmation(strict_remote):
    c = strict_remote
    receipt = await checkpoint(c)
    c.state["run"].update(status="succeeded", source_tree=receipt["source_tree"], final_verification=[{"exit_code": 0}])

    async def exact(*args):
        pass

    c.service.engine._publication_binding = Engine._publication_binding
    c.service.engine._assert_delivery_tree = exact
    c.service.engine._assert_publication_current = exact
    draft = {"title": "Verified unit", "body": "Recorded checks passed.", "communication": {"mode": "deterministic"}}
    draft["body_sha256"] = hashlib.sha256(draft["body"].encode()).hexdigest()
    draft["binding_sha256"] = Engine._publication_binding(
        await c.service.store.get_run(RUN), await c.service.store.repository(c.state["run"]["repository_id"])
    )
    prepared = await c.service.final_start(await c.service.store.get_run(RUN), ACTOR, draft)
    assert await c.service.final_ready(prepared, draft) == receipt["pr_url"]
    assert c.remote_state["draft"]["draft"] is False
    assert sum(e["kind"] == "git_delivery_remote_receipt" for e in c.state["entries"]) == 2


@pytest.mark.parametrize("change", ["lease", "authority", "source"])
async def test_post_effect_drift_retains_unknown_intent_without_replaying(strict_remote, change):
    c = strict_remote

    async def changed(response):
        if response.request.method != "POST":
            return
        if change == "lease":
            c.state["owned"] = False
        elif change == "authority":
            c.state["allowed"] = False
        else:
            (c.root / "product.py").write_text("value = 999\n")

    c.after_response = changed
    with pytest.raises((RuntimeError, PermissionError, ValueError)):
        await checkpoint(c)
    assert c.remote_state["draft"] is not None
    assert not any(e["kind"] == "git_delivery_remote_receipt" for e in c.state["entries"])
    c.state.update(owned=True, allowed=True)
    c.after_response = None
    effects = list(c.observed)
    head = git(c.root, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="unknown|unresolved"):
        await c.service.checkpoint(RUN, request_id="b" * 32, expected_head=head, actor=ACTOR)
    assert c.observed == effects and current_unit_of_work() is None


@pytest.mark.parametrize("stage", ["before_dispatch", "before_receipt"])
async def test_final_publication_facts_are_rechecked_after_owned_wait(strict_remote, stage):
    c = strict_remote
    receipt = await checkpoint(c)
    c.state["run"].update(status="succeeded", source_tree=receipt["source_tree"], final_verification=[{"exit_code": 0}])
    armed = False
    ready_waits = 0
    changed = False

    async def outside_review(*args):
        nonlocal armed
        assert current_unit_of_work() is None
        armed = True

    c.service.engine._assert_delivery_tree = outside_review
    c.service.engine._assert_publication_current = outside_review
    c.service.engine._publication_binding = Engine._publication_binding
    run = await c.service.store.get_run(RUN)
    draft = {
        "title": "Verified unit",
        "body": "Recorded checks passed.",
        "body_sha256": hashlib.sha256(b"Recorded checks passed.").hexdigest(),
        "communication": {"mode": "deterministic"},
        "binding_sha256": Engine._publication_binding(run, await c.service.store.repository(run["repository_id"])),
    }
    prepared = await c.service.final_start(run, ACTOR, draft)
    armed = False

    async def change_during_owned_wait():
        nonlocal changed, ready_waits
        if not armed or changed:
            return
        if c.remote_state["draft"]["draft"] is False:
            ready_waits += 1
        if stage == "before_dispatch" or ready_waits == 2:
            c.state["run"]["final_verification"] = [{"exit_code": 1}]
            changed = True

    c.before_owned = change_during_owned_wait
    effects = list(c.observed)
    with pytest.raises(ValueError, match="Publication facts"):
        await c.service.final_ready(prepared, draft)
    assert changed and sum(e["kind"] == "git_delivery_remote_receipt" for e in c.state["entries"]) == 1
    if stage == "before_dispatch":
        assert c.observed == effects and c.remote_state["draft"]["draft"] is True
    else:
        assert c.remote_state["draft"]["draft"] is False
    assert current_unit_of_work() is None
