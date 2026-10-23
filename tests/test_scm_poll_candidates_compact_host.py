# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Candidate reads expose bounded verified references, never retained dialogue trees."""

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from test_scm_polling import poll_case as poll_case

from firefly_forge.journal import digest
from firefly_forge.oauth_credential_fingerprints import OAuthCredentialMatcher


@pytest.mark.parametrize("context_kind", ["wide", "deep"])
async def test_candidates_admit_compact_references_with_65_retained_dialogues(poll_case, context_kind):
    async with poll_case() as c:
        await c.ready()
        original = next(iter(c.db["inbox"].values())).data
        context = {"public_note": "retained discussion"}
        if context_kind == "wide":
            context = {"turns": [{"reply": "retained discussion"} for _ in range(200)]}
        else:
            for _ in range(30):
                context = {"parent": context}
        c.db["inbox"].clear()
        ids = []
        for _ in range(65):
            key = uuid4()
            data = {**copy.deepcopy(original), "id": key.hex, "conversation": context}
            c.db["inbox"][key.hex] = SimpleNamespace(id=key, repository_id=key.__class__(c.repository), data=data)
            ids.append(key.hex)
        matcher = OAuthCredentialMatcher(Fernet.generate_key().decode(), "public_candidate_fixture")

        async def admit(value):
            assert not matcher.contains(value, [])

        c.store.admit_credentials = admit
        c.store.reject_credentials = lambda value: matcher.contains(value, [])
        result = await c.service.candidates(c.project, c.repository)
        assert [item["id"] for item in result["items"]] == list(reversed(ids))[:50]
        assert all(set(item) == {"id", "verification", "reference_digest"} for item in result["items"])
        assert all(item["verification"] == original["verification"] for item in result["items"])
        assert all(item["reference_digest"] == digest(original["verification"]) for item in result["items"])
        assert all("conversation" in row.data for row in c.db["inbox"].values())
        assert not c.state["posts"] and not c.db["journal"]
