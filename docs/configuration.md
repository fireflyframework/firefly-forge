---
description: Configure factory providers, execution access, identity and private settings without placing credentials in product source.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Configure the development host

Keycloak identifies production users and project memberships authorize their actions. A loopback operator token is available only in explicit local bootstrap mode. Provider credentials authenticate the selected coding worker. Source-control credentials authorize repository access. PostgreSQL stores the run ledger. Keep these responsibilities separate when setting up the host.

## Start with a working baseline

1. Configure a dedicated PostgreSQL database and `FORGE_DATABASE_URL`.
2. Configure [identity](identity.md) and a private `FORGE_DATA_DIR`; select explicit local bootstrap only for a trusted loopback development host.
3. Sign in through Studio. Its opaque HttpOnly session survives reload; credentials remain on the server.
4. Choose an official CLI login or configure a native API provider.
5. Test that connection, connect the intended repository, and run a small [pyfly](https://fireflyframework.org/frameworks/python/) or [larafly](https://fireflyframework.org/frameworks/php/) acceptance brief.

Use a provider's default model first when using its signed-in CLI. If you set a model explicitly, verify that the account can use it in a real run. Native API models require their matching provider key. The account's available models must be checked separately; the factory has no fixed menu that establishes that access.

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
| `bridge` | `model`, `endpoint`, `token`, `timeout`, optional `completion_recovery`, optional `checkpoints` |

For the restricted [closed-text implementation continuation](recovery.md#explicit-closed-text-implementation-continuation), `checkpoints` accepts `disabled` or `required`; required also needs `completion_recovery: required`. Absence does not insert a default into queued snapshots. Review and other tasks do not support required checkpoints.

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
| `FORGE_AUTH_MODE` | `oidc` by default; explicit `local_bootstrap` for trusted loopback development |
| `FORGE_API_TOKEN` | Local bootstrap credential, at least 24 characters; not production authentication |
| `FORGE_ALLOWED_HOSTS` | Allowed host headers for the API |
| `FORGE_CONCURRENCY` | Parallel runs per active execution leader, from `1` to `64`; default `2` |
| `FORGE_PROVIDER_TIMEOUT` | Environment fallback for generation timeout |
| `FORGE_REPAIR_ATTEMPTS` | Additional repair attempts, default `2`, bounded `0..3` |
| `FORGE_POLICY_CATALOG` | Optional trusted JSON policy catalog file |

Restart the server after changing environment-only settings. The configuration API does not mutate process-global environment variables.

## Assign models by task

[Named model profiles](model-profiles.md) keep several models and their credentials separate. Explicit task bindings select the intended profile; an unsupported capability fails instead of silently choosing another model. Product dialogue records its actual task/profile selection in the immutable conversation receipt. Verify implementation, review and visual task behavior separately from saving their configuration.
