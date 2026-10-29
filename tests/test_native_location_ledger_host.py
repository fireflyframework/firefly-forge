# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One append-only location leaf excludes forks, forged state and incomplete transfers."""

import copy
import hashlib
import os

import pytest

from firefly_forge.journal import digest
from firefly_forge.native_location_files import read_storage_identity
from firefly_forge.native_location_runtime import location_event, resolve_native_location


@pytest.fixture
def ledger_case(tmp_path):
    root = tmp_path / "new"
    root.mkdir(mode=0o700)
    marker = root / "runtime-storage.identity"
    marker.write_bytes(b"a" * 64)
    marker.chmod(0o600)
    identity = read_storage_identity(root)
    base = {
        "mode": "combined",
        "owner_uid": os.getuid(),
        "storage_identity": "a" * 64,
        "storage_binding": hashlib.sha256(b"/retained/storage").hexdigest(),
    }
    candidate = {
        "version": 1,
        "kind": "native_location_candidate",
        "plan": {
            "version": 1,
            "source_storage": "/retained/storage",
            "target_storage": str(root),
            "workspaces": [],
            "verification_runtimes": [],
        },
        "postgres_data_directory": "/retained/postgresql-data",
        "owner_uid": os.getuid(),
        "base_topology_digest": digest(base),
        "database_fingerprint": "b" * 64,
        "storage_identity_sha256": identity.sha256,
        "previous_receipt_digest": None,
        "preserved_native_digest": "c" * 64,
        "filesystem_proof_digest": "d" * 64,
        "execution_authorized": False,
    }
    candidate["digest"] = digest(candidate)
    request = {
        "version": 1,
        "request_id": "1" * 32,
        "candidate_digest": candidate["digest"],
        "acknowledge_stopped_runtime": True,
        "acknowledge_history_preserved": True,
        "acknowledge_no_execution_grant": True,
    }
    return root, identity, base, candidate, request


def events(c):
    _, identity, _, candidate, request = c
    intent = location_event("native_location_intent", 1, request, candidate, None, None, identity)
    receipt = location_event(
        "native_location_receipt", 2, request, candidate, intent["digest"], intent["digest"], identity
    )
    return [intent, receipt]


def test_exact_completed_transfer_selects_new_leaf_without_rewriting_original_topology(ledger_case):
    root, identity, base, _, _ = ledger_case
    before = copy.deepcopy(base)
    value = resolve_native_location(base, events(ledger_case), identity, "b" * 64)
    assert value.storage_binding == hashlib.sha256(str(root).encode()).hexdigest()
    assert value.receipt_digest == events(ledger_case)[1]["digest"]
    assert base == before


@pytest.mark.parametrize(
    "change",
    [
        "signature",
        "request",
        "sequence_float",
        "owner_float",
        "database",
        "base",
        "identity",
        "fork",
        "missing_intent",
        "incomplete",
    ],
)
def test_location_history_refuses_tampering_forks_and_unknown_outcomes(ledger_case, change):
    _, identity, base, _, _ = ledger_case
    history = events(ledger_case)
    database = "b" * 64
    if change == "signature":
        history[1]["signature"] = "0" * 64
    elif change == "request":
        history[1]["request"]["request_id"] = "2" * 32
    elif change == "sequence_float":
        history[1]["sequence"] = 2.0
    elif change == "owner_float":
        base["owner_uid"] = float(os.getuid())
    elif change == "database":
        database = "e" * 64
    elif change == "base":
        base["storage_binding"] = "f" * 64
    elif change == "identity":
        base["storage_identity"] = "b" * 64
    elif change == "fork":
        history += copy.deepcopy(history)
    elif change == "missing_intent":
        history = history[1:]
    else:
        history = history[:1]
    with pytest.raises(ValueError):
        resolve_native_location(base, history, identity, database)
