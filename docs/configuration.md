<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Configure the development host

The factory operator token protects the API. Provider credentials authenticate the selected coding worker. Source-control credentials authorize repository access. PostgreSQL stores the run ledger. Keep these responsibilities separate when setting up the host.

## Start with a working baseline

1. Configure a dedicated PostgreSQL database and `FORGE_DATABASE_URL`.
2. Start the API with a unique `FORGE_API_TOKEN` and private `FORGE_DATA_DIR`.
3. Connect Studio using the operator token, held in page memory.
4. Choose an official CLI login or configure a native API provider.
5. Test that connection, connect the intended repository, and run a small PyFly or LaraFly acceptance brief.

Use a provider's default model first when using its signed-in CLI. If you set a model explicitly, verify that the account can use it in a real run. Native API models require their matching provider key. There is no fixed model menu claiming account-specific availability.

## Save provider configuration

`GET /api/v1/providers` describes each connection's editable fields, configured model, tools, capabilities and readiness evidence. `PUT /api/v1/providers/{id}/configuration` accepts:

```json
{
  "values": {
    "model": "bridge/default",
    "endpoint": "http://127.0.0.1:8766",
    "token": "your-authorized-token",
    "timeout": "1800"
  },
  "clear_secrets": []
}
```

The server never returns the token. Responses contain `token_configured` or `api_key_configured`. Leaving a secret blank preserves its saved value. To remove it explicitly, include its field name in `clear_secrets`. Clearing a secret also suppresses its environment fallback; enter a replacement before running again.

| Provider | Editable fields |
| --- | --- |
| `codex`, `claude` | `executable`, `model`, `timeout` |
| `agentic` | `model`, `endpoint`, `api_key`, `timeout`, `max_tokens` |
| `bridge` | `model`, `endpoint`, `token`, `timeout` |

`api` remains an alias for `agentic`. Timeout values range from 1 to 7,200 seconds. Direct API output-token limits range from 1 to 131,072. The provider's actual model may impose a lower limit. Generation request and tool budgets remain bounded.

Settings are owner-private plaintext files with mode `0600` under the data directory's `configuration/` directory, which has mode `0700`. This installation does not provide encryption at rest or a hosted secret manager. Use a dedicated operating-system identity and protect backups accordingly. Private configuration refuses symlinked files and unexpected permissions.

## Understand queued-run snapshots

Queueing a run freezes the effective provider settings, including credentials, into a private revision file. The public run record contains a random `config_revision` identifier and nonsecret `execution_config`. It also records the exact selected policy instructions and their digest.

Changing a model, token or policy later affects new runs. An existing run and its repairs use their original snapshots. Retry creates a new run with current provider configuration while retaining the previous run's selected policy IDs. If those IDs were removed from the active catalog, choose the replacement policies before retrying.

Credential values are redacted from run events, errors, summaries, usage and verification output. Source containing a configured credential is rejected before export. Worker and verification subprocesses receive a filtered environment that excludes the factory's database URL and unrelated server credentials. Git authentication is scoped to Git operations.

## Keep operational settings explicit

| Setting | Purpose |
| --- | --- |
| `FORGE_DATABASE_URL` | PostgreSQL async connection URL, using `postgresql+asyncpg://` |
| `FORGE_DATABASE_SCHEMA` | Ledger schema, default `forge` |
| `FORGE_DATA_DIR` | Private configuration, ownership lock and workspaces |
| `FORGE_API_TOKEN` | Trusted operator authentication, at least 24 characters |
| `FORGE_ALLOWED_HOSTS` | Allowed host headers for the API |
| `FORGE_CONCURRENCY` | Active local runs per service, default `2` |
| `FORGE_PROVIDER_TIMEOUT` | Environment fallback for generation timeout |
| `FORGE_REPAIR_ATTEMPTS` | Additional repair attempts, default `2`, bounded `0..3` |
| `FORGE_POLICY_CATALOG` | Optional trusted JSON policy catalog file |

Environment-only changes require restarting the server to make its host setup predictable. The configuration API does not mutate process-global environment variables.
