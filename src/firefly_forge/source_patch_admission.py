# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The existing bounded Git patch is a distinct flat-text credential-admission surface."""

from dataclasses import dataclass

MAX_SOURCE_PATCH_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class SourcePatch:
    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise TypeError("Source patch admission requires flat text")
        try:
            size = len(self.text.encode())
        except UnicodeError:
            raise ValueError("Source patch admission requires valid Unicode") from None
        if size > MAX_SOURCE_PATCH_BYTES:
            raise ValueError("Source patch admission exceeds the bounded Git patch")
