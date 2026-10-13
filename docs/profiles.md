<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Choose a product foundation

Profiles provide concrete framework instructions to each coding provider.
`GET /api/v1/profiles` is the runtime catalog: it reports names, versions,
upstream sources and validation boundaries. Read the catalog for the running
version. The descriptions on this page do not establish release acceptance.

| Backend | Role | Start here |
| --- | --- | --- |
| PyFly | Primary Python service profile | [PyFly walkthrough](pyfly.md) |
| LaraFly | Primary Laravel/PHP profile | [LaraFly walkthrough](larafly.md) |
| Firefly Java | Additional reactive Java/Spring profile | Inspect runtime catalog and upstream contracts |
| Firefly Rust | Additional Rust profile | Inspect runtime catalog and upstream contracts |
| Firefly .NET | Additional .NET profile | Inspect runtime catalog and upstream contracts |

Java, Rust and .NET are separate product validation tracks. An upstream source
inspection or a passing factory test does not establish that a product generated
with one of them has passed its runtime, packaging or deployment checks.

| Frontend | Intended approach |
| --- | --- |
| Angular | Firefly product template and current Frontend Playbook conventions |
| React | Typed React components, accessible UI and production build |
| Next.js | App Router with explicit server and client boundaries |
| Vue | Composition API and typed components |
| API only | No browser frontend |

Angular products use the current template revision and Hub components. An auth
flag in a scaffold is not implemented identity behavior; generated products must
actually implement and test authentication if their brief requires it.

forge Studio itself is a React application. Its own frontend does not restrict
the frontend selected for a generated product.

## Match verification to the repository

Define explicit verification commands when the project's contract is known.
For example, `make check` can cover behavioral tests, lint, types and a build in
one project-owned gate. Command arguments are passed directly to the process,
not interpreted as a shell expression.

Leave commands empty only when the generated project declares a supported check
that forge can discover. A project without a verification command fails
verification; the factory cannot assume a passing result.
