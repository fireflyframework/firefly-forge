# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reader guidance and native proof must bind the strict server argument contract."""

import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from source_edit_qualification_fixtures import register_fixture_profile

from firefly_forge import source_edit_qualification as qualification
from firefly_forge.coding_check_transport import CodingCheckServer
from firefly_forge.prompts import prompt_catalog

PROMPT_INPUTS = (
    "prompts.py",
    "resources/prompts/v1/manifest.json",
    *(
        f"resources/prompts/v1/coding-runtime/source-{name}-{'1.0.1' if name == 'read' else '1.0.2'}.yaml"
        for name in ("list", "read", "search")
    ),
)


async def test_native_reader_descriptions_explain_strict_server_bounds_and_keep_old_versions():
    async def read(request):
        raise AssertionError("Listing tools must never read source")

    server = CodingCheckServer(None, expires_at_ms=int(time.time() * 1000) + 30000, source_callback=read)
    app = server.application(19433)
    headers = {"authorization": "Bearer " + server.token, "accept": "application/json, text/event-stream"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19433") as client,
    ):
        response = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    catalog = prompt_catalog()
    for tool, resource in (("list_files", "list"), ("read_file", "read"), ("search_text", "search")):
        name = "coding-runtime/source-" + resource
        description = tools[tool]["description"]
        assert description == catalog.text(name, version="1.0.1" if resource == "read" else "1.0.2")
        assert description != catalog.text(name, version="1.0.0")
        assert "32 lowercase hexadecimal" in description
        assert "Only declared fields" in description
    assert "1 to 1000 characters" in tools["read_file"]["description"]
    search = tools["search_text"]["description"]
    for bound in ("1 to 500 characters", "0 to 1000 characters", "integer from 1 to 100", "default 50"):
        assert bound in search


def test_legacy_sixteen_outcome_proof_cannot_admit_native_readers(tmp_path, monkeypatch):
    executable = tmp_path / "public-fixture"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)
    legacy = replace(
        profile,
        proof=replace(
            profile.proof,
            outcome_checks=tuple(x for x in profile.proof.outcome_checks if x != "source_reader_bounds_enforced"),
        ),
    )
    legacy = replace(legacy, proof=replace(legacy.proof, contract_digest=qualification.profile_contract_digest(legacy)))
    monkeypatch.setattr(qualification, "_profiles", lambda: (legacy,))
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(legacy)


@pytest.mark.parametrize("changed", PROMPT_INPUTS)
def test_selected_prompt_or_renderer_drift_revokes_profile_before_admission(changed, tmp_path, monkeypatch):
    assert set(PROMPT_INPUTS) <= set(qualification.LOADER_MODULES)
    root = Path(qualification.__file__).parent
    copied = tmp_path / "package"
    for name in qualification.LOADER_MODULES:
        target = copied / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
    monkeypatch.setattr(qualification, "__file__", str(copied / "source_edit_qualification.py"))
    executable = tmp_path / "public-fixture"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)
    qualification.require_profile(profile)
    target = copied / changed
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="qualified"):
        qualification.require_profile(profile)


def test_reader_bound_proof_uses_a_new_registry_and_loader_contract():
    assert qualification.REGISTRY_VERSION == 18
    assert qualification.LOADER_PROFILE == "codex-exec-restricted-source/v19"
    historical = [
        profile for profile in qualification._profiles() if profile.loader_profile == "codex-exec-restricted-source/v5"
    ]
    assert len(historical) == 1
    assert (
        qualification.historical_profile_seal(historical[0])["profile_digest"]
        == "17320f82c0f2f9ab4b65c7e3bd8b89741a3716a8896dd5f674f6409ac1490c3c"
    )
    assert {"source_edit_filesystem_policy.py", "workspace_tools.py"} <= set(qualification.LOADER_MODULES)
    assert qualification.FILESYSTEM_CHECKS == (
        "patch_add",
        "patch_update",
        "patch_move",
        "patch_delete",
        "private_read_denied",
        "private_write_denied",
        "outside_read_denied",
        "outside_write_denied",
        "case_alias_read_denied",
        "case_alias_write_denied",
        "public_env_example_allowed",
        "filesystem_groups_absent",
        "filesystem_streams_drained",
    )
