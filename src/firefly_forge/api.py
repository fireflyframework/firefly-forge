# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated PyFly API and Studio host for a single trusted factory operator."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from pyfly.core import PyFlyApplication, pyfly_application
from pyfly.web.adapters.starlette import create_app
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from firefly_forge import __version__
from firefly_forge.engine import Engine, redact
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.store import Store


class OperatorAuth(BaseHTTPMiddleware):
    def __init__(self, app: Any, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {self.token}"):
                return JSONResponse({"detail": "Connect using the factory operator token"}, status_code=401)
            if int(request.headers.get("Content-Length", "0")) > 100_000:
                return JSONResponse({"detail": "Request exceeds 100 KB"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def openapi_document() -> dict[str, Any]:
    paths: dict[str, Any] = {}
    specifications = [
        ("/repositories", "get", None),
        ("/repositories", "post", "RepositoryInput"),
        ("/runs", "get", None),
        ("/runs", "post", "RunInput"),
        ("/runs/{id}", "get", None),
        ("/runs/{id}/cancel", "post", None),
        ("/runs/{id}/retry", "post", None),
        ("/runs/{id}/publish", "post", None),
        ("/runs/{id}/artifacts/changes.zip", "get", None),
        ("/profiles", "get", None),
        ("/providers", "get", None),
        ("/health", "get", None),
    ]
    schemas: dict[str, Any] = {}
    models: list[type[BaseModel]] = [RepositoryInput, RunInput]
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema
    for path, method, model_name in specifications:
        operation: dict[str, Any] = {
            "summary": f"{method.upper()} {path}",
            "responses": {
                "200": {"description": "Successful operation"},
                "401": {"description": "Operator token required"},
                "422": {"description": "Invalid request"},
            },
        }
        if "{id}" in path:
            operation["parameters"] = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]
        if model_name:
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model_name}"}}},
            }
        paths.setdefault("/api/v1" + path, {})[method] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "Firefly Forge", "version": __version__},
        "paths": paths,
        "components": {"schemas": schemas, "securitySchemes": {"operator": {"type": "http", "scheme": "bearer"}}},
        "security": [{"operator": []}],
    }


def create_application(data_dir: Path | None = None, token: str | None = None) -> Starlette:
    data_dir = data_dir or Path(os.environ.get("FORGE_DATA_DIR", ".forge")).resolve()
    token = token if token is not None else os.environ.get("FORGE_API_TOKEN", "")
    if len(token) < 24:
        raise ValueError("Set a factory operator token of at least 24 characters in FORGE_API_TOKEN")
    store = Store(data_dir / "factory.db")
    engine = Engine(store, data_dir / "workspaces", concurrency=int(os.environ.get("FORGE_CONCURRENCY", "2")))

    @pyfly_application(name="firefly-forge", version=__version__)
    class Application:
        pass

    runtime = PyFlyApplication(Application, config_path=Path(__file__).parent / "pyfly.yaml")
    runtime.context.container.register_instance(Store, store)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await runtime.startup()
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()
            await runtime.shutdown()

    async def endpoint(request: Request) -> Response:
        path = request.url.path.removeprefix("/api/v1/")
        try:
            if path == "health":
                return JSONResponse({"status": "ok", "version": __version__, "runtime": "PyFly", "storage": "SQLite"})
            if path == "openapi.json":
                return JSONResponse(openapi_document())
            if path == "profiles":
                from firefly_forge.profiles import profile_catalog

                return JSONResponse(profile_catalog())
            if path == "providers":
                from firefly_forge.providers import provider_catalog

                return JSONResponse(await asyncio.to_thread(provider_catalog))
            if path == "repositories":
                if request.method == "GET":
                    return JSONResponse(store.repositories())
                body = await request.json()
                return JSONResponse(store.add_repository(RepositoryInput.model_validate(body)), status_code=201)
            if path == "runs":
                if request.method == "GET":
                    return JSONResponse(store.runs())
                body = await request.json()
                return JSONResponse(store.create_run(RunInput.model_validate(body)), status_code=201)
            identifier = request.path_params["id"]
            run = store.get_run(identifier)
            if path.endswith("/cancel"):
                await engine.cancel(identifier)
                return JSONResponse(store.get_run(identifier))
            if path.endswith("/retry"):
                if run["status"] not in {"failed", "cancelled", "interrupted"}:
                    raise ValueError("Only failed, cancelled or interrupted runs can be retried")
                retry = RunInput.model_validate({key: run[key] for key in RunInput.model_fields})
                return JSONResponse(store.create_run(retry), status_code=201)
            if path.endswith("/publish"):
                await engine.publish(identifier)
                return JSONResponse(store.get_run(identifier))
            if path.endswith("/artifacts/changes.zip"):
                if not run["artifacts"]:
                    raise KeyError("Artifact not found")
                artifact = engine.workspaces / f"{identifier}.zip"
                return FileResponse(artifact, filename="changes.zip", media_type="application/zip")
            return JSONResponse(run)
        except (ValidationError, json.JSONDecodeError) as exc:
            return JSONResponse({"detail": str(exc)[:4000]}, status_code=422)
        except KeyError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except Exception as exc:
            return JSONResponse({"detail": redact(str(exc))}, status_code=500)

    routes: list[Route] = []
    for path, methods in [
        ("health", ["GET"]),
        ("openapi.json", ["GET"]),
        ("profiles", ["GET"]),
        ("providers", ["GET"]),
        ("repositories", ["GET", "POST"]),
        ("runs", ["GET", "POST"]),
        ("runs/{id}", ["GET"]),
        ("runs/{id}/cancel", ["POST"]),
        ("runs/{id}/retry", ["POST"]),
        ("runs/{id}/publish", ["POST"]),
        ("runs/{id}/artifacts/changes.zip", ["GET"]),
    ]:
        routes.append(Route("/api/v1/" + path, endpoint, methods=methods))
    app = create_app(
        title="Firefly Forge",
        version=__version__,
        context=runtime.context,
        extra_routes=routes,
        docs_enabled=False,
        actuator_enabled=False,
        lifespan=lifespan,
    )
    app.add_middleware(OperatorAuth, token=token)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=os.environ.get("FORGE_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(","),
    )
    source_static = Path(__file__).resolve().parents[2] / "studio" / "dist"
    packaged_static = Path(__file__).parent / "static"
    static = packaged_static if packaged_static.is_dir() else source_static
    if static.is_dir():
        app.routes.append(Mount("/", app=StaticFiles(directory=static, html=True), name="studio"))
    app.state.store, app.state.engine, app.state.runtime = store, engine, runtime
    return app
