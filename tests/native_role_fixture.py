# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Opt-in real non-superuser connections for the two isolated archive journeys."""

import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from firefly_forge.native_location_files import read_postgres_identity

MODULES = {"test_source_explorer_native", "test_runner_object_artifacts_native"}
IDENTITY = """SELECT current_database() AS database, current_user AS username,
host(inet_server_addr()) AS address, inet_server_port() AS port,
pg_postmaster_start_time() AS started, rolsuper AS superuser,
rolcreatedb AS createdb, rolcreaterole AS createrole, rolreplication AS replication
FROM pg_roles WHERE rolname=current_user"""


async def identity(connection):
    return dict((await connection.execute(text(IDENTITY))).mappings().one())


@asynccontextmanager
async def non_superuser_url(url, data_directory, module):
    url = make_url(url)
    if (
        module not in MODULES
        or url.get_backend_name() != "postgresql"
        or re.fullmatch(r"[a-z][a-z0-9_]*_test", url.database or "") is None
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or not url.username
    ):
        raise ValueError("Only an explicit isolated archive test database is admitted")
    admin = create_async_engine(url, hide_parameters=True)
    role = "forge_archive_test_" + uuid4().hex
    created = False
    try:
        async with admin.begin() as connection:
            before = await identity(connection)
        if (
            before["database"] != url.database
            or before["username"] != url.username
            or before["address"] not in {"127.0.0.1", "::1"}
            or before["port"] != (url.port or 5432)
        ):
            raise ValueError("Archive test database endpoint or current user changed")
        if before["superuser"] is False:
            yield url
            return
        if data_directory is None:
            raise ValueError("Role provisioning requires the exact owned native fixture directory")
        native = read_postgres_identity(Path(data_directory))
        if native.port != before["port"]:
            raise ValueError("Archive test database differs from its owned native fixture")

        async def current(connection):
            if await identity(connection) != before or read_postgres_identity(Path(data_directory)) != native:
                raise ValueError("The owned archive test database identity changed")
            proof = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT current_setting('data_directory') AS directory, "
                            "(pg_control_system()).system_identifier::text AS system_identifier"
                        )
                    )
                )
                .mappings()
                .one()
            )
            if proof != {"directory": str(native.directory), "system_identifier": native.system_identifier}:
                raise ValueError("Archive test PostgreSQL does not match the owned control files")

        password = secrets.token_urlsafe(32)
        async with admin.begin() as connection:
            await current(connection)
            await connection.execute(
                text(
                    "SELECT set_config('forge.fixture.role', :role, true), "
                    "set_config('forge.fixture.password', :password, true)"
                ),
                {"role": role, "password": password},
            )
            # Passwords are bound parameters; no generated credential appears in DDL or diagnostics.
            await current(connection)
            await connection.execute(
                text("""DO $fixture$ BEGIN
EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
current_setting('forge.fixture.role'), current_setting('forge.fixture.password'));
END $fixture$""")
            )
            await current(connection)
            await connection.execute(text(f'GRANT CONNECT, CREATE, TEMPORARY ON DATABASE "{url.database}" TO "{role}"'))
        created = True
        selected = url.set(username=role, password=password)
        worker = create_async_engine(selected, hide_parameters=True)
        try:
            async with worker.begin() as connection:
                observed = await identity(connection)
            expected = {
                **before,
                "username": role,
                "superuser": False,
                "createdb": False,
                "createrole": False,
                "replication": False,
            }
            if observed != expected:
                raise ValueError("The actual native test role or database identity differs")
            yield selected
        finally:
            await worker.dispose()
    finally:
        try:
            if created:
                async with admin.begin() as connection:
                    await current(connection)
                    schemas = (
                        (
                            await connection.execute(
                                text(
                                    "SELECT nspname FROM pg_namespace WHERE nspowner="
                                    "(SELECT oid FROM pg_roles WHERE rolname=:role) ORDER BY nspname"
                                ),
                                {"role": role},
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if any(
                        re.fullmatch(r"forge_(?:test|source)_[a-f0-9]{32}(?:_runner)?", schema) is None
                        for schema in schemas
                    ):
                        raise ValueError("An unexpected role-owned schema requires retained fixture review")
                    for schema in schemas:
                        await current(connection)
                        await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                    await current(connection)
                    await connection.execute(
                        text(f'REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE "{url.database}" FROM "{role}"')
                    )
                    await current(connection)
                    await connection.execute(text(f'DROP ROLE "{role}"'))
        finally:
            await admin.dispose()


@pytest_asyncio.fixture
async def postgres_schema(postgres_schema, request, monkeypatch):
    url = os.environ.get("FORGE_TEST_DATABASE_URL", "")
    data = os.environ.get("FORGE_TEST_POSTGRES_DATA")
    async with non_superuser_url(url, Path(data) if data else None, request.module.__name__) as selected:
        monkeypatch.setenv("FORGE_DATABASE_URL", selected.render_as_string(hide_password=False))
        yield postgres_schema
