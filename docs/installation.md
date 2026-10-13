<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Install and start firefly forge

You need Python 3.13+, [uv](https://docs.astral.sh/uv/), Git, a dedicated PostgreSQL database, and Node.js 22.12+ to build Studio. Use macOS or Linux; worker cancellation uses POSIX process groups. Install the product toolchains you intend to use: Python for [pyfly](https://fireflyframework.org/frameworks/python/), PHP 8.3+ and Composer for [larafly](https://fireflyframework.org/frameworks/php/), and Node.js for browser products.

## Build the application

```sh
git clone https://github.com/fireflyframework/firefly-forge.git
cd firefly-forge
uv sync --locked --group dev --group docs
npm --prefix studio ci
npm --prefix studio run build
```

Studio's production files are written to `studio/dist/`. The API serves that build. The installed distribution also includes Studio; a source checkout needs the build above.

## Prepare PostgreSQL

Use a database created for this factory. Give its development identity permission to create and manage the selected ledger schema. The application applies its versioned migrations to that schema; it does not require an application SQLite file or initialize unrelated databases.

Set an async PostgreSQL connection URL through your local secret manager or a private environment file:

```sh
export FORGE_DATABASE_URL="postgresql+asyncpg://forge:YOUR_DATABASE_PASSWORD@127.0.0.1:5432/forge"
export FORGE_DATABASE_SCHEMA="forge"
export FORGE_DATA_DIR="$PWD/.forge"
export FORGE_AUTH_MODE="local_bootstrap"
export FORGE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

The values above are examples. Use the actual dedicated database and a strong password. Keep the connection URL and operator token out of Git, briefs, screenshots and shell output shared with others. A container must receive its own database configuration; `127.0.0.1` inside it identifies the container itself.

PostgreSQL stores repositories, runs, events, plans, task states and the write-ahead journal. The private data directory stores connection settings, immutable credential revisions, workspaces and delivery artifacts. Back up both parts together. See [operations](operations.md).

## Run with Compose

The supplied Compose file includes PostgreSQL 17 and separate database and factory-data volumes. Set `FORGE_POSTGRES_PASSWORD` and the OIDC settings from the [identity guide](identity.md) through your private deployment environment. Compose uses OIDC by default; its network boundary does not turn a container gateway into a trusted loopback bootstrap caller. Set `FORGE_DATABASE_URL` to `postgresql+asyncpg://forge:YOUR_URL_ENCODED_PASSWORD@postgres:5432/forge`; the host is the Compose service name `postgres`. The URL password must match the configured database password and reserved URI characters must be percent-encoded.

Start with `docker compose up --build`. The API binds to the host loopback address on port 8787. Back up both volumes and the database before upgrading. Local CLI subscription logins are host credentials; the container needs its own selected provider setup.

## Connect a coding provider

An official CLI login is a useful first setup on a development host:

```sh
codex login
codex login status
# Or:
claude auth login
claude auth status
```

Native Firefly Agentic uses a provider API key and an available provider-qualified model. The internal bridge requires its authorized endpoint and token. [Agents](agents.md) explains their tools and readiness evidence; [configuration](configuration.md) covers saved settings.

## Start the service

```sh
uv run forge serve --host 127.0.0.1 --port 8787 --data-dir .forge
```

Open [Studio](http://127.0.0.1:8787/). For this explicit local setup, enter the operator token once; the server exchanges it for a durable HttpOnly session. Production installations use [Keycloak sign-in](identity.md). Configure a provider and test its access. Connect an authorized repository, choose pyfly or larafly, and create a small run with observable acceptance checks.

A database connection or migration failure prevents startup. A rejected operator token requires matching Studio and server credentials. Missing Studio files require rebuilding its production bundle. Provider authentication and actual model execution have separate checks.

## Develop Studio

Keep the API running on port 8787 and run:

```sh
npm --prefix studio run dev
```

The development server on port 5173 proxies `/api` to the API. Use `/api/v1` as the browser API address. The Studio restores its server session after reload. For this separate development origin, set `FORGE_BROWSER_ORIGIN=http://127.0.0.1:5173` and `FORGE_OIDC_ALLOW_LOOPBACK_HTTP=true` before starting the API. The browser never stores the operator token.

For remote operation, configure Keycloak OIDC, serve Studio and the API through one HTTPS origin, restrict allowed hosts, and register exact identity and source-control callback URLs. Separate API replicas from execution workers before treating an installation as cluster-ready. [SCM connections](scm.md) describes provider-owned setup and grants.

## Run the development gate

Use an explicitly isolated PostgreSQL test database. Set `FORGE_TEST_DATABASE_URL` to its async connection URL. Tests create uniquely named schemas and remove those test schemas during cleanup; they fail when the test database is not configured.

```sh
export FORGE_TEST_DATABASE_URL="postgresql+asyncpg://forge:YOUR_TEST_PASSWORD@127.0.0.1:5432/forge_test"
npx --prefix studio playwright install chromium
make check
```

Linux hosts may need Playwright's `--with-deps` browser installation option. The gate covers native PostgreSQL persistence, factory code, Studio behavior and the strict documentation build. Live coding-provider, SCM and generated-product acceptance are separate evidence.

## Upgrade an initial SQLite installation

Stop the old service and retain a private backup of its SQLite ledger and data directory. Configure a dedicated PostgreSQL database for the current service. The current server does not automatically read or migrate the old SQLite file. Preserve the old history for inspection; do not rename a SQLite file into a PostgreSQL connection or overwrite it during setup.
