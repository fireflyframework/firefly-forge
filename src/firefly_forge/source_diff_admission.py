# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded streaming checks; complete source is never a credential RPC value."""

from __future__ import annotations

import codecs
import copy
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from firefly_forge.oauth_credential_fingerprints import OAuthCredentialMatcher
from firefly_forge.secrets import _ephemeral, contains_known_credential, credential_representations, sensitive_name

Data = dict[str, Any]


@dataclass(frozen=True, repr=False)
class CredentialHistorySnapshot:
    matcher: OAuthCredentialMatcher | None
    packets: tuple[Data, ...]
    credentials: tuple[str, ...]


class SourceDiffAdmission:
    def __init__(
        self,
        *,
        credentials: tuple[str, ...],
        matcher: OAuthCredentialMatcher | None,
        packets: tuple[Data, ...],
        verify_current: Callable[[], Awaitable[None]],
    ) -> None:
        self._matcher = matcher
        self._packets = copy.deepcopy(list(packets))
        if matcher is None and packets:
            raise ValueError("Authenticated credential history is unavailable")
        if matcher is not None:
            matcher.contains("", self._packets)
        if any(type(value) is not str for value in credentials):
            raise ValueError("Streaming credential admission requires exact credential text")
        self._registry = {f"TOKEN_{i}": value for i, value in enumerate(credentials) if value}
        represented = {item for value in self._registry.values() for item in credential_representations(value)}
        lengths = [len(item) for item in represented]
        if matcher is not None:
            lengths.extend(item["length"] for packet in self._packets for item in matcher._validated(packet))
        # The authenticated history protocol bounds individual representations.
        # This bounds overlap memory, independently of total source size.
        if lengths and max(lengths) > 262144:
            raise ValueError("Credential representation exceeds the streaming overlap bound")
        self._overlap = max([32, *lengths]) - 1
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._tail = ""
        self._verify_current = verify_current
        self._closed = False
        self._busy = False
        self._admitted = False

    async def __call__(self, chunk: bytes, final: bool) -> None:
        if self._closed or self._busy:
            self._closed = True
            self._admitted = False
            raise ValueError("Streaming credential admission is closed")
        self._busy = True
        try:
            if type(chunk) is not bytes or type(final) is not bool or len(chunk) > 1048576 or (final and chunk):
                raise ValueError("Streaming credential admission requires bounded exact byte chunks")
            for start in range(0, len(chunk), 65536):
                self._scan(self._decoder.decode(chunk[start : start + 65536]))
            if final:
                self._scan(self._decoder.decode(b"", final=True))
                await self._verify_current()
                if self._closed:
                    raise ValueError("Streaming credential admission is closed")
                self._closed = True
                self._admitted = True
                self._tail = ""
                self._registry.clear()
        except BaseException:
            self._closed = True
            self._admitted = False
            self._tail = ""
            self._registry.clear()
            raise
        finally:
            self._busy = False

    async def revalidate(self) -> None:
        """Recheck the original cut after native staging awaits without reopening it."""
        if not self._admitted or self._busy:
            self._admitted = False
            raise ValueError("Streaming credential admission was not completed")
        self._busy = True
        try:
            await self._verify_current()
            if not self._admitted:
                raise ValueError("Streaming credential admission was not completed")
        except BaseException:
            self._admitted = False
            raise
        finally:
            self._busy = False

    def finish_document(self) -> None:
        """Separate path/blob documents without changing their immutable admission cut."""
        if self._closed or self._busy:
            self._closed = True
            self._admitted = False
            raise ValueError("Streaming credential admission is closed")
        try:
            self._scan(self._decoder.decode(b"", final=True))
        except BaseException:
            self._closed = True
            self._admitted = False
            self._registry.clear()
            raise
        finally:
            self._tail = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _scan(self, text: str) -> None:
        window = self._tail + text
        if contains_known_credential(window, self._registry) or (
            self._matcher is not None and self._matcher._contains_strings([window], self._packets)
        ):
            raise ValueError("The source diff contains a configured or historical credential")
        self._tail = window[-self._overlap :]


def _history_identity(snapshot: CredentialHistorySnapshot) -> tuple[Any, ...]:
    import json

    if type(snapshot) is not CredentialHistorySnapshot:
        raise ValueError("Authenticated streaming credential history is unavailable")
    if snapshot.matcher is None and snapshot.packets:
        raise ValueError("Authenticated streaming credential history is unavailable")
    if snapshot.matcher is not None:
        snapshot.matcher.contains("", list(snapshot.packets))
    return (
        snapshot.matcher.key_id if snapshot.matcher is not None else None,
        tuple(sorted(json.dumps(p, sort_keys=True, separators=(",", ":")) for p in snapshot.packets)),
        tuple(sorted(snapshot.credentials)),
    )


async def prepare_source_diff_admission(
    store: Any, *, settings: dict[str, str], assert_current: Callable[[], Awaitable[None]]
) -> SourceDiffAdmission:
    from pyfly.data.transaction import current_unit_of_work

    from firefly_forge.store import Store, _credential_registry

    if current_unit_of_work() is not None:
        raise ValueError("Prepare streaming credential admission outside the native transaction")
    generation = store.configuration.write_generation
    registry_source = store.credential_registry
    history_source = getattr(store, "vault_source_diff_history", None)

    def current_credentials() -> tuple[str, ...]:
        if (
            store.configuration.write_generation is not generation
            or store.credential_registry is not registry_source
            or getattr(store, "vault_source_diff_history", None) is not history_source
        ):
            raise ValueError("Streaming credential sources changed")
        values = set(_ephemeral.get())
        for source in (_credential_registry(store), settings, os.environ):
            for name, value in source.items():
                if value and sensitive_name(name):
                    values.add(value)
                if "://" in value:
                    try:
                        password = urlsplit(value).password
                    except ValueError:
                        password = None
                    if password:
                        values.update((password, unquote(password)))
        if store.configuration.write_generation is not generation:
            raise ValueError("Streaming credential sources changed")
        return tuple(sorted(values))

    credentials = current_credentials()
    history = await Store.source_diff_history(store)
    identity = _history_identity(history)
    await assert_current()
    if current_credentials() != credentials:
        raise ValueError("Streaming credential sources changed")

    async def verify() -> None:
        await assert_current()
        fresh = await Store.source_diff_history(store)
        if _history_identity(fresh) != identity or current_credentials() != credentials:
            raise ValueError("Streaming credential sources or history changed")

    return SourceDiffAdmission(
        credentials=(*credentials, *history.credentials),
        matcher=history.matcher,
        packets=history.packets,
        verify_current=verify,
    )
