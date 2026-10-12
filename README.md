# firefly forge

![firefly forge: from brief to verified software](assets/banner.svg)

The Firefly software factory. Describe a product, connect a repository, choose your Firefly stack and coding worker, and inspect a verified implementation before publishing a pull request.

forge combines native PyFly services and controllers, PostgreSQL persistence, Firefly Agentic, a web Studio and official local coding agents. Build, maintain or test projects with retained plans, verification and recovery records. Connect GitHub, GitLab, Bitbucket Cloud, Azure DevOps, Gitea or a generic Git remote. Internal teams can also connect the private Firefly Agent Bridge.

## Start locally

Requirements: a dedicated PostgreSQL database, Python 3.13+, Git, Node.js 22.12+ for Studio development, and a signed-in coding CLI or an API model. Use a dedicated macOS or Linux development environment.

```sh
uv sync --group dev --group docs
cd studio && npm ci && npm run build && cd ..
export FORGE_DATABASE_URL="postgresql+asyncpg://forge:YOUR_DATABASE_PASSWORD@127.0.0.1:5432/forge"
export FORGE_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run forge serve
```

Open http://127.0.0.1:8787, connect using your token, add a repository and create a run. Workers execute code on the trusted machine running forge. Use an isolated development host for repositories you do not trust.

[Documentation](https://fireflyframework.github.io/firefly-forge/) · [Installation](docs/installation.md) · [Architecture](docs/architecture.md) · [Capabilities](docs/capabilities.md)

Configure provider access and native SCM application grants in Studio. Project policies can use the public [central policy catalog](https://github.com/fireflyframework/firefly-forge-policies); optional Flycanon retrieval supplies cited context and verified project memory. Deployment requires an explicit configured target and passing source evidence. See [recovery](docs/recovery.md), [knowledge](docs/knowledge.md) and [deployment](docs/deployment.md).

## Development

Install Chromium with `npx --prefix studio playwright install chromium` before the first browser gate. Set `FORGE_TEST_DATABASE_URL` to an isolated PostgreSQL test database. `make check` runs Python tests, lint and types, Studio checks/tests/browser journeys/build, and the strict documentation build. `make package` creates installable distributions with Studio included. Private bridge source is maintained separately and is never included in forge distributions. See the [release notes](docs/release-notes.md) for recorded validation and remaining boundaries.

Apache 2.0 for code. Firefly names and generated brand assets retain their separate trademark and asset rights notices.
