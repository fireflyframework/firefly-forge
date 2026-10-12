<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Install and start your factory

Use this guide to run the API and Studio from source. You need Python 3.13 or
newer, [uv](https://docs.astral.sh/uv/), Git and Node.js 22.12 or newer. The
current process lifecycle uses POSIX process groups; use macOS or Linux for the
initial operator environment. Install
the runtime and build tools for the products you intend to generate: Python for
PyFly, PHP and Composer for LaraFly, and Node.js for browser frontends.

## 1. Get the source and build Studio

```sh
git clone https://github.com/fireflyframework/firefly-forge.git
cd firefly-forge
uv sync --locked --group dev --group docs
cd studio
npm ci
npm run build
cd ..
```

Expected: Studio writes its production build to `studio/dist/`. The API serves
this build; a source checkout without it has no built browser application.

The supplied Docker image also includes Python, Node.js, PHP and Composer for
primary product verification. Choose a configured API provider or an authorized
bridge endpoint inside a container; host CLI subscription logins are not copied
into images. Java, Rust and .NET require their matching runtime/toolchain.

## 2. Configure access

Choose a strong, unique token and set it in your shell before starting Forge.

```sh
export FORGE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export FORGE_DATA_DIR="$PWD/.forge"
```

Keep that terminal open. Copy the token from your own environment into Studio's
**Connections** form when you connect. Avoid placing it in a command committed to
Git, a repository URL or a product brief. [Security](security.md) explains who can
use this token and why it is operator access.

## 3. Configure a provider

Install an official coding CLI and sign in using its own login flow. Forge uses
the existing login on the server. It does not implement an alternative login.

```sh
codex login
codex login status
```

Or, for Claude Code:

```sh
claude auth login
claude auth status
```

See [agents](agents.md) for native Firefly Agentic configuration and provider
readiness boundaries. Provider subscriptions and model access belong to the
account used by the server.

## 4. Start Forge

```sh
uv run forge serve --host 127.0.0.1 --port 8787 --data-dir .forge
```

Open [Studio](http://127.0.0.1:8787/). Select **Connections**, keep the API address
as `/api/v1`, enter the token and save. Expected: **API connected** appears, and
the execution provider panel reports the server's readiness checks.

If the token is rejected, confirm that Studio and the server use the same
`FORGE_API_TOKEN`. If Studio is missing, rebuild `studio/dist/` and restart the
server. If an agent is unavailable, fix its installation or login on the server
and refresh the connection.

## Develop Studio separately

Keep the API running on port 8787. In another terminal:

```sh
cd studio
npm run dev
```

The development server uses port 5173 and proxies `/api` to port 8787. Enter
`/api/v1` in Connections. Tokens remain in page memory and are cleared when you
reload or close the page.

For a remote installation, serve Studio and the API through the same HTTPS
origin. This initial API does not configure cross-origin browser access. An
arbitrary remote base address needs its own appropriate origin/proxy setup.

Continue with [your first product](quickstart.md).

## Prepare developer verification

`make check` includes Studio's browser journeys. Install Chromium once before
running that gate:

```sh
npx --prefix studio playwright install chromium
make check
```

On Linux, use Playwright's `--with-deps` option when its system browser
dependencies are not already installed. The gate covers factory code and
Studio behavior; it does not replace a live coding-provider or generated-product
acceptance run.
