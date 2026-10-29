# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Conversation files use identical scoped API contracts across client transports."""

import base64
import contextlib
import json

import httpx
import pytest
from click.testing import CliRunner
from test_client_task_profiles import connection

from firefly_forge import cli
from firefly_forge.mcp_server import create_server

REPOSITORY, CONVERSATION, ATTACHMENT = "1" * 32, "2" * 32, "3" * 32


def upload():
    return {
        "request_id": "a" * 32,
        "filename": "diseño.txt",
        "content_base64": base64.b64encode(b"Bounded business context").decode(),
    }


def metadata():
    return {
        "id": ATTACHMENT,
        "request_id": "a" * 32,
        "filename": "diseño.txt",
        "mime_type": "text/plain",
        "kind": "document",
        "content_bytes": 24,
        "sha256": "b" * 64,
        "status": "ready",
        "text_chars": 24,
        "truncated": False,
        "created_at": "2026-10-12T00:00:00Z",
    }


def response(request, body):
    value = {**metadata(), "raw_bytes": "private source", "text": "private extracted text", "image": "private pixels"}
    return httpx.Response(
        200, json=[value] if request.method == "GET" and request.url.path.endswith("attachments") else value
    )


def test_sdk_upload_list_and_get_only_metadata_and_exact_scoped_paths():
    seen = []
    with connection(seen, response) as client:
        assert client.upload_assistant_attachment(REPOSITORY, CONVERSATION, upload()) == metadata()
        assert client.assistant_attachments(REPOSITORY, CONVERSATION) == [metadata()]
        assert client.assistant_attachment(REPOSITORY, CONVERSATION, ATTACHMENT) == metadata()
    path = f"/api/v1/repositories/{REPOSITORY}/assistant/conversations/{CONVERSATION}/attachments"
    assert seen == [("POST", path, upload()), ("GET", path, None), ("GET", path + "/" + ATTACHMENT, None)]


@pytest.mark.parametrize("values", [{"filename": "../private.txt"}, {"content_base64": "!!!?"}, {"request_id": "bad"}])
def test_sdk_invalid_upload_rejected_before_http_without_source_in_error(values):
    seen = []
    with connection(seen, response) as client, pytest.raises(ValueError) as error:
        client.upload_assistant_attachment(REPOSITORY, CONVERSATION, {**upload(), **values})
    assert upload()["content_base64"] not in str(error.value)
    assert seen == []


def test_sdk_upload_error_cannot_reflect_raw_content():
    seen = []

    def rejected(request, body):
        return httpx.Response(422, json={"detail": body["content_base64"]})

    with connection(seen, rejected) as client, pytest.raises(ValueError) as error:
        client.upload_assistant_attachment(REPOSITORY, CONVERSATION, upload())
    assert upload()["content_base64"] not in str(error.value)


def test_cli_selected_local_file_is_bounded_and_receipt_has_no_raw_source(tmp_path, monkeypatch):
    path = tmp_path / "diseño.txt"
    path.write_text("Bounded business context")
    seen = []
    with connection(seen, response) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        args = ["assistant", "upload", REPOSITORY, CONVERSATION, "--file", str(path), "--request-id", "a" * 32]
        result = CliRunner().invoke(cli.main, args)
        assert result.exit_code == 0, result.output
        assert seen[0][2] == upload()
        assert "private extracted text" not in result.output and upload()["content_base64"] not in result.output
        seen.clear()
        path.write_bytes(b"x" * 1_000_001)
        assert CliRunner().invoke(cli.main, args).exit_code != 0
        assert seen == []


def test_cli_rejects_link_and_conflicting_selection_without_http(tmp_path, monkeypatch):
    path = tmp_path / "private.txt"
    path.write_text("Selected source")
    link = tmp_path / "link.txt"
    link.symlink_to(path)
    seen = []
    with connection(seen) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        rejected = CliRunner().invoke(
            cli.main,
            ["assistant", "upload", REPOSITORY, CONVERSATION, "--file", str(link), "--request-id", "a" * 32],
        )
        assert rejected.exit_code != 0
        rejected = CliRunner().invoke(
            cli.main,
            [
                "assistant",
                "ask",
                REPOSITORY,
                CONVERSATION,
                "--request-id",
                "a" * 32,
                "--message",
                "Review",
                "--attachment",
                ATTACHMENT,
                "--detach-attachments",
            ],
        )
        assert rejected.exit_code != 0
    assert seen == []


@pytest.mark.parametrize(
    "flags,expected", [([], None), (["--detach-attachments"], []), (["--attachment", ATTACHMENT], [ATTACHMENT])]
)
def test_cli_turn_preserves_inherit_detach_and_explicit_selection(monkeypatch, flags, expected):
    seen = []
    with connection(seen) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        result = CliRunner().invoke(
            cli.main,
            [
                "assistant",
                "ask",
                REPOSITORY,
                CONVERSATION,
                "--request-id",
                "a" * 32,
                "--message",
                "Review journey",
                *flags,
            ],
        )
        assert result.exit_code == 0, result.output
    session_path = f"/api/v1/repositories/{REPOSITORY}/assistant/conversations/{CONVERSATION}"
    assert [(method, path) for method, path, _ in seen] == [("GET", session_path), ("POST", session_path + "/turns")]
    assert seen[0][2] is None
    assert seen[1][2]["attachment_ids"] == expected


async def test_mcp_file_schemas_and_real_invocation_are_metadata_only(monkeypatch):
    server = create_server()
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert {"list_product_attachments", "upload_product_attachment", "get_product_attachment"} <= tools.keys()
    schema = tools["upload_product_attachment"].input_schema
    assert "content_base64" in str(schema) and "1333336" in str(schema)
    assert "attachment_ids" in tools["request_product_design_turn"].input_schema["properties"]
    seen = []
    with connection(seen, response) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        result = await server.call_tool(
            "upload_product_attachment",
            {"repository_id": REPOSITORY, "conversation_id": CONVERSATION, "request": upload()},
        )
        assert not result.is_error
        assert "private extracted text" not in str(result) and upload()["content_base64"] not in str(result)
        result = await server.call_tool(
            "request_product_design_turn",
            {
                "repository_id": REPOSITORY,
                "conversation_id": CONVERSATION,
                "request_id": "a" * 32,
                "message": "Review",
                "attachment_ids": [],
            },
        )
        assert not result.is_error
    session_path = f"/api/v1/repositories/{REPOSITORY}/assistant/conversations/{CONVERSATION}"
    assert [(method, path) for method, path, _ in seen] == [
        ("POST", session_path + "/attachments"),
        ("GET", session_path),
        ("POST", session_path + "/turns"),
    ]
    assert seen[0][2] == upload() and seen[1][2] is None and seen[2][2]["attachment_ids"] == []


async def test_mcp_rejected_oversized_upload_never_reflects_base64_content(monkeypatch):
    seen = []
    canary = base64.b64encode(b"private rejected source canary").decode()
    with connection(seen) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises(Exception) as error:
            await create_server().call_tool(
                "upload_product_attachment",
                {
                    "repository_id": REPOSITORY,
                    "conversation_id": CONVERSATION,
                    "request": {**upload(), "content_base64": canary * 40_000},
                },
            )
    assert canary[:16] not in str(error.value)
    assert seen == []


def test_native_sdk_cli_and_mcp_share_actual_pg_attachment_receipt(tmp_path, postgres_schema, monkeypatch):
    import asyncio

    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.client import ForgeClient
    from firefly_forge.engine import Engine
    from firefly_forge.models import RepositoryInput

    async def idle(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    monkeypatch.setenv("FORGE_AUTH_MODE", "local_bootstrap")
    source = tmp_path / "source"
    source.mkdir()
    token = "native-attachment-parity-operator-token"
    app = create_application(tmp_path / "factory", token)
    with TestClient(
        app,
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        client = ForgeClient("http://testserver", token)
        client.http.close()
        client.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        repo = client.add_repository(RepositoryInput(name="Product files", url=str(source)))
        session = client.create_assistant_conversation(repo["id"])
        other = client.create_assistant_conversation(repo["id"])
        receipt = client.upload_assistant_attachment(repo["id"], session["id"], upload())
        assert receipt["status"] == "ready" and receipt["kind"] == "document" and receipt["text_chars"] == 24
        assert client.assistant_attachments(repo["id"], session["id"]) == [receipt]
        assert client.assistant_attachment(repo["id"], session["id"], receipt["id"]) == receipt
        assert client.assistant_attachments(repo["id"], other["id"]) == []
        with pytest.raises(RuntimeError):
            client.assistant_attachment(repo["id"], other["id"], receipt["id"])
        result = asyncio.run(
            create_server().call_tool(
                "upload_product_attachment",
                {"repository_id": repo["id"], "conversation_id": session["id"], "request": upload()},
            )
        )
        assert not result.is_error and receipt["id"] in str(result)
        result = CliRunner().invoke(cli.main, ["assistant", "files", repo["id"], session["id"]])
        assert result.exit_code == 0 and json.loads(result.output) == [receipt]
        assert "Bounded business context" not in result.output
        assert client.assistant_conversation(repo["id"], session["id"])["turns"] == []
