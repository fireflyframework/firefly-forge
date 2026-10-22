---
description: Install firefly forge and initialize its managed local knowledge stack, identity prerequisites and development execution configuration.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Install and start firefly forge

For a fresh managed installation, use Docker Compose, Git and a configured Keycloak identity provider. The stack includes the factory database and Flycanon dependencies. Source development on the host also needs Python 3.13+, [uv](https://docs.astral.sh/uv/), a dedicated PostgreSQL database and Node.js 22.12+ for Studio. The full development test gate also requires an existing PHP 8.3+ CLI for its explicit recipe fixtures; it does not require Xdebug. Use macOS or Linux; worker cancellation uses POSIX process groups.

Choose the managed stack below or the [source-development path](#source-development). Install the product toolchains in the selected execution environment: Python for [pyfly](https://fireflyframework.org/frameworks/python/), PHP 8.3+ and Composer for [larafly](https://fireflyframework.org/frameworks/php/), and Node.js for browser products.

## Run the complete local stack

```sh
git clone https://github.com/fireflyframework/firefly-forge.git
cd firefly-forge
```

<picture>
  <source media="(max-width: 640px)" srcset="../diagrams/local-stack-narrow.svg">
  <img src="../diagrams/local-stack.svg" alt="Compose creates private credentials, prepares pgvector and the local embedding model, starts authenticated knowledge, requires cited framework search, then starts forge.">
</picture>

[Open the local startup diagram](diagrams/local-stack.svg) · [Portrait layout](diagrams/local-stack-narrow.svg)

The supplied Compose file includes forge PostgreSQL, private factory state, Flycanon with its own pgvector database and original-object storage, and a local embedding service. Fresh managed setup enables required retrieval and automatic verified memory, with generated private credentials and authenticated internal TLS.

Configure a Keycloak OIDC issuer and client before starting; Compose does not install Keycloak. Complete the [identity setup](identity.md#configure-a-keycloak-realm). The issuer must be reachable by both the browser and forge, with exact callback, audience and role settings.

Set `FORGE_POSTGRES_PASSWORD` and the OIDC settings from the [identity guide](identity.md) through your private deployment environment. Set `FORGE_AUTH_MODE=oidc` for this stack. Its network boundary does not turn a container gateway into a trusted loopback bootstrap caller. Set `FORGE_DATABASE_URL` to `postgresql+asyncpg://forge:YOUR_URL_ENCODED_PASSWORD@postgres:5432/forge`; the host is the Compose service name `postgres`. The URL password must match the configured database password and reserved URI characters must be percent-encoded.

Start the complete stack:

```sh
docker compose up --build -d
docker compose ps --all
docker compose logs knowledge-initialize embeddings-initialize
```

The forge image runs as the `forge` user (UID 10001), with Tini as PID1. Tini reaps adopted child processes and forwards termination signals to the application; the default command remains `forge serve --host 0.0.0.0`. Compose command overrides inherit this entrypoint. An `--entrypoint` override bypasses it, so a replacement entrypoint must supply its own process supervision. Project sandbox and runner images have separate execution policies; this image-level change does not replace their controls. For shutdown diagnostics, inspect the application's completion messages as well as the exit code. Uvicorn can finish its shutdown and then re-raise SIGTERM, producing exit 143; that code alone does not establish a clean shutdown. A forced kill or OOM is a failed acceptance check.

Only the forge API publishes a host port, bound to `127.0.0.1:8787`. Flycanon uses `https://knowledge:8500` on the private service network. Bootstrap initializes its owner-private credentials once; service initialization admits the catalog and 14 pinned core pyfly/larafly manuals, then requires actual embedding-backed source citations before new work. Local embedding uses `nomic-embed-text`, without a paid provider API key. Model download and first initialization can take time; a running container is distinct from completed knowledge retrieval.

Open Studio, sign in through the configured identity provider and inspect **Knowledge**. Test the selected tenant's authentication, then perform a scoped search and inspect the source citations. [Knowledge initialization](knowledge.md#initialize-the-managed-service) explains bootstrap, external-service configuration and failed dependencies. Keep required knowledge enabled for the factory.

Back up all durable factory and knowledge stores before upgrading. Existing knowledge settings and queued snapshots remain intact; an older disabled or external connection needs an explicit administrator review. Local CLI subscription logins are host credentials; the container needs its own selected provider setup.

For API and headless-worker processes sharing one execution leader, choose the [split topology](runtime-roles.md) on a fresh installation. For a shared server or Kubernetes cluster, use the [factory infrastructure guide](factory-infrastructure.md).

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

## Source development

Use the following host setup for developing forge itself. Its explicit local bootstrap settings apply to the loopback host process; use a separate private environment for the OIDC Compose installation above.

### Build the application

```sh
git clone https://github.com/fireflyframework/firefly-forge.git
cd firefly-forge
uv sync --locked --group dev --group docs
npm --prefix studio ci
npm --prefix studio run build
```

Studio's production files are written to `studio/dist/`. The API serves that build. The installed distribution also includes Studio; a source checkout needs the build above. Run [full development dependency audits](security.md#maintain-studio-development-tools) when updating the source toolchain.

### Prepare PostgreSQL

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

### Start a source-development process

A host API still needs the required Flycanon connection. The managed service's internal `knowledge` hostname is available to containers on its network. Configure a separately reachable authenticated HTTPS service and its CA when running forge directly on the host; do not disable knowledge or publish an unprotected service to work around that boundary.

For a new managed host connection, [bootstrap with an explicit HTTPS loopback endpoint](knowledge.md#initialize-a-new-managed-connection-on-the-host). It creates the matching certificate and preserves required retrieval. You must supply the running local flycanon, PostgreSQL/pgvector and embedding backend; the command starts or downloads none of them.

```sh
uv run forge serve --host 127.0.0.1 --port 8787 --data-dir .forge
```

`forge serve` selects the stdlib asyncio event loop explicitly. If you host the application through Uvicorn directly, configure `--loop asyncio`. With a finite POSIX file-size limit, forge refuses trusted CLI dispatch under a known uvloop host because its subprocess path does not preserve the required ignored file-size signal. Use the supported loop and keep the existing kernel limit; see [file-size failure recovery](recovery.md#recover-a-known-failed-official-cli-attempt).

Open [Studio](http://127.0.0.1:8787/). For this explicit local setup, enter the operator token once; the server exchanges it for a durable HttpOnly session. Production installations use [Keycloak sign-in](identity.md). Configure a provider and test its access. Connect an authorized repository, choose pyfly or larafly, and create a small run with observable acceptance checks.

A database connection or migration failure prevents startup. A rejected operator token requires matching Studio and server credentials. Missing Studio files require rebuilding its production bundle. Provider authentication and actual model execution have separate checks.

### Develop Studio

Keep the API running on port 8787 and run:

```sh
npm --prefix studio run dev
```

The development server on port 5173 proxies `/api` to the API. Use `/api/v1` as the browser API address. The Studio restores its server session after reload. For this separate development origin, set `FORGE_BROWSER_ORIGIN=http://127.0.0.1:5173` and `FORGE_OIDC_ALLOW_LOOPBACK_HTTP=true` before starting the API. The browser never stores the operator token.

For remote operation, configure Keycloak OIDC, serve Studio and the API through one HTTPS origin, restrict allowed hosts, and register exact identity and source-control callback URLs. Separate API replicas from execution workers before treating an installation as cluster-ready. [SCM connections](scm.md) describes provider-owned setup and grants.

## Check local readiness

Use the installed CLI to inspect coding-provider availability and stack profiles:

```sh
uv run forge doctor
```

This command checks the environment where it runs. A host CLI check does not establish that the container has the same tools or subscription login. It does not probe Flycanon. Use the managed knowledge initialization result, the Knowledge authentication test and a scoped search with real citations to check that dependency. A provider login, an available embedding model and an actual coding response have separate readiness evidence.

## Run the development gate

Run `make php-fixture-check` to check the existing PHP interpreter before the full gate. Set `FORGE_TEST_PHP_FIXTURE` to an executable path if it is not named `php` on your `PATH`. The check never installs a runtime or extension; fixture subprocesses are not a product host fallback. Missing-Xdebug refusal is tested separately against an explicitly prepared isolated image.

Use an explicitly isolated PostgreSQL test database. Set `FORGE_TEST_DATABASE_URL` to its async connection URL. Tests create uniquely named schemas and remove those test schemas during cleanup; they fail when the test database is not configured.

```sh
export FORGE_TEST_DATABASE_URL="postgresql+asyncpg://forge:YOUR_TEST_PASSWORD@127.0.0.1:5432/forge_test"
npx --prefix studio playwright install chromium
make check
```

Linux hosts may need Playwright's `--with-deps` browser installation option. The gate covers native PostgreSQL persistence, factory code, Studio behavior and the strict documentation build. Live coding-provider, SCM and generated-product acceptance are separate evidence.

## Upgrade an initial SQLite installation

Stop the old service and retain a private backup of its SQLite ledger and data directory. Configure a dedicated PostgreSQL database for the current service. The current server does not automatically read or migrate the old SQLite file. Preserve the old history for inspection; do not rename a SQLite file into a PostgreSQL connection or overwrite it during setup.
