# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated append-only location history selects one physical storage leaf."""

from __future__ import annotations

import copy
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from firefly_forge.journal import digest
from firefly_forge.native_location_files import NativeStorageIdentity
from firefly_forge.native_location_models import NativeLocationCandidateV1, NativeLocationEventV1
from firefly_forge.source_catalog import _json

Data = dict[str, Any]


def location_binding(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


def location_record_id(event: Data) -> UUID:
    value = NativeLocationEventV1.model_validate(event)
    return uuid5(NAMESPACE_URL, "firefly-forge:native-location:" + value.request.request_id + ":" + value.kind)


def location_event(
    kind: Literal["native_location_intent", "native_location_receipt"],
    sequence: int,
    request: Data,
    candidate: Data,
    previous_hash: str | None,
    intent_digest: str | None,
    identity: NativeStorageIdentity,
) -> Data:
    value = dict(
        version=1,
        kind=kind,
        sequence=sequence,
        request=request,
        candidate=candidate,
        previous_hash=previous_hash,
        intent_digest=intent_digest,
    )
    value["digest"] = digest(value)
    value["signature"] = identity.sign(_json(value))
    return NativeLocationEventV1.model_validate(value).model_dump(mode="json")


@dataclass(frozen=True)
class NativeLocationState:
    storage_binding: str
    receipt_digest: str | None
    candidate: NativeLocationCandidateV1 | None
    history: tuple[Data, ...] = ()


class NativeLocationUnknown(ValueError):
    def __init__(self, request_id: str, intent_digest: str) -> None:
        super().__init__("Native location outcome is incomplete; inspect the original request without replay")
        self.request_id, self.intent_digest = request_id, intent_digest


def resolve_native_location(
    base: Data, events: list[Data], identity: NativeStorageIdentity, database_fingerprint: str
) -> NativeLocationState:
    native_identity = base.get("storage_identity")
    if (
        base.get("mode") != "combined"
        or type(base.get("owner_uid")) is not int
        or base["owner_uid"] != os.getuid()
        or type(native_identity) is not str
        or not identity.matches(native_identity)
        or type(base.get("storage_binding")) is not str
    ):
        raise ValueError("Native relocation requires its unchanged combined topology, owner and retained identity")
    identity.fence()
    state = NativeLocationState(base["storage_binding"], None, None)
    previous = None
    pending: NativeLocationEventV1 | None = None
    completed: set[str] = set()
    for sequence, raw in enumerate(events, 1):
        event = NativeLocationEventV1.model_validate(raw)
        if (
            _json(raw) != _json(event.model_dump(mode="json"))
            or event.sequence != sequence
            or event.previous_hash != previous
            or digest({k: v for k, v in raw.items() if k not in {"digest", "signature"}}) != event.digest
            or identity.sign(_json({k: v for k, v in raw.items() if k != "signature"})) != event.signature
            or event.request.candidate_digest != event.candidate.digest
            or event.candidate.base_topology_digest != digest(base)
            or event.candidate.database_fingerprint != database_fingerprint
            or event.candidate.owner_uid != base["owner_uid"]
            or event.candidate.storage_identity_sha256 != identity.sha256
        ):
            raise ValueError("Native location history differs from its authenticated immutable chain")
        if event.kind == "native_location_intent":
            if (
                pending is not None
                or event.intent_digest is not None
                or event.request.request_id in completed
                or event.candidate.previous_receipt_digest != state.receipt_digest
                or location_binding(event.candidate.plan.source_storage) != state.storage_binding
            ):
                raise ValueError("Native location transfer cannot fork or reuse a spent request")
            pending = event
        else:
            if (
                pending is None
                or event.intent_digest != pending.digest
                or _json(event.request.model_dump()) != _json(pending.request.model_dump())
                or _json(event.candidate.model_dump()) != _json(pending.candidate.model_dump())
            ):
                raise ValueError("Native location receipt requires its exact preceding maintenance intent")
            state = NativeLocationState(
                location_binding(event.candidate.plan.target_storage), event.digest, event.candidate
            )
            completed.add(event.request.request_id)
            pending = None
        previous = event.digest
    if pending is not None:
        raise NativeLocationUnknown(pending.request.request_id, pending.digest)
    identity.fence()
    return NativeLocationState(
        state.storage_binding, state.receipt_digest, state.candidate, tuple(copy.deepcopy(events))
    )
