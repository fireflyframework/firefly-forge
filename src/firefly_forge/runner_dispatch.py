# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native DI key for explicitly selected runner backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from firefly_forge.runner_kubernetes import KubernetesAuthority
    from firefly_forge.runner_pool_dispatch import RunnerDockerAuthority


class RunnerPoolDispatcher(ABC):
    authority: RunnerDockerAuthority | KubernetesAuthority

    @abstractmethod
    async def verify_daemon(self) -> None: ...

    @abstractmethod
    async def verify_images(self) -> None: ...
