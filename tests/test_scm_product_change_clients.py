# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Whole-product capability adapters preserve reviewed identities and exact read recovery."""

import asyncio
import contextlib
import copy
import json

import httpx
import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from test_mcp_prompt_templates import KNOWLEDGE_DESCRIPTION_CHANGES, KOINON_TOOLS

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.scm_product_change_models import SCMProductChangeGrantInputV1

PROJECT, REPOSITORY, REQUEST, BINDING = (value * 32 for value in "abcd")
BASE = f"projects/{PROJECT}/repositories/{REPOSITORY}/scm-product-change-grants"


def grant():
    return dict(
        version=1,
        request_id=REQUEST,
        expected_previous_digest=None,
        expected_project_binding_digest="1" * 64,
        expected_binding_request_id=BINDING,
        expected_binding_digest="2" * 64,
        component_ids=["api"],
        allowed_actions=["product_change"],
        active=True,
        acknowledge_product_definition_edit=True,
    )


def connection_with(transport):
    connection = ForgeClient("https://forge.invalid", "fictional-local-token")
    connection.http.close()
    connection.http = httpx.Client(base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(transport))
    return connection


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_three_adapters_forward_exact_grant_and_read_recovery(monkeypatch, tmp_path, interface):
    calls = []
    body = grant()
    receipt = {"version": 1, "request": body, "digest": "3" * 64}

    def transport(request):
        calls.append(request)
        return httpx.Response(201 if request.method == "POST" else 200, json=receipt)

    with connection_with(transport) as connection:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        if interface == "sdk":
            saved = connection.configure_scm_product_change_grant(PROJECT, REPOSITORY, body)
            exact = connection.scm_product_change_grant(PROJECT, REPOSITORY, REQUEST)
            current = connection.current_scm_product_change_grant(PROJECT, REPOSITORY, BINDING)
            operation = connection.scm_product_change(PROJECT, REPOSITORY, REQUEST)
        elif interface == "cli":
            document = tmp_path / "consent.json"
            document.write_text(json.dumps(body))

            def invoke(*args):
                result = CliRunner().invoke(cli.main, ["project", "scm-product-change-grants", *args])
                assert result.exit_code == 0, result.output
                return json.loads(result.output)

            saved = invoke("configure", PROJECT, REPOSITORY, str(document))
            exact = invoke("get", PROJECT, REPOSITORY, REQUEST)
            current = invoke("current", PROJECT, REPOSITORY, BINDING)
            result = CliRunner().invoke(
                cli.main, ["project", "scm-product-changes", "get", PROJECT, REPOSITORY, REQUEST]
            )
            assert result.exit_code == 0, result.output
            operation = json.loads(result.output)
        else:
            server = create_server()

            async def invoke(name, **kwargs):
                result = await server.call_tool(name, {"project_id": PROJECT, "repository_id": REPOSITORY, **kwargs})
                assert not result.is_error, str(result)
                return result.structured_content

            saved = asyncio.run(invoke("configure_scm_product_change_grant", request=body))
            exact = asyncio.run(invoke("get_scm_product_change_grant", request_id=REQUEST))
            current = asyncio.run(invoke("get_current_scm_product_change_grant", binding_request_id=BINDING))
            operation = asyncio.run(invoke("get_scm_product_change", request_id=REQUEST))
        assert saved == exact == current == operation == receipt
        assert [(item.method, item.url.path) for item in calls] == [
            ("POST", "/api/v1/" + BASE),
            ("GET", "/api/v1/" + BASE + "/requests/" + REQUEST),
            ("GET", "/api/v1/" + BASE + "/current/" + BINDING),
            ("GET", f"/api/v1/projects/{PROJECT}/repositories/{REPOSITORY}/scm-product-changes/requests/{REQUEST}"),
        ]
        assert json.loads(calls[0].content) == body


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("outcome", ["lost", 403, 409, 422])
def test_unknown_write_and_missing_exact_read_never_trigger_another_post(outcome, interface, monkeypatch, tmp_path):
    calls = []
    body = grant()
    original = copy.deepcopy(body)

    def transport(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "No retained receipt"})
        if outcome == "lost":
            raise httpx.ReadTimeout("Offline lost delivery")
        return httpx.Response(outcome, json={"detail": "Consent not confirmed"})

    with connection_with(transport) as connection:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        if interface == "sdk":
            with pytest.raises((RuntimeError, httpx.ReadTimeout)):
                connection.configure_scm_product_change_grant(PROJECT, REPOSITORY, body)
            with pytest.raises(RuntimeError, match="404"):
                connection.scm_product_change_grant(PROJECT, REPOSITORY, REQUEST)
        elif interface == "cli":
            document = tmp_path / "consent.json"
            document.write_text(json.dumps(body))
            runner = CliRunner()
            prefix = ["project", "scm-product-change-grants"]
            result = runner.invoke(cli.main, [*prefix, "configure", PROJECT, REPOSITORY, str(document)])
            assert result.exit_code != 0
            missing = runner.invoke(cli.main, [*prefix, "get", PROJECT, REPOSITORY, REQUEST])
            assert missing.exit_code != 0 and "404" in str(missing.exception)
        else:
            from mcp.server.mcpserver.exceptions import UnexpectedToolError

            server = create_server()
            with pytest.raises(UnexpectedToolError):
                asyncio.run(
                    server.call_tool(
                        "configure_scm_product_change_grant",
                        {"project_id": PROJECT, "repository_id": REPOSITORY, "request": body},
                    )
                )
            with pytest.raises(UnexpectedToolError) as missing:
                asyncio.run(
                    server.call_tool(
                        "get_scm_product_change_grant",
                        {"project_id": PROJECT, "repository_id": REPOSITORY, "request_id": REQUEST},
                    )
                )
            assert isinstance(missing.value.__cause__, RuntimeError)
            assert "404" in str(missing.value.__cause__)
    assert [item.method for item in calls] == ["POST", "GET"]
    assert body == original == json.loads(calls[0].content)


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"acknowledge_product_definition_edit": False},
        {"active": 1},
        {"allowed_actions": ["status"]},
        {"expected_binding_digest": "do-not-reflect"},
        {"component_ids": ["api", "api"]},
    ],
)
def test_invalid_consent_is_refused_before_dispatch_without_reflection(monkeypatch, tmp_path, interface, change):
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    calls = []
    body = {**grant(), **change}
    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        if interface == "sdk":
            with pytest.raises(ValueError) as failure:
                connection.configure_scm_product_change_grant(PROJECT, REPOSITORY, body)
            assert "do-not-reflect" not in str(failure.value)
        elif interface == "cli":
            document = tmp_path / "consent.json"
            document.write_text(json.dumps(body))
            result = CliRunner().invoke(
                cli.main, ["project", "scm-product-change-grants", "configure", PROJECT, REPOSITORY, str(document)]
            )
            assert result.exit_code != 0
            assert "do-not-reflect" not in result.output
        else:
            with pytest.raises(UnexpectedToolError) as failure:
                asyncio.run(
                    create_server().call_tool(
                        "configure_scm_product_change_grant",
                        {"project_id": PROJECT, "repository_id": REPOSITORY, "request": body},
                    )
                )
            assert "do-not-reflect" not in str(failure.value)
            assert "do-not-reflect" not in str(failure.value.__cause__)
    assert calls == []


def test_constructed_native_model_does_not_bypass_sdk_validation(monkeypatch):
    calls = []
    body = SCMProductChangeGrantInputV1.model_construct(**{**grant(), "allowed_actions": ["do-not-reflect"]})
    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
        with pytest.raises(ValueError) as failure:
            connection.configure_scm_product_change_grant(PROJECT, REPOSITORY, body)
    assert "do-not-reflect" not in str(failure.value)
    assert calls == []


@pytest.mark.parametrize(
    "method,arguments",
    [
        ("configure_scm_product_change_grant", ("../project", REPOSITORY, grant())),
        ("scm_product_change_grant", (PROJECT, REPOSITORY, "../request")),
        ("current_scm_product_change_grant", (PROJECT, REPOSITORY, True)),
        ("scm_product_change", (PROJECT, REPOSITORY, "../operation")),
    ],
)
def test_invalid_route_identity_cannot_dispatch(method, arguments, monkeypatch):
    calls = []
    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
        with pytest.raises(ValueError):
            getattr(connection, method)(*arguments)
    assert calls == []


async def test_mcp_grants_have_strict_schema_and_do_not_offer_signed_command_fabrication():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    assert len(tools) == 198
    names = {
        "configure_scm_product_change_grant",
        "get_scm_product_change_grant",
        "get_current_scm_product_change_grant",
        "get_scm_product_change",
    }
    write = tools["configure_scm_product_change_grant"]
    Draft202012Validator.check_schema(write.input_schema)
    Draft202012Validator(write.input_schema).validate(
        {"project_id": PROJECT, "repository_id": REPOSITORY, "request": grant()}
    )
    assert write.input_schema["properties"]["request"]["properties"]["version"]["const"] == 1
    assert (
        write.input_schema["properties"]["request"]["properties"]["acknowledge_product_definition_edit"]["const"]
        is True
    )
    assert write.annotations.read_only_hint is False and write.annotations.destructive_hint is True
    assert write.annotations.idempotent_hint is True and write.annotations.open_world_hint is True
    for name in names - {write.name}:
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.idempotent_hint is True
        assert tools[name].annotations.open_world_hint is False


async def test_all_145_prior_complete_mcp_objects_remain_literal():
    from pathlib import Path

    from mcp.types import ListToolsResult

    new = {
        "get_framework_knowledge_status",
        "check_framework_updates",
        "refresh_framework_knowledge",
        "get_framework_knowledge_refresh",
        "observe_framework_knowledge_refresh",
        "list_framework_corpora",
        "list_framework_corpus_sources",
        "search_framework_knowledge",
        "get_run_diff_index",
        "get_run_diff_file",
        "get_git_delivery_policy",
        "configure_git_delivery_policy",
        "get_git_deliveries",
        "request_git_delivery",
        "get_git_delivery_request",
        "get_git_delivery_observations",
        "get_run_activity_content",
        "configure_scm_polling_resume_grant",
        "get_scm_polling_resume_grant",
        "get_current_scm_polling_resume_grant",
        "review_scm_polling_resume",
        "request_run_creation",
        "get_run_creation_request",
        "cancel_run_creation_request",
        "get_run_creation_decision",
        "get_agent_checkpoint_candidates",
        "request_agent_checkpoint_restore",
        "get_agent_checkpoint_restore",
        "query_run_evidence",
        "configure_scm_polling_functional_grant",
        "get_scm_polling_functional_grant",
        "get_current_scm_polling_functional_grant",
        "review_scm_polling_functional",
        "configure_scm_product_change_grant",
        "get_scm_product_change_grant",
        "get_current_scm_product_change_grant",
        "get_scm_product_change",
        "get_final_outcome_candidate",
        "request_final_outcome_continuation",
        "get_final_outcome_status",
    }
    tools = await create_server().list_tools()
    previous = [tool for tool in tools if tool.name not in new | KOINON_TOOLS]
    baseline = json.loads((Path(__file__).parent / "fixtures/mcp-product-change-baseline-v1.json").read_text())
    assert len(previous) == 145 and len(tools) == 198
    expected = json.loads(baseline["wire_json"])
    routing = next(tool for tool in expected["tools"] if tool["name"] == "save_task_routing")
    bindings = routing["inputSchema"]["$defs"]["TaskRoutingInput"]["properties"]["bindings"]
    # Only the reviewed routing binding and campaign aggregate limit changes differ.
    assert bindings["maxProperties"] == 7
    bindings["maxProperties"] = 8
    for name, model in [("launch_project_campaign", "CampaignLaunch"), ("retry_project_campaign", "CampaignRetry")]:
        tool = next(tool for tool in expected["tools"] if tool["name"] == name)
        components = tool["inputSchema"]["$defs"][model]["properties"]["component_ids"]
        array = (
            next(value for value in components["anyOf"] if value.get("type") == "array")
            if "anyOf" in components
            else components
        )
        assert array.pop("maxItems") == 50
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.work_duration_models import WorkDurationPolicyV2

    runner = SandboxInput.model_json_schema()
    additions = ("process_monitor", "monitor_owner_grace_seconds", "monitor_timeout_seconds", "work_duration")
    for name in ("save_repository_runner", "probe_runner"):
        tool = next(item for item in expected["tools"] if item["name"] == name)
        definitions = tool["inputSchema"]["$defs"]
        properties = definitions["SandboxInput"]["properties"]
        assert runner["properties"].keys() - properties.keys() == set(additions)
        binding = properties.pop("pool_binding")
        properties.update({field: runner["properties"][field] for field in additions})
        properties["pool_binding"] = binding
        definitions["WorkDurationPolicyV2"] = WorkDurationPolicyV2.model_json_schema()
    for name, (retained, current) in KNOWLEDGE_DESCRIPTION_CHANGES.items():
        tool = next(item for item in expected["tools"] if item["name"] == name)
        assert tool["description"] == retained
        tool["description"] = current
    assert ListToolsResult(tools=previous).model_dump_json(by_alias=True, exclude_none=True) == json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")
    )


async def test_campaign_schemas_accept_large_explicit_selection_and_still_require_nonempty_lists():
    from pydantic import ValidationError

    from firefly_forge.project_campaigns import CampaignLaunch, CampaignRetry

    tools = {tool.name: tool for tool in await create_server().list_tools()}
    for name, model, parameters, body in [
        (
            "launch_project_campaign",
            CampaignLaunch,
            {"project_id": PROJECT},
            {"request_id": REQUEST, "brief": "Maintain explicit components"},
        ),
        (
            "retry_project_campaign",
            CampaignRetry,
            {"project_id": PROJECT, "campaign_id": BINDING},
            {"request_id": REQUEST},
        ),
    ]:
        request = {**body, "component_ids": [f"component-{index}" for index in range(64)]}
        assert model.model_validate(request).component_ids == request["component_ids"]
        validator = Draft202012Validator(tools[name].input_schema)
        validator.validate({**parameters, "request": request})
        empty = {**body, "component_ids": []}
        with pytest.raises(ValidationError):
            model.model_validate(empty)
        assert not validator.is_valid({**parameters, "request": empty})
