<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Use the HTTP API

The API prefix is `/api/v1`. Send `Authorization: Bearer <FORGE_API_TOKEN>` for
operator requests. Request and response bodies use JSON. The actual running
application's OpenAPI document is the authoritative schema where available.

| Method | Path | Operation |
| --- | --- | --- |
| GET | `/health` | Inspect server health |
| GET | `/providers` | Read configured coding-provider readiness |
| GET | `/openapi.json` | Read the authenticated OpenAPI contract |
| GET | `/profiles` | Read backend and frontend contracts |
| GET | `/repositories` | List registered repositories |
| POST | `/repositories` | Register a Git source |
| GET | `/runs` | List run history |
| POST | `/runs` | Queue generation and verification |
| GET | `/runs/{id}` | Read a run's events and evidence |
| POST | `/runs/{id}/cancel` | Request cancellation |
| POST | `/runs/{id}/retry` | Create another attempt after failure, cancellation or interruption |
| POST | `/runs/{id}/publish` | Publish an already verified local change |
| GET | `/runs/{id}/artifacts/changes.zip` | Download retained changed files |

## Register a repository

```json
{
  "name": "Meeting rooms",
  "url": "https://github.com/your-org/meeting-rooms.git",
  "provider": "github",
  "default_branch": "main"
}
```

Provider values are `git`, `github`, `gitlab`, `bitbucket`, `azure` and `gitea`.
Repository registration does not establish clone or publication permissions.

## Start a local run

```json
{
  "repository_id": "the-id-returned-by-registration",
  "brief": "Build the meeting-room reservation service described in our brief.",
  "backend": "pyfly",
  "frontend": "none",
  "agent": "codex",
  "commands": [{"argv": ["make", "check"], "timeout": 120}],
  "publish": false
}
```

Backend values are `pyfly`, `larafly`, `java`, `rust` and `dotnet`. Frontend values
are `react`, `angular`, `nextjs`, `vue` and `none`. Agent values include `codex`,
`claude`, `agentic` and `bridge`. Consult `/profiles` and `/providers` before
selecting a combination.

Commands require non-empty argument arrays and a timeout between 1 and 3600
seconds. Empty commands ask Forge to discover verification from real project
files. There must still be an executable verification gate.

## Follow evidence and errors

A run response reports its ID and status. Poll `/runs/{id}` to obtain events,
verification, changed files, diff, artifacts, error details and any pull-request
URL. Do not infer success from HTTP acceptance of the queue request.

Automatic repairs record an `attempt` number on each command result. The full
`verification` list preserves failed and successful attempts; `final_verification`
contains the checks supporting a successful run, and `source_tree` identifies
their source snapshot. Publication verifies that the workspace still matches
that snapshot. The [operations guide](operations.md) explains the bounded repair
budget and exclusive data-directory ownership.

Invalid input is rejected; a rejected token requires correcting authentication.
A provider or command failure appears as a failed run. Retry creates a separate
attempt and preserves the failed result. Cancellation is a request to stop active
work, not a rollback of external effects.

## Use the CLI

Set `FORGE_URL` to the server origin and supply `FORGE_API_TOKEN` in the client
environment. The CLI uses the same authenticated API:

```sh
export FORGE_URL="http://127.0.0.1:8787"
uv run forge repository list
uv run forge run --repository REPOSITORY_ID --brief 'Build the reservation service.' \
  --backend pyfly --frontend none --agent codex --check 'make check' --wait
uv run forge status RUN_ID
uv run forge export RUN_ID --output changes.zip
```

`forge doctor` reads local provider and profile configuration. `forge cancel`,
`forge retry` and `forge publish` operate on the named run. Publication is an
explicit remote write; export writes a new local file and refuses an existing
output path.

## Integrate a Python client

```python
import os

from firefly_forge.client import ForgeClient
from firefly_forge.models import Command, RunInput

with ForgeClient(os.environ["FORGE_URL"], os.environ["FORGE_API_TOKEN"]) as client:
    repositories = client.repositories()
    run = client.create_run(RunInput(
        repository_id=repositories[0]["id"],
        brief="Build the reservation service described in the product brief.",
        backend="pyfly", frontend="none", agent="codex",
        commands=[Command(argv=["make", "check"], timeout=120)],
        publish=False,
    ))
    print(run["id"], run["status"])
```

Register at least one intended repository before using this example. Queueing a
run does not wait for completion; poll `client.get_run(run["id"])` and inspect
verification before accepting the result.

## Give an external agent MCP access

`forge mcp` runs the standard MCP stdio transport. Configure your MCP client to
launch that executable with `FORGE_URL` and `FORGE_API_TOKEN` in its environment.
The tools list repositories, register a source, create a local run, inspect
evidence, cancel work and explicitly publish a verified run. The token grants
the same trusted operator authority as the CLI; an MCP connection does not add
a separate user authorization model. Keep publishing tool use within the
operator's explicit intent.
