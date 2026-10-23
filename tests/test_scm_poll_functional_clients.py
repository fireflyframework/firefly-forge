# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""V3 polling consent adapters submit one reviewed identity and recover through explicit reads."""

import asyncio
import contextlib
import copy
import json

import httpx
import pytest
from click.testing import CliRunner
from test_scm_poll_functional_contract import grant as functional_grant

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.scm_poll_functional_models import SCMPollFunctionalGrantInputV1

PROJECT, REPOSITORY, REQUEST, BINDING = (value * 32 for value in "abcd")
BASE = f"projects/{PROJECT}/repositories/{REPOSITORY}/scm-polling/functional-grants"


def grant():
    return {**functional_grant(), "request_id": REQUEST, "expected_binding_request_id": BINDING}


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
            saved = connection.configure_scm_polling_functional_grant(PROJECT, REPOSITORY, body)
            exact = connection.scm_polling_functional_grant(PROJECT, REPOSITORY, REQUEST)
            current = connection.current_scm_polling_functional_grant(PROJECT, REPOSITORY, BINDING)
        elif interface == "cli":
            document = tmp_path / "consent.json"
            document.write_text(json.dumps(body))

            def invoke(*args):
                result = CliRunner().invoke(cli.main, ["project", "scm-polling-functional-grants", *args])
                assert result.exit_code == 0, result.output
                return json.loads(result.output)

            saved = invoke("configure", PROJECT, REPOSITORY, str(document))
            exact = invoke("get", PROJECT, REPOSITORY, REQUEST)
            current = invoke("current", PROJECT, REPOSITORY, BINDING)
        else:
            server = create_server()

            async def invoke(name, **kwargs):
                result = await server.call_tool(name, {"project_id": PROJECT, "repository_id": REPOSITORY, **kwargs})
                assert not result.is_error, str(result)
                return result.structured_content

            saved = asyncio.run(invoke("configure_scm_polling_functional_grant", request=body))
            exact = asyncio.run(invoke("get_scm_polling_functional_grant", request_id=REQUEST))
            current = asyncio.run(invoke("get_current_scm_polling_functional_grant", binding_request_id=BINDING))
        assert saved == exact == current == receipt
        assert [(item.method, item.url.path) for item in calls] == [
            ("POST", "/api/v1/" + BASE),
            ("GET", "/api/v1/" + BASE + "/requests/" + REQUEST),
            ("GET", "/api/v1/" + BASE + "/current/" + BINDING),
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
                connection.configure_scm_polling_functional_grant(PROJECT, REPOSITORY, body)
            with pytest.raises(RuntimeError, match="404"):
                connection.scm_polling_functional_grant(PROJECT, REPOSITORY, REQUEST)
        elif interface == "cli":
            document = tmp_path / "consent.json"
            document.write_text(json.dumps(body))
            runner = CliRunner()
            prefix = ["project", "scm-polling-functional-grants"]
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
                        "configure_scm_polling_functional_grant",
                        {"project_id": PROJECT, "repository_id": REPOSITORY, "request": body},
                    )
                )
            with pytest.raises(UnexpectedToolError) as missing:
                asyncio.run(
                    server.call_tool(
                        "get_scm_polling_functional_grant",
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
        {"acknowledge_separate_assistant_allowance": False},
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
                connection.configure_scm_polling_functional_grant(PROJECT, REPOSITORY, body)
            assert "do-not-reflect" not in str(failure.value)
        elif interface == "cli":
            document = tmp_path / "consent.json"
            document.write_text(json.dumps(body))
            result = CliRunner().invoke(
                cli.main, ["project", "scm-polling-functional-grants", "configure", PROJECT, REPOSITORY, str(document)]
            )
            assert result.exit_code != 0
            assert "do-not-reflect" not in result.output
        else:
            with pytest.raises(UnexpectedToolError) as failure:
                asyncio.run(
                    create_server().call_tool(
                        "configure_scm_polling_functional_grant",
                        {"project_id": PROJECT, "repository_id": REPOSITORY, "request": body},
                    )
                )
            assert "do-not-reflect" not in str(failure.value)
            assert "do-not-reflect" not in str(failure.value.__cause__)
    assert calls == []


def test_constructed_native_model_does_not_bypass_sdk_validation(monkeypatch):
    calls = []
    body = SCMPollFunctionalGrantInputV1.model_construct(**{**grant(), "allowed_actions": ["do-not-reflect"]})
    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
        with pytest.raises(ValueError) as failure:
            connection.configure_scm_polling_functional_grant(PROJECT, REPOSITORY, body)
    assert "do-not-reflect" not in str(failure.value)
    assert calls == []


@pytest.mark.parametrize(
    "method,arguments",
    [
        ("configure_scm_polling_functional_grant", ("../project", REPOSITORY, grant())),
        ("scm_polling_functional_grant", (PROJECT, REPOSITORY, "../request")),
        ("current_scm_polling_functional_grant", (PROJECT, REPOSITORY, True)),
    ],
)
def test_invalid_route_identity_cannot_dispatch(method, arguments, monkeypatch):
    calls = []
    with ForgeClient("https://forge.invalid", "fictional-local-token") as connection:
        monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
        with pytest.raises(ValueError):
            getattr(connection, method)(*arguments)
    assert calls == []
