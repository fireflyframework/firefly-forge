# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reader changes preserve the approved historical query and proof identity."""

import hashlib
import inspect
from pathlib import Path

import pytest

from firefly_forge import invalid_read_continuity as continuity
from firefly_forge.journal import digest
from firefly_forge.run_evidence import EvidenceQuery

HISTORICAL = "a259f91531882f1370cd2eab5aee98c5e1e76b3cca4084e3abfa05da77d77344"
SUCCESSOR = "2712e480fb4b6a21f0ff999d5b8fa233d5087278d6b755f12a90e90889649e30"


def test_only_reviewed_reader_sources_share_the_exact_historical_query_identity():
    continuity.require_legacy_query_validator()
    assert continuity.LEGACY_QUERY_SHA256 == HISTORICAL
    assert continuity.APPROVED_QUERY_VALIDATORS == {HISTORICAL: HISTORICAL, SUCCESSOR: HISTORICAL}
    assert hashlib.sha256(inspect.getsource(EvidenceQuery).encode()).hexdigest() == (
        "7f026a7fb2a7f777e75fd2a1ea289f6b641a9563243011bc8d9f9b303e608f54"
    )
    assert digest(EvidenceQuery.model_json_schema()) == (
        "bbcb3a2fa1baa1a01ba98e599a7426cbf8fed0370f06c20a80a4eb5da4e842f9"
    )


@pytest.mark.parametrize("change", ["one_byte", "query_class", "historical_identity"])
def test_changed_validator_or_historical_identity_is_not_inferred_compatible(monkeypatch, change):
    original = Path.read_bytes
    if change == "historical_identity":
        monkeypatch.setattr(continuity, "LEGACY_QUERY_SHA256", "0" * 64)
    else:

        def changed(path):
            raw = original(path)
            if path == Path(continuity.__file__).with_name("run_evidence.py"):
                return raw + b"\n" if change == "one_byte" else raw.replace(b"le=50", b"le=51", 1)
            return raw

        monkeypatch.setattr(Path, "read_bytes", changed)
    with pytest.raises(ValueError, match="historical input validator"):
        continuity.require_legacy_query_validator()
