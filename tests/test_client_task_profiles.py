# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import contextlib
import json

import httpx
import pytest
from click.testing import CliRunner

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server


def connection(seen, responder=None):
    client = ForgeClient("http://factory.invalid", "parity-operator-token")
    client.http.close()

    def handle(request):
        assert request.headers["Authorization"] == "Bearer parity-operator-token"
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if responder:
            return responder(request, body)
        return httpx.Response(200, json={"id": "result", "status": "queued"})

    client.http = httpx.Client(
        base_url="http://factory.invalid/api/v1/",
        headers={"Authorization": "Bearer parity-operator-token"},
        transport=httpx.MockTransport(handle),
    )
    return client


def change():
    return {
        "request_id": "a" * 32,
        "base_revision": 2,
        "base_digest": "b" * 64,
        "append_acceptance": ["Owner sees changes"],
    }


def test_sdk_named_handoff_preserves_legacy_body_and_rejects_empty_selection():
    seen = []
    with connection(seen) as client:
        client.handoff("1" * 32, "claude")
        client.handoff("1" * 32, model_profile_id="source-builder")
        assert seen[0][2] == {"agent": "claude"}
        assert seen[1][2] == {"model_profile_id": "source-builder"}
        with pytest.raises(ValueError):
            client.handoff("1" * 32)
        assert len(seen) == 2


def test_sdk_assistant_routes_task_and_exact_reviewed_analysis_without_false_default_agent():
    seen = []
    with connection(seen) as client:
        client.create_assistant_conversation(
            "1" * 32,
            task="functions",
            model_profile_id="planner",
            project_id="2" * 32,
            analysis_id="3" * 32,
            analysis_digest="4" * 64,
            analysis_component_ids=["api"],
        )
        body = seen[0][2]
        assert "agent" not in body
        assert body["task"] == "functions" and body["model_profile_id"] == "planner"
        assert body["analysis_digest"] == "4" * 64 and body["analysis_component_ids"] == ["api"]
        with pytest.raises(ValueError):
            client.create_assistant_conversation("1" * 32, analysis_id="3" * 32)
        assert len(seen) == 1


def test_sdk_lifecycle_and_admin_routes_validate_native_dtos_and_redact_secret_response():
    from firefly_forge.model_profiles import ModelProfileInput
    from firefly_forge.product_lifecycle import FeatureChangeInput
    from firefly_forge.task_routing import TaskRoutingInput

    seen = []
    secret = "private-profile-key"

    def response(request, body):
        if request.method == "GET" and request.url.path.endswith("model-profiles"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "id": "builder",
                "name": "Builder",
                "provider": "agentic",
                "config": {"api_key": secret, "api_key_configured": True, "model": "openai:configured-model"},
                "values": {"api_key": secret},
                "status": "configured",
            },
        )

    with connection(seen, response) as client:
        client.preview_product_change("1" * 32, FeatureChangeInput.model_validate(change()))
        client.apply_product_change("1" * 32, change())
        client.model_profiles()
        public = client.save_model_profile(
            "builder", ModelProfileInput(name="Builder", provider="agentic", values={"api_key": secret})
        )
        assert secret not in json.dumps(public)
        assert public["config"]["api_key_configured"] is True
        client.test_model_profile("builder")
        client.task_routing()
        client.save_task_routing(TaskRoutingInput(bindings={"functions": "planner"}))
        paths = [item[1] for item in seen]
        assert paths == [
            "/api/v1/repositories/" + "1" * 32 + "/product/changes/preview",
            "/api/v1/repositories/" + "1" * 32 + "/product/changes",
            "/api/v1/model-profiles",
            "/api/v1/model-profiles/builder",
            "/api/v1/model-profiles/builder/test",
            "/api/v1/task-routing",
            "/api/v1/task-routing",
        ]
        with pytest.raises(ValueError):
            client.save_task_routing({"bindings": {"unknown-task": "builder"}})
        assert len(seen) == 7


def test_sdk_profile_error_does_not_echo_write_only_secret():
    secret = "write-only-profile-key"
    seen = []
    with connection(seen, lambda *_: httpx.Response(400, json={"detail": secret})) as client:
        with pytest.raises(RuntimeError) as caught:
            client.save_model_profile(
                "builder", {"name": "Builder", "provider": "agentic", "values": {"api_key": secret}}
            )
        assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "args,method",
    [
        (["handoff", "1" * 32, "--model-profile", "builder"], "handoff"),
        (
            ["assistant", "start", "1" * 32, "--task", "architecture", "--model-profile", "planner"],
            "create_assistant_conversation",
        ),
    ],
)
def test_cli_forwards_explicit_profile_and_task(monkeypatch, args, method):
    calls = []

    class Connection:
        def __getattr__(self, name):
            def call(*positional, **kwargs):
                calls.append((name, positional, kwargs))
                return {"id": "result"}

            return call

    monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(Connection()))
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    assert calls[0][0] == method and calls[0][2]["model_profile_id"] in {"builder", "planner"}


async def test_mcp_task_lifecycle_and_admin_schemas_are_native_and_exclude_raw_credentials():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    assert "model_profile_id" in tools["handoff_run"].input_schema["properties"]
    assistant = tools["start_product_conversation"].input_schema
    assert "functions" in str(assistant) and "analysis_digest" in assistant["properties"]
    assert "model_profile_id" in assistant["properties"]
    assert "base_digest" in str(tools["preview_product_change"].input_schema)
    assert "integration_upserts" in str(tools["apply_product_change"].input_schema)
    assert "bindings" in str(tools["save_task_routing"].input_schema)
    properties = tools["configure_model_profile"].input_schema["properties"]
    assert "credential_file" in properties and "api_key" not in properties and "token" not in properties


async def test_mcp_invokes_named_handoff_assistant_and_typed_change_through_sdk(monkeypatch):
    seen = []
    with connection(seen) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        server = create_server()
        result = await server.call_tool("handoff_run", {"identifier": "1" * 32, "model_profile_id": "builder"})
        assert not result.is_error
        result = await server.call_tool(
            "start_product_conversation",
            {"repository_id": "1" * 32, "task": "functions", "model_profile_id": "planner"},
        )
        assert not result.is_error
        result = await server.call_tool("preview_product_change", {"repository_id": "1" * 32, "request": change()})
        assert not result.is_error
        assert seen[0][2] == {"model_profile_id": "builder"}
        assert seen[1][2]["task"] == "functions" and "agent" not in seen[1][2]
        assert seen[2][2]["request_id"] == "a" * 32


def test_cli_configuration_uses_private_credential_file_and_never_prints_key(tmp_path, monkeypatch):
    seen = []
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"name": "Builder", "provider": "agentic", "values": {"model": "openai:model"}}))
    key = tmp_path / "provider.key"
    secret = "private-profile-key"
    key.write_text(secret)
    key.chmod(0o600)

    def response(request, body):
        assert body["values"]["api_key"] == secret
        return httpx.Response(
            200, json={"id": "builder", "provider": "agentic", "config": {"api_key_configured": True}}
        )

    with connection(seen, response) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        result = CliRunner().invoke(
            cli.main, ["models", "save", "builder", str(profile), "--credential-file", str(key)]
        )
        assert result.exit_code == 0, result.output
        assert secret not in result.output
        key.chmod(0o644)
        rejected = CliRunner().invoke(
            cli.main, ["models", "save", "builder", str(profile), "--credential-file", str(key)]
        )
        assert rejected.exit_code != 0 and secret not in rejected.output
        assert len(seen) == 1


async def test_mcp_model_profile_rejects_inline_credential_before_http(monkeypatch):
    seen = []
    with connection(seen) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        server = create_server()
        with pytest.raises(Exception) as error:
            await server.call_tool(
                "configure_model_profile",
                {
                    "profile_id": "builder",
                    "request": {
                        "name": "Builder",
                        "provider": "agentic",
                        "values": {"api_key": "private-model-input-key"},
                    },
                },
            )
        assert "private-model-input-key" not in str(error.value)
        assert not seen


def test_native_task_profiles_handoff_lifecycle_and_assistant_sdk_mcp_parity(tmp_path, postgres_schema, monkeypatch):
    import asyncio

    from native_client_knowledge_fixtures import activate_native_client_target
    from starlette.testclient import TestClient
    from test_collaboration import product

    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.models import RepositoryInput, RunInput

    async def idle_worker(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle_worker)
    monkeypatch.setenv("FORGE_AUTH_MODE", "local_bootstrap")
    token = "native-task-parity-operator-token-32chars"
    app = create_application(tmp_path / "factory", token)

    class NativeClient(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with NativeClient(
        app,
        base_url="http://127.0.0.1/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        http.portal.call(activate_native_client_target, app.state.store, tmp_path / "framework-prerequisite")
        client = ForgeClient("http://127.0.0.1", token)
        client.http.close()
        client.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        repo = client.add_repository(RepositoryInput(name="Product", url="https://github.com/team/product.git"))
        for name in ("builder", "next-builder"):
            result = client.save_model_profile(
                name,
                {
                    "name": name,
                    "provider": "agentic",
                    "values": {"model": "openai:configured-model", "api_key": "native-profile-fixture-key"},
                },
            )
            assert result["config"]["api_key_configured"] is True
            assert "native-profile-fixture-key" not in json.dumps(result)
        assert len(client.model_profiles()) == 2
        client.save_task_routing({"bindings": {"functions": "builder"}})
        assert client.task_routing()["bindings"] == {"functions": "builder"}
        conversation = client.create_assistant_conversation(repo["id"], task="functions")
        assert conversation["agent"] == "agentic" and conversation["model_selection"]["profile_id"] == "builder"
        run = client.create_run(
            RunInput(repository_id=repo["id"], brief="Scoped source change", model_profile_id="builder")
        )

        async def pause():
            await app.state.store.update(run["id"], status="paused", pause={"kind": "quota"})

        http.portal.call(pause)
        resumed = client.handoff(run["id"], model_profile_id="next-builder")
        assert resumed["model_selection"]["profile_id"] == "next-builder"
        assert resumed["config_revision"] == resumed["model_selection"]["config_revision"]
        assert client.journal(run["id"])["entries"][-1]["payload"]["selection_mode"] == "named_profile"
        saved = client.save_product_definition(repo["id"], product().model_dump())
        request = change() | {"base_revision": saved["revision"], "base_digest": saved["digest"]}
        preview = client.preview_product_change(repo["id"], request)
        assert preview["implementation_status"] == "not_started"
        applied = client.apply_product_change(repo["id"], request)
        assert applied["revision"] == 2 and not applied["definition"]["functional_reviewed"]
        server = create_server()
        replay = asyncio.run(
            server.call_tool("apply_product_change", {"repository_id": repo["id"], "request": request})
        )
        assert not replay.is_error and applied["digest"] in str(replay)
        listed = CliRunner().invoke(cli.main, ["models", "list"])
        assert listed.exit_code == 0 and len(json.loads(listed.output)) == 2
        assert "native-profile-fixture-key" not in listed.output


def test_cli_and_mcp_run_profiles_omit_false_provider_defaults(monkeypatch):
    import asyncio

    calls = []

    class Connection:
        def create_run(self, request):
            calls.append(request.model_dump(exclude_unset=True))
            return {"id": "result", "status": "queued"}

    monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(Connection()))
    result = CliRunner().invoke(
        cli.main,
        [
            "run",
            "--repository",
            "1" * 32,
            "--brief",
            "Scoped change",
            "--model-profile",
            "builder",
            "--review-model-profile",
            "reviewer",
        ],
    )
    assert result.exit_code == 0, result.output
    asyncio.run(
        create_server().call_tool(
            "create_run",
            {
                "repository_id": "1" * 32,
                "brief": "Scoped change",
                "model_profile_id": "builder",
                "review_model_profile_id": "reviewer",
            },
        )
    )
    assert len(calls) == 2
    for request in calls:
        assert "agent" not in request
        assert request["model_profile_id"] == "builder"
        assert request["assurance"]["review_model_profile_id"] == "reviewer"
