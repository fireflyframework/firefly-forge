<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Apply project policies and reusable skills

Policies add versioned instructions and optional executable quality gates to a run. Set project defaults once, then inherit or override them when building, maintaining or testing that repository.

Framework integration is part of the mandatory stack profile. Turning off an optional policy never replaces real PyFly or LaraFly imports, removes workspace confinement, disables credential checks or bypasses passing verification before publication and deployment.

## Choose defaults for a project

The built-in catalog contains these recommendation skills:

| ID | Purpose | Default |
| --- | --- | --- |
| `implementation` | Inspect repository conventions and use real framework service boundaries | Enabled |
| `verification` | Add meaningful behavioral checks and preserve their failure evidence | Enabled |
| `security` | Preserve authentication, CSRF, input validation and credential boundaries | Enabled |
| `accessibility` | Use semantic controls, keyboard focus and accessible feedback | Optional |
| `documentation` | Provide reproducible installation, usage and operational instructions | Optional |

Read `GET /api/v1/policies` or `GET /api/v1/skills` for the active catalog, its source revision and digest. Its entries describe their category, kind, actual instructions and commands.

Set project defaults with `PUT /api/v1/repositories/{id}/policies`:

```json
{"skills": ["implementation", "verification", "security", "accessibility"]}
```

Omit `skills` when queueing a run to inherit the project's defaults. If the project has no explicit defaults, the active catalog's default entries apply. Supply an explicit array to override them for that run; `[]` disables optional policy injection. Unknown or duplicate IDs are rejected.

Recommendations are injected into the chosen worker's prompt. They are useful guidance, but do not guarantee that every suggestion was followed. `quality_gate` policies contain bounded command argument arrays and matching backend IDs; selected commands execute after normal verification and must pass. Core factory gates apply in either case.

## Use the central repository

The [central policy repository](https://github.com/fireflyframework/firefly-forge-policies) holds the JSON catalog and its schema. Review a commit, then synchronize its exact 40-character Git commit ID:

```json
{
  "repository_url": "https://github.com/fireflyframework/firefly-forge-policies.git",
  "ref": "REPLACE_WITH_THE_EXACT_40_CHARACTER_COMMIT_ID",
  "catalog_path": "catalog.json"
}
```

Send this body to `POST /api/v1/policies/sync`. Branch names and moving tags are rejected. The factory fetches the requested commit into a temporary private Git repository, reads the named JSON blob, validates it and saves the active data catalog. It does not check out or execute policy repository code.

Synchronization requires Git read access to the repository. Use a public catalog or an already configured host credential helper for this route. The response identifies the source commit and SHA-256 content digest. A later central commit has no effect until an operator synchronizes it.

For a trusted local checkout, set `FORGE_POLICY_CATALOG` to its JSON catalog file. An explicitly synchronized catalog takes precedence over this fallback. The file must be regular JSON below 1 MB and must satisfy the same schema.

## Define a quality gate

A catalog is an object with `version` and `skills`. A gate entry can add a real Python check:

```json
{
  "id": "python-quality",
  "name": "Python static quality",
  "description": "Run the project's Ruff and mypy checks.",
  "category": "quality",
  "kind": "quality_gate",
  "default": false,
  "instructions": "Configure Ruff and mypy for the existing project and address their findings.",
  "commands": [
    {"backends": ["pyfly"], "argv": ["uv", "run", "ruff", "check", "."], "timeout": 300},
    {"backends": ["pyfly"], "argv": ["uv", "run", "mypy", "src"], "timeout": 300}
  ]
}
```

Use commands that match the actual project layout and dependencies. Gate commands run with the server's development identity; accepting a catalog is trusted operator work. The data-only format prevents a hidden plugin install, but an explicit command still executes software. Keep central policy changes under normal code review.

Every queued run stores `policy_snapshot`, including selected instructions, gate commands, source revision and digest. Repairs keep that snapshot. This makes the instructions and checks reviewable even after the central catalog changes.
