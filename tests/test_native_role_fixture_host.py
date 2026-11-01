# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The opt-in native fixture changes only its owned isolated test database."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import native_role_fixture as subject
import pytest
from sqlalchemy.engine import make_url


class Result:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


@pytest.fixture
def database(monkeypatch, tmp_path):
    state = SimpleNamespace(superuser=True, role=None, events=[], drift=False, bad_user=False, schemas=[])
    data = tmp_path / "owned-data"
    identity = SimpleNamespace(
        directory=data, system_identifier="123", postmaster_pid=1234, started_at=1000, port=19044
    )
    monkeypatch.setattr(subject, "read_postgres_identity", lambda path: identity)

    class Connection:
        def __init__(self, url):
            self.url = url

        async def execute(self, statement, parameters=None):
            sql = str(statement)
            if "current_database()" in sql:
                child = self.url.username != "fixture_bootstrap"
                return Result(
                    dict(
                        database="archive_test",
                        username="wrong" if child and state.bad_user else self.url.username,
                        address="127.0.0.1",
                        port=19045 if state.drift else 19044,
                        started="unchanged",
                        superuser=False if child else state.superuser,
                        createdb=False,
                        createrole=False,
                        replication=False,
                    )
                )
            if "current_setting('data_directory')" in sql:
                return Result(dict(directory=str(data), system_identifier="123"))
            if "set_config" in sql:
                state.role = parameters["role"]
                state.events.append("password-bound")
            elif "CREATE ROLE" in sql:
                assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION" in sql
                assert state.events == ["password-bound"]
                state.events.append("create-role")
            elif sql.startswith("GRANT"):
                assert 'CONNECT, CREATE, TEMPORARY ON DATABASE "archive_test"' in sql
                state.events.append("grant-isolated-db")
            elif "SELECT nspname" in sql:
                return Result(state.schemas)
            elif sql.startswith("REVOKE"):
                state.events.append("revoke-isolated-db")
            elif sql.startswith("DROP SCHEMA"):
                state.events.append("drop-owned-schema")
            elif sql.startswith("DROP ROLE"):
                state.events.append("drop-role")
            else:
                raise AssertionError("Unexpected fixture database operation")
            return Result(None)

    class Engine:
        def __init__(self, url):
            self.url = make_url(url)

        @asynccontextmanager
        async def begin(self):
            yield Connection(self.url)

        async def dispose(self):
            pass

    monkeypatch.setattr(subject, "create_async_engine", lambda url, **kwargs: Engine(url))
    state.url = make_url("postgresql+asyncpg://fixture_bootstrap:ephemeral@127.0.0.1:19044/archive_test")
    state.data = data
    return state


async def test_existing_non_superuser_connection_is_unchanged_without_role_ddl(database):
    database.superuser = False
    database.url = database.url.update_query_dict({"ssl": "disable"})
    async with subject.non_superuser_url(database.url, None, "test_source_explorer_native") as url:
        assert url == database.url
    assert database.events == []


async def test_new_role_uses_real_dsn_and_cleans_owned_schemas_before_role(database):
    async with subject.non_superuser_url(database.url, database.data, "test_source_explorer_native") as url:
        assert url.username != database.url.username
        assert url.password and url.password != database.url.password
        assert url.database == "archive_test" and url.port == 19044
        database.schemas = ["forge_test_" + "a" * 32, "forge_source_" + "b" * 32]
    assert database.events == [
        "password-bound",
        "create-role",
        "grant-isolated-db",
        "drop-owned-schema",
        "drop-owned-schema",
        "revoke-isolated-db",
        "drop-role",
    ]


@pytest.mark.parametrize("change", ["module", "database", "host", "port", "missing-proof"])
async def test_unowned_or_unknown_database_never_creates_a_role(database, change):
    url, module, data = database.url, "test_source_explorer_native", database.data
    if change == "module":
        module = "test_unrelated"
    elif change == "database":
        url = url.set(database="production")
    elif change == "host":
        url = url.set(host="external.example")
    elif change == "port":
        database.drift = True
    else:
        data = None
    with pytest.raises(ValueError):
        async with subject.non_superuser_url(url, data, module):
            pytest.fail("An unowned database was admitted")
    assert database.events == []


async def test_incorrect_current_user_cannot_supply_native_non_superuser_proof(database):
    database.bad_user = True
    with pytest.raises(ValueError, match="role"):
        async with subject.non_superuser_url(database.url, database.data, "test_runner_object_artifacts_native"):
            pytest.fail("Wrong actual database user admitted")
    assert database.events[-1] == "drop-role"


async def test_unknown_schema_is_retained_and_cleanup_failure_is_not_hidden(database):
    with pytest.raises(ValueError, match="schema"):
        async with subject.non_superuser_url(database.url, database.data, "test_source_explorer_native"):
            database.schemas = ["unrelated_retained_data"]
    assert "drop-owned-schema" not in database.events and "drop-role" not in database.events
