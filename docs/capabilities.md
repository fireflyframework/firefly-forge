<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Know what the factory establishes

Forge implements an end-to-end development loop: register a Git source, queue a
brief with stack and provider, generate in an isolated workspace, run
verification, retain change evidence and optionally publish a pull request.

| Surface | What is implemented | Evidence boundary |
| --- | --- | --- |
| Git workspace | Clone a configured source and create a run branch | Does not isolate the host operating system |
| Primary backends | PyFly and LaraFly instruction profiles | Specific products require their own live checks |
| Additional backends | Java, Rust and .NET profiles | Source inspection is separate from product runtime validation |
| Browser products | Angular, React, Next.js and Vue profiles | Build success is separate from visual and interaction acceptance |
| Codex / Claude Code | Official signed-in CLI execution paths | Installed and authenticated readiness is separate from live generation |
| Firefly Agentic | Native API-driven workspace tool execution | API configuration is separate from actual model access |
| Internal bridge | Compatible endpoint client | Authorized internal service is installed separately |
| Verification | Explicit or discovered project commands with retained results | Commands establish only what they test |
| Automatic repair | Failed checks returned to the provider with bounded retries | Earlier failures remain recorded; final passing checks are separate |
| Source integrity | Verified source-tree snapshot checked before publication | Changed source requires fresh verification |
| SCM publication | GitHub, GitLab, Bitbucket, Azure DevOps and Gitea adapters | Each live host/account requires separate acceptance |
| Run persistence | Local durable records and startup reconciliation | Single-node service; no distributed execution guarantees |
| Service ownership | Exclusive active owner per data directory | Separate services require separate data directories |

## Read a successful run precisely

Generation means the provider returned a result. Verification means configured
commands actually ran and passed. Visual review means someone inspected the UI
at the stated viewports and exercised its controls. Live integration means the
real external service returned the expected outcome. Publication means a remote
branch or pull request was actually created. These facts are independent.

This first version is intended for trusted development environments. It does not
claim tenant isolation, regulated decision authority, production availability,
an SLA, automatic deployment, a universal migration path or correctness for an
arbitrary brief. The operator remains responsible for reviewing generated
changes and the resulting product's own tests and release process.

See the [0.1.0 release notes](release-notes.md) for the specific preparation
checks and remaining live-acceptance boundaries.
