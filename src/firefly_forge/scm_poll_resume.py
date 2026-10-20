# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""API-observed continuation authority with an independent immutable consent history."""

from __future__ import annotations

from typing import Any

from pyfly.data.transaction import current_unit_of_work

from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.scm_poll_models import GitHubPollReferenceV1
from firefly_forge.scm_poll_resume_models import SCMPollResumeGrantInputV3, SCMPollResumeGrantReceiptV3
from firefly_forge.scm_polling import BINDING_KIND, SCMPolling, poll_key
from firefly_forge.scm_resume_cut import polling_resume_cut
from firefly_forge.scm_resume_models import SCMResumeGrantInputV2
from firefly_forge.scm_resume_service import SCMResumeAuthority
from firefly_forge.store import identifier

Data = dict[str, Any]


class SCMPollResumeAuthority(SCMResumeAuthority):
    state_key = "_scm_poll_resume_v3"
    event_kind = "scm_poll_resume_grant_v3"
    grant_version = 3
    request_type = SCMPollResumeGrantInputV3
    receipt_type = SCMPollResumeGrantReceiptV3
    cut_builder = staticmethod(polling_resume_cut)
    polling: SCMPolling

    async def _current_binding(self, project: str, repository: str, request_id: str) -> Data | None:
        async with self.store._unit():
            _, state = await self.polling._state(repository)
        pointer = state["requests"].get(project + ":" + request_id)
        if pointer is None:
            return None
        return await self.polling._event(pointer, BINDING_KIND, project, repository)

    async def _binding(
        self, project: str, repository: str, request: SCMResumeGrantInputV2 | SCMPollResumeGrantInputV3
    ) -> Data:
        if not isinstance(request, SCMPollResumeGrantInputV3):
            raise AccessDenied("Polling resume requires its separate transport-bound consent")
        binding = await self._current_binding(project, repository, request.expected_binding_request_id)
        if binding is None:
            raise AccessDenied("Review an exact current API-observed polling binding first")
        current = await self.polling._head(project, repository, binding["binding_key"])
        policy, state = await self.polling._policy(repository)
        await self.polling._scope(repository, policy)
        project_cut = await self.authority._project(project, repository, request.component_ids)
        observed = await self.polling._observation(project, repository, binding["request"]["observed_receipt_id"])
        if (
            current != binding
            or policy["project_id"] != project
            or not state["cursor"]["ready"]
            or observed.get("source_conflict")
            or binding["digest"] != request.expected_binding_digest
            or not binding["request"]["active"]
            or "respond" not in binding["request"]["allowed_actions"]
            or not set(request.component_ids) <= set(binding["request"]["component_ids"])
            or digest(project_cut) != request.expected_project_binding_digest
            or binding["project_binding_digest"] != digest(project_cut)
            or any(binding["verified_reference"].get(k) != v for k, v in policy["scope"].items())
        ):
            raise AccessDenied("The reviewed polling binding, connection or project scope changed")
        async with self.store._unit():
            user = await self.polling.users.find_by_id(identifier(binding["request"]["native_user_id"]))
            memberships = await self.polling.memberships.for_user(identifier(binding["request"]["native_user_id"]))
            row, _ = await self.polling._state(repository)
            if (
                user is None
                or not user.enabled
                or binding["verified_reference"]["external_actor_id"] not in row.data.get("trusted_actors", [])
                or "respond" not in row.data.get("channel_commands", [])
                or not any(
                    item.project_id.hex == project and item.active and ProjectAccess.permits(item.role, "execute")
                    for item in memberships
                )
            ):
                raise AccessDenied("The polling binding no longer has current native execution authority")
        return binding

    async def _preflight(self, context: Data) -> None:
        await super()._preflight(context)
        binding = context["binding"]
        delivery = context.get("delivery") or await self.polling._observation(
            binding["project_id"], binding["repository_id"], binding["request"]["observed_receipt_id"]
        )
        await self.polling._fresh(delivery, await self.store.repository(binding["repository_id"]))

    async def _context(self, delivery: Data) -> Data:
        command = delivery["command"]
        if command.get("version") != 2 or command.get("action") != "resume":
            raise AccessDenied("Polling continuation supports only the typed same-run resume command")
        run = await self._run_data(command["run_id"])
        project, repository, component = run.get("project_id"), run["repository_id"], run.get("component_id")
        if not project or not component or repository != delivery["repository_id"]:
            raise AccessDenied("Polling resume requires the original run project, repository and component")
        reference = GitHubPollReferenceV1.model_validate(delivery.get("verification")).model_dump(mode="json")
        retained = await self.polling._observation(project, repository, delivery["id"])
        if (
            retained != delivery
            or retained.get("source_conflict")
            or reference["payload_digest"] != delivery.get("digest")
            or reference["external_actor_id"] != delivery.get("actor")
            or reference["remote_repository_id"] != delivery.get("remote_id")
        ):
            raise AccessDenied("The API-observed continuation source or actor changed")
        binding = await self.polling._head(project, repository, poll_key(project, reference))
        grant = await self._grant(project, repository, key=binding["binding_key"])
        if grant is None or grant["digest"] != command["expected_grant_digest"] or not grant["request"]["active"]:
            raise AccessDenied("A current separately consented polling resume grant is required")
        request = SCMPollResumeGrantInputV3.model_validate(grant["request"])
        current = await self._binding(project, repository, request)
        identity_fields = (
            "provider",
            "api_origin",
            "installation_id",
            "remote_repository_id",
            "external_actor_id",
            "connection_revision",
            "connection_scope_digest",
            "transport",
        )
        if (
            current != binding
            or component not in request.component_ids
            or grant["native_user_id"] != binding["request"]["native_user_id"]
            or any(reference[k] != binding["verified_reference"][k] for k in identity_fields)
        ):
            raise AccessDenied("The polling resume grant no longer binds this account, user or component")
        async with self.store._unit():
            user = await self.polling.users.find_by_id(identifier(grant["native_user_id"]))
            memberships = await self.polling.memberships.for_user(identifier(grant["native_user_id"]))
            row, _ = await self.polling._state(repository)
            if (
                user is None
                or not user.enabled
                or delivery["actor"] not in row.data.get("trusted_actors", [])
                or "respond" not in row.data.get("channel_commands", [])
                or not any(
                    item.project_id.hex == project and item.active and ProjectAccess.permits(item.role, "execute")
                    for item in memberships
                )
            ):
                raise AccessDenied("Current polling actor and native execution permission are required")
        return {"run": run, "binding": binding, "grant": grant, "user_id": user.id.hex, "delivery": delivery}

    async def _after_admission(self, context: Data) -> None:
        await self._preflight(context)

    async def review(self, project: str, repository: str, binding_request: str, run_id: str) -> Data:
        if current_unit_of_work() is not None:
            raise ValueError("Read the polling resume review outside a caller transaction")
        actor = CURRENT_PRINCIPAL.get()
        await self.authority._reader(actor, project, repository, admin=False)
        binding = await self._current_binding(project, repository, binding_request)
        if binding is None:
            raise AccessDenied("The exact polling binding is unavailable")
        grant = await self._grant(project, repository, key=binding["binding_key"])
        if grant is None or not grant["request"]["active"]:
            raise AccessDenied("A separately consented polling resume grant is required")
        request = SCMPollResumeGrantInputV3.model_validate(grant["request"])
        binding = await self._binding(project, repository, request)
        run = await self._run_data(run_id)
        if (run.get("project_id"), run["repository_id"]) != (project, repository) or run.get(
            "component_id"
        ) not in request.component_ids:
            raise AccessDenied("The reviewed run is outside this polling grant")
        context = {"run": run, "binding": binding, "grant": grant, "user_id": grant["native_user_id"]}
        await self._original(run)
        await self._preflight(context)
        cut = await self._cut(context)
        await self.store.admit_credentials(cut)
        await self._after_admission(context)
        await self.authority._reader(actor, project, repository, admin=False)
        current_grant = await self._grant(project, repository, key=binding["binding_key"])
        if current_grant != grant or await self._binding(project, repository, request) != binding:
            raise AccessDenied("The polling continuation review cut changed")
        current_run = await self._run_data(run_id)
        await self._original(current_run)
        if await self._cut({**context, "run": current_run}) != cut:
            raise AccessDenied("The polling continuation review cut changed")
        self.store.reject_credentials(cut)
        return cut
