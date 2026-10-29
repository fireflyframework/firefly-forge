# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native API/DI/grant WAL and exact readback; no coding provider is executed."""

import json
import os
from pathlib import Path
from uuid import uuid4

from starlette.testclient import TestClient
from test_execution import seed_repository
from test_provider_recovery_allowance_native import OUTCOME
from worker_reply_fixtures import authenticated_operator

from firefly_forge.api import create_application
from firefly_forge.coding_check_grants import CodingCheckGrants
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.engine import Engine
from firefly_forge.git_workspace import metadata_digest
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowanceInput


def test_native_coding_check_http_di_candidate_grant_current_revoke_and_exact_receipt(tmp_path, postgres_schema):
    from native_client_knowledge_fixtures import activate_native_client_target

    token = "public-native-coding-check-api-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52551),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        context, store = http.app.state.runtime.context, http.app.state.store
        http.portal.call(activate_native_client_target, store, tmp_path / "framework-prerequisite")
        engine, recoveries = context.get_bean(Engine), context.get_bean(ProviderRecoveries)
        grants = context.get_bean(CodingCheckGrants)
        assert engine.coding_checks is grants and recoveries.coding_checks is grants
        executable = tmp_path / "public-configured-cli"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        store.configuration.update("codex", ProviderConfiguration(values={"executable": str(executable)}))

        async def seed():
            async with authenticated_operator(store):
                repository = await store.add_repository(
                    RepositoryInput(
                        name="Configured-check native API fixture", url=str(await seed_repository(tmp_path / "source"))
                    )
                )
                run = await store.create_run(
                    RunInput(
                        repository_id=repository["id"],
                        brief="Review explicit configured checks",
                        frontend="none",
                        skills=[],
                        commands=[Command(argv=["approved-check"], timeout=10)],
                    )
                )
                journal = context.get_bean(Journal)
                await journal.ensure_plan(run["id"])
                workspace = await seed_repository(engine.workspaces / run["id"])
                (workspace / "partial.py").write_text("VALUE = 1\n")
                await store.update(run["id"], git_metadata_digest=metadata_digest(workspace), active_task="implement")
                for attempt in range(1, 5):
                    intent = await journal.intent(run["id"], "implement", attempt)
                    await store.update(run["id"], status="developing", attempt=attempt)
                    await recoveries.normal_dispatch(run["id"], intent["idempotency_key"])
                    await recoveries.record_process(run["id"], intent["idempotency_key"], OUTCOME)
                await store.update(run["id"], status="failed", error="Controlled known native failure")
                return run["id"]

        run_id = http.portal.call(seed)
        endpoint = f"/api/v1/runs/{run_id}/coding-check-grants"
        future = uuid4().hex
        response = http.get(endpoint + f"/candidate?recovery_request_id={future}&bind_port=11447")
        assert response.status_code == 200, response.text
        candidate = response.json()
        assert candidate["channel"]["bind_port"] == 11447 and candidate["commands"][0]["argv"] == ["approved-check"]
        assert http.get(endpoint + "/current").json()["status"] == "none"
        body = dict(
            request_id=uuid4().hex,
            recovery_request_id=future,
            candidate_digest=candidate["digest"],
            expected_revision=0,
            command_indices=[0],
            call_limit=2,
            valid_seconds=300,
            bind_port=11447,
            reason="Explicitly reviewed only the retained configured check.",
        )
        response = http.post(endpoint, json=body)
        assert response.status_code == 201, response.text
        receipt = response.json()
        assert receipt["call_limit"] == 2
        assert http.post(endpoint, json=body).json() == receipt
        assert http.get(endpoint + "/" + body["request_id"]).json() == receipt
        current = http.get(endpoint + "/current").json()
        assert current["status"] == "active" and current["receipt_digest"] == receipt["receipt_digest"]
        assert http.post(endpoint, json=body | {"argv": ["other"]}).status_code == 422
        assert http.post(endpoint, json=body | {"request_id": uuid4().hex}).status_code == 409
        allowance_path = f"/api/v1/runs/{run_id}/provider-recoveries/allowances"
        allowance_candidate = http.get(allowance_path + "/candidate?agent=codex&adopt_current_executable=true").json()
        allowance_body = ProviderRecoveryAllowanceInput(
            request_id=future,
            candidate_digest=allowance_candidate["digest"],
            native_process_digest=allowance_candidate["native_process_digest"],
            agent="codex",
            reason="Public native fixture reviewed mitigation.",
            mitigation_evidence_digest="e" * 64,
            adopt_current_executable=True,
            coding_check_grant_id=body["request_id"],
        ).model_dump()
        linked = http.post(allowance_path, json=allowance_body)
        assert linked.status_code == 201, linked.text
        assert http.get(allowance_path + "/" + future).json() == linked.json()
        export = os.environ.get("FORGE_NATIVE_FIXTURE_EXPORT_DIR")
        if export:
            directory = Path(export)
            assert directory.is_dir() and not directory.is_symlink() and directory.stat().st_mode & 0o777 == 0o700
            for name, value in {
                "candidate": candidate,
                "grant": receipt,
                "current": current,
                "allowance_candidate": allowance_candidate,
                "allowance": linked.json(),
            }.items():
                encoded = json.dumps(value, indent=2).encode()
                assert token not in encoded.decode()
                http.portal.call(store.admit_credentials, value)
                descriptor = os.open(directory / (name + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as target:
                    target.write(encoded)
                    target.flush()
                    os.fsync(target.fileno())
        revoked = dict(
            request_id=uuid4().hex,
            grant_id=body["request_id"],
            expected_revision=1,
            reason="Revoke the explicit native channel before launch.",
        )
        response = http.post(endpoint + "/revocations", json=revoked)
        assert response.status_code == 201, response.text
        assert http.post(endpoint + "/revocations", json=revoked).json() == response.json()
        assert http.get(endpoint + "/" + revoked["request_id"]).json() == response.json()
        assert http.get(endpoint + "/current").json()["status"] == "revoked"
        assert http.get(endpoint + "/" + body["request_id"]).json() == receipt
