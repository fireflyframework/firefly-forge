# firefly forge

![firefly forge: from brief to verified software](assets/banner.svg)

firefly forge is the Firefly software factory. Describe a product, connect a repository and choose your Firefly stack and coding worker. Inspect the verified implementation and its evidence before publishing a pull request.

forge combines native [pyfly](https://fireflyframework.org/frameworks/python/) services and controllers, PostgreSQL persistence, Firefly Agentic, a web Studio and official local coding agents. Build, maintain or test projects with retained plans, verification and recovery records. Connect GitHub, GitLab, Bitbucket Cloud, Azure DevOps, Gitea or a generic Git remote. Development teams can also connect Firefly Agent Bridge.

## Start locally

The managed local stack includes forge, its PostgreSQL ledger, Flycanon, a separate pgvector database, durable knowledge objects and local embeddings. Fresh setup requires knowledge retrieval and enables verified project memory. Use Docker Compose and a dedicated macOS or Linux development environment. Source development also needs Python 3.13+, Git and Node.js 22.12+.

Complete the [installation prerequisites](docs/installation.md#run-the-complete-local-stack), including the separately configured Keycloak client and private environment settings, before starting:

```sh
git clone https://github.com/fireflyframework/firefly-forge.git
cd firefly-forge
docker compose up --build -d
docker compose ps --all
```

Open http://127.0.0.1:8787, sign in and verify the included [knowledge service](docs/knowledge.md). Connect a repository and an authorized coding provider before creating a run. Project users require isolated execution; direct host coding CLIs remain an administrator capability. Existing knowledge connections and queued configuration snapshots are preserved. [Factory infrastructure](docs/factory-infrastructure.md) covers server and cluster preparation, storage, backup and recovery.

[Documentation](https://fireflyframework.github.io/firefly-forge/) · [Installation](docs/installation.md) · [Architecture](docs/architecture.md) · [Capabilities](docs/capabilities.md)

Configure provider access and native SCM application grants in Studio. Project policies can use the public [central policy catalog](https://github.com/fireflyframework/firefly-forge-policies); Flycanon supplies required cited context and verified project memory in the managed installation. Deployment requires an explicit configured target and passing source evidence. See [recovery](docs/recovery.md), [knowledge](docs/knowledge.md) and [deployment](docs/deployment.md).

## Development

Install Chromium with `npx --prefix studio playwright install chromium` before the first browser gate. Set `FORGE_TEST_DATABASE_URL` to an isolated PostgreSQL test database. `make check` runs Python tests, lint and types, Studio checks/tests/browser journeys/build, and the strict documentation build. `make package` creates installable distributions with Studio included. Agent bridge source is maintained separately and is never included in forge distributions. See the [release notes](docs/release-notes.md) for recorded validation and remaining boundaries.

Apache 2.0 for code. Firefly names and generated brand assets retain their separate trademark and asset rights notices.
