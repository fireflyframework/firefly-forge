# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared adapters through actual role-API/PG, never a synthetic successful service."""

import asyncio
import contextlib
import copy
import json
from uuid import uuid4

import pytest
from click.testing import CliRunner
from test_scm_product_change_native import native_product  # noqa: F401
from test_scm_resume_native import native_resume  # noqa: F401

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.scm_product_change_models import SCMProductChangeGrantInputV1


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_product_change_adapters_use_real_role_api_and_exact_signed_result(
    native_product,  # noqa: F811
    monkeypatch,
    tmp_path,
    interface,
):
    p = native_product
    c = p.case
    before_run = c.http.portal.call(c.store.get_run, c.run["id"])
    before_wal = c.http.portal.call(c.journal.entries, c.run["id"])
    before_comments = copy.deepcopy(c.comments)
    body = SCMProductChangeGrantInputV1(
        version=1,
        request_id=uuid4().hex,
        expected_previous_digest=None,
        expected_project_binding_digest=c.binding["project_binding_digest"],
        expected_binding_request_id=c.binding_body["request_id"],
        expected_binding_digest=c.binding["digest"],
        component_ids=[item["id"] for item in c.project["definition"]["components"]],
        allowed_actions=["product_change"],
        active=True,
        acknowledge_product_definition_edit=True,
    ).model_dump(mode="json")
    connection = ForgeClient("http://testserver", "owned-native-resume-bootstrap-token")
    connection.http.close()
    # The existing role-API TestClient owns genuine login/XSRF/current native identity.
    # ForgeClient still builds every actual route; no successful API/service result is substituted.
    connection.http = c.http
    monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
    original_http = c.http.request
    calls = []

    def native_request(method, path, **kwargs):
        calls.append((method, path))
        kwargs.pop("timeout", None)
        return original_http(method, "/api/v1/" + str(path).lstrip("/"), **kwargs)

    monkeypatch.setattr(c.http, "request", native_request)

    def invoke(name, value):
        if interface == "sdk":
            return getattr(connection, name)(c.project["id"], c.repo["id"], value)
        if interface == "mcp":
            names = {
                "configure_scm_product_change_grant": ("configure_scm_product_change_grant", "request"),
                "scm_product_change_grant": ("get_scm_product_change_grant", "request_id"),
                "current_scm_product_change_grant": ("get_current_scm_product_change_grant", "binding_request_id"),
                "scm_product_change": ("get_scm_product_change", "request_id"),
            }
            tool, key = names[name]
            result = asyncio.run(
                create_server().call_tool(
                    tool, {"project_id": c.project["id"], "repository_id": c.repo["id"], key: value}
                )
            )
            assert not result.is_error, str(result)
            return result.structured_content
        groups = {
            "configure_scm_product_change_grant": ("scm-product-change-grants", "configure"),
            "scm_product_change_grant": ("scm-product-change-grants", "get"),
            "current_scm_product_change_grant": ("scm-product-change-grants", "current"),
            "scm_product_change": ("scm-product-changes", "get"),
        }
        group, action = groups[name]
        if isinstance(value, dict):
            document = tmp_path / "owned-consent.json"
            document.write_text(json.dumps(value))
            value = str(document)
        result = CliRunner().invoke(cli.main, ["project", group, action, c.project["id"], c.repo["id"], value])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    grant = invoke("configure_scm_product_change_grant", body)
    assert invoke("scm_product_change_grant", body["request_id"]) == grant
    assert invoke("current_scm_product_change_grant", c.binding_body["request_id"]) == grant
    assert [method for method, _ in calls] == ["POST", "GET", "GET"]
    # Restore the client's original direct HTTP paths while normal signed ingress processes the command.
    monkeypatch.setattr(c.http, "request", original_http)
    command = p.command(grant)
    receipt = c.process(p.receive(command))["action_result"]
    monkeypatch.setattr(c.http, "request", native_request)
    assert invoke("scm_product_change", command["change"]["request_id"]) == receipt
    assert [method for method, _ in calls] == ["POST", "GET", "GET", "GET"]
    monkeypatch.setattr(c.http, "request", original_http)
    assert c.http.portal.call(c.store.get_run, c.run["id"]) == before_run
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before_wal
    assert c.comments == before_comments
    assert c.http.get(p.path + "/scm-product-changes/requests/" + uuid4().hex).status_code == 404
