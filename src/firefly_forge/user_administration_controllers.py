# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Administrator-only identity lifecycle; no password or project-grant shortcuts."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.user_administration import (
    AdministrationReconciliation,
    AdministrationRecovery,
    UserAdministration,
    UserEnabledInput,
    UserInviteInput,
    UserProvisionInput,
)


def admin_principal() -> Principal:
    actor = CURRENT_PRINCIPAL.get()
    if actor is None:
        raise PermissionError("Identity administration requires an authenticated administrator")
    return actor


@rest_controller
@request_mapping("/api/v1/identity")
class UserAdministrationController:
    def __init__(self, administration: UserAdministration) -> None:
        self.administration = administration

    @get_mapping("/administration")
    def catalog(self) -> dict[str, Any]:
        return self.administration.catalog()

    @get_mapping("/operations")
    async def operations(self) -> list[dict[str, Any]]:
        return await self.administration.receipts()

    @put_mapping("/users/{id}")
    async def enabled(self, id: PathVar[str], body: Body[UserEnabledInput]) -> dict[str, Any]:
        return await self.administration.set_enabled(id, body.enabled, admin_principal())

    @post_mapping("/users", status_code=202)
    async def provision(self, body: Body[UserProvisionInput]) -> dict[str, Any]:
        return await self.administration.provision(body, admin_principal())

    @post_mapping("/users/{id}/invite", status_code=202)
    async def invite(self, id: PathVar[str], body: Body[UserInviteInput]) -> dict[str, Any]:
        return await self.administration.invite(id, body, admin_principal())

    @post_mapping("/operations/{id}/reconcile")
    async def reconcile(self, id: PathVar[str], body: Body[AdministrationReconciliation]) -> dict[str, Any]:
        return await self.administration.reconcile(id, body, admin_principal())

    @post_mapping("/operations/{id}/recover")
    async def recover(self, id: PathVar[str], body: Body[AdministrationRecovery]) -> dict[str, Any]:
        return await self.administration.recover_operation(id, body, admin_principal())
