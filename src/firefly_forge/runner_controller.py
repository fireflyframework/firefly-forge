# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Dedicated native PyFly runner API; no factory-user routes or daemon argv endpoint."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pyfly.container import bean, configuration, controller_advice, rest_controller, service
from pyfly.core import PyFlyApplication, pyfly_application
from pyfly.data.relational.sqlalchemy import SessionProvider
from pyfly.kernel.exceptions import InvalidRequestException, ValidationException
from pyfly.web import Body, OncePerRequestFilter, exception_handler, get_mapping, post_mapping, request_mapping
from pyfly.web.adapters.starlette import create_app
from pyfly.web.ports.filter import CallNext
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from firefly_forge import __version__
from firefly_forge.runner_controller_config import RunnerServerTLS
from firefly_forge.runner_git_snapshot import RunnerGitSnapshot
from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts
from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher, RunnerDockerAuthority
from firefly_forge.runner_pool_execution import RunnerPoolExecutions
from firefly_forge.runner_pool_models import (
    FactoryFence,
    RunnerInvocation,
    RunnerProjectInvocation,
    parse_invocation,
    pool_digest,
)
from firefly_forge.runner_pool_persistence import (
    ForgeRunnerDaemonRepository,
    ForgeRunnerEventRepository,
    ForgeRunnerInvocationRepository,
    ForgeRunnerPoolRepository,
)
from firefly_forge.runner_pools import RunnerPools
from firefly_forge.runner_run_liveness import RUN_LIVENESS_PROTOCOL, RunRenewalV1
from firefly_forge.secrets import contains_known_credential

UPLOAD_DEADLINE_SECONDS = 30


@dataclass(frozen=True)
class RunnerControllerSettings:
    authority: RunnerDockerAuthority
    runner_schema: str
    factory_schema: str
    token: SecretStr
    tls: RunnerServerTLS
    active: bool = False
    git_binary: Path | None = None

    def __post_init__(self) -> None:
        if self.runner_schema == self.factory_schema:
            raise ValueError("Controller schema must be distinct from the factory schema")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) for value in (self.runner_schema, self.factory_schema)):
            raise ValueError("Controller schemas must be lowercase PostgreSQL identifiers")
        if len(self.token.get_secret_value()) < 32:
            raise ValueError("A private controller credential of at least 32 characters is required")


def invocation_scope(token: SecretStr, request: dict[str, Any]) -> str:
    return hmac.new(
        token.get_secret_value().encode(), ("forge-runner-scope-v1:" + pool_digest(request)).encode(), hashlib.sha256
    ).hexdigest()


class PoolSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invocation: RunnerProjectInvocation | RunnerInvocation
    fence: FactoryFence
    archive_base64: str = Field(min_length=4, max_length=359_313_412)
    stdin_base64: str = Field(default="", max_length=2_796_204)


class PoolLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    scope: str = Field(pattern=r"^[a-f0-9]{64}$")


class PoolControl(PoolLookup):
    fence: FactoryFence


class PoolContinuation(PoolControl):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$", strict=True)
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$", strict=True)


class PoolRunRenewal(PoolControl):
    renewal: RunRenewalV1


class PoolArtifact(PoolLookup):
    kind: Literal["output", "result"]


@service
class RunnerControllerAuth(OncePerRequestFilter):
    __pyfly_order__ = -1_000_000

    def __init__(self, settings: RunnerControllerSettings) -> None:
        self.settings = settings

    async def do_filter(self, request: Any, call_next: CallNext) -> Response:
        if request.url.scheme != "https":
            return JSONResponse({"detail": "Runner control requires mutual TLS"}, status_code=403)
        expected = "Bearer " + self.settings.token.get_secret_value()
        supplied = request.headers.get("authorization", "")
        if len(supplied) > 5000 or not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Runner control authentication required"}, status_code=401)
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


class RunnerRequestLimits:
    def __init__(self, app: ASGIApp, settings: RunnerControllerSettings) -> None:
        self.app = app
        self.limit = 4 * ((settings.authority.profile.max_source_bytes + 2) // 3) + 4_194_304
        self.authorization = ("Bearer " + settings.token.get_secret_value()).encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        auth = next((value for name, value in scope["headers"] if name.lower() == b"authorization"), b"")
        if scope["scheme"] != "https" or not hmac.compare_digest(auth, self.authorization):
            await JSONResponse(
                {"detail": "Authenticated mutual TLS runner control required"},
                status_code=403 if scope["scheme"] != "https" else 401,
            )(scope, receive, send)
            return
        chunks, size = [], 0
        try:
            async with asyncio.timeout(UPLOAD_DEADLINE_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.limit:
                        await JSONResponse(
                            {"detail": "Runner upload exceeds its configured byte limit"}, status_code=413
                        )(scope, receive, send)
                        return
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"detail": "Runner upload deadline elapsed"}, status_code=408)(scope, receive, send)
            return
        delivered = False
        body = b"".join(chunks)

        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


@service
class RunnerControllerService:
    phase = 10

    def __init__(
        self,
        settings: RunnerControllerSettings,
        pools: RunnerPools,
        artifacts: RunnerPoolArtifacts,
        dispatcher: DockerPoolDispatcher,
        executions: RunnerPoolExecutions,
    ) -> None:
        self.settings, self.pools, self.artifacts, self.dispatcher = settings, pools, artifacts, dispatcher
        self.executions = executions
        self.ready = False

    async def start(self) -> None:
        await self.pools.initialize()
        if self.settings.active:
            await self.dispatcher.verify_images()
            self.ready = True

    async def stop(self) -> None:
        self.ready = False
        await self.executions.stop()

    def profile(self) -> dict[str, Any]:
        return {
            **({"workspace_protocols": ["forge-project-bundle-v1"]} if self.executions.git_snapshot else {}),
            **(
                {"run_liveness_protocols": [RUN_LIVENESS_PROTOCOL]}
                if self.settings.authority.profile.run_liveness
                else {}
            ),
            "profile": self.settings.authority.profile.model_dump(mode="json"),
            "revision": self.settings.authority.profile.revision,
            "status": "ready" if self.ready else "runtime_unverified" if self.settings.active else "inactive",
        }

    async def submit(self, request: PoolSubmission) -> dict[str, Any]:
        if not self.settings.active or not self.ready:
            raise RuntimeError("Administrator runner profile is inactive or runtime unverified")
        if contains_known_credential(request.invocation.model_dump_json(), self.artifacts.credential_registry()):
            raise ValueError("Invocation metadata contains a private controller credential")
        archive = base64.b64decode(request.archive_base64, validate=True)
        stdin = base64.b64decode(request.stdin_base64, validate=True)
        return await self.executions.accept(request.invocation, request.fence, archive, stdin)

    async def observe(self, request: PoolLookup) -> dict[str, Any]:
        current = await self.pools.observe(self.settings.authority.profile, request.execution_id)
        if not hmac.compare_digest(request.scope, invocation_scope(self.settings.token, current["request"])):
            raise PermissionError("Invocation scope differs from the frozen request")
        return current

    async def artifact(self, request: PoolArtifact) -> dict[str, Any]:
        current = await self.observe(request)
        receipt = (current.get("receipt") or {}).get(request.kind)
        if not receipt:
            raise KeyError("Runner artifact is not retained")
        invocation = parse_invocation(current["request"])
        data = self.artifacts.read(self.settings.authority.profile, invocation, request.kind, receipt)
        return {"receipt": receipt, "data_base64": base64.b64encode(data).decode()}

    async def continue_invocation(self, request: PoolContinuation) -> dict[str, Any]:
        if not self.settings.active or not self.ready:
            raise RuntimeError("Administrator runner profile is inactive or runtime unverified")
        observed = await self.observe(request)
        invocation = parse_invocation(observed["request"])
        return await self.executions.continue_invocation(
            invocation, request.fence, request.request_id, request.request_digest
        )

    async def reconcile(self, request: PoolControl) -> dict[str, Any]:
        await self.observe(request)
        return await self.executions.reconcile(request.execution_id, request.fence)

    async def renew_run(self, request: PoolRunRenewal) -> dict[str, Any]:
        if not self.settings.active or not self.ready:
            raise RuntimeError("Administrator runner profile is inactive or runtime unverified")
        observed = await self.observe(request)
        return await self.executions.renew_run(parse_invocation(observed["request"]), request.fence, request.renewal)

    async def cancel(self, request: PoolControl) -> dict[str, Any]:
        await self.observe(request)
        return await self.executions.cancel(request.execution_id, request.fence)


@rest_controller
@request_mapping("/runner/v1")
class PoolController:
    def __init__(self, service: RunnerControllerService) -> None:
        self.service = service

    @get_mapping("/profile")
    def profile(self) -> dict[str, Any]:
        return self.service.profile()

    @post_mapping("/invocations")
    async def submit(self, request: Body[PoolSubmission]) -> dict[str, Any]:
        return await self.service.submit(request)

    @post_mapping("/observe")
    async def observe(self, request: Body[PoolLookup]) -> dict[str, Any]:
        return await self.service.observe(request)

    @post_mapping("/artifact")
    async def artifact(self, request: Body[PoolArtifact]) -> dict[str, Any]:
        return await self.service.artifact(request)

    @post_mapping("/continue")
    async def continue_invocation(self, request: Body[PoolContinuation]) -> dict[str, Any]:
        return await self.service.continue_invocation(request)

    @post_mapping("/reconcile")
    async def reconcile(self, request: Body[PoolControl]) -> dict[str, Any]:
        return await self.service.reconcile(request)

    @post_mapping("/renew-run")
    async def renew_run(self, request: Body[PoolRunRenewal]) -> dict[str, Any]:
        return await self.service.renew_run(request)

    @post_mapping("/cancel")
    async def cancel(self, request: Body[PoolControl]) -> dict[str, Any]:
        return await self.service.cancel(request)


@controller_advice
class RunnerControllerErrors:
    @exception_handler(Exception)
    def handle(self, exc: Exception) -> Response:
        if isinstance(exc, (ValidationError, InvalidRequestException, ValidationException)):
            return JSONResponse({"detail": "Invalid typed runner request"}, status_code=422)
        if isinstance(exc, PermissionError):
            return JSONResponse({"detail": "Runner invocation scope required"}, status_code=403)
        if isinstance(exc, KeyError):
            return JSONResponse({"detail": "Runner invocation not found"}, status_code=404)
        if isinstance(exc, ValueError):
            return JSONResponse({"detail": "Runner request violates its frozen policy"}, status_code=409)
        return JSONResponse({"detail": "Runner unavailable; inspect retained controller evidence"}, status_code=503)


@configuration
class RunnerControllerConfiguration:
    def __init__(self, settings: RunnerControllerSettings) -> None:
        self.settings = settings

    @bean
    def runner_pools(
        self,
        sessions: SessionProvider,
        pools: ForgeRunnerPoolRepository,
        daemons: ForgeRunnerDaemonRepository,
        invocations: ForgeRunnerInvocationRepository,
        events: ForgeRunnerEventRepository,
    ) -> RunnerPools:
        return RunnerPools(
            sessions,
            self.settings.runner_schema,
            factory_schema=self.settings.factory_schema,
            pools=pools,
            daemons=daemons,
            invocations=invocations,
            events=events,
        )

    @bean
    def runner_pool_artifacts(self, async_engine: AsyncEngine) -> RunnerPoolArtifacts:
        artifacts = RunnerPoolArtifacts(self.settings.authority.state_directory)
        artifacts.credential_registry = lambda: {
            "FORGE_RUNNER_CONTROLLER_TOKEN": self.settings.token.get_secret_value(),
            "FORGE_RUNNER_DATABASE_PASSWORD": str(async_engine.url.password or ""),
        }
        return artifacts

    @bean
    def runner_pool_executions(
        self, pools: RunnerPools, artifacts: RunnerPoolArtifacts, dispatcher: DockerPoolDispatcher
    ) -> RunnerPoolExecutions:
        builder = RunnerGitSnapshot(self.settings.git_binary) if self.settings.git_binary else None
        return RunnerPoolExecutions(pools, artifacts, dispatcher, builder)

    @bean
    def runner_pool_dispatcher(self) -> DockerPoolDispatcher:
        return DockerPoolDispatcher(self.settings.authority)


def create_runner_application(settings: RunnerControllerSettings) -> Starlette:
    @pyfly_application(name="firefly-forge-runner", version=__version__)
    class Application:
        pass

    runtime = PyFlyApplication(Application, config_path=Path(__file__).parent / "runner_runtime")
    container = runtime.context.container
    container.register_instance(RunnerControllerSettings, settings)
    container.register_instance(RunnerControllerConfiguration, RunnerControllerConfiguration(settings))
    for component in (
        ForgeRunnerDaemonRepository,
        ForgeRunnerPoolRepository,
        ForgeRunnerInvocationRepository,
        ForgeRunnerEventRepository,
        RunnerControllerAuth,
        RunnerControllerService,
        PoolController,
        RunnerControllerErrors,
    ):
        container.register(component)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            await runtime.startup()
            yield
        finally:
            await runtime.shutdown()

    app = create_app(
        title="Firefly Forge trusted runner",
        version=__version__,
        context=runtime.context,
        docs_enabled=False,
        actuator_enabled=False,
        lifespan=lifespan,
    )
    app.add_middleware(RunnerRequestLimits, settings=settings)
    app.state.runtime = runtime
    return app
