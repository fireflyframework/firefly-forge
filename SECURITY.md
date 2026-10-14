# Security

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

Report vulnerabilities privately through GitHub Security Advisories for this repository. Include the affected release, a reproduction with synthetic data and the expected trust boundary. Exclude working credentials, private logs and sensitive customer source.

firefly forge is a software factory with native pyfly authentication, project permissions, PostgreSQL persistence and one execution leader. Shared installations use Keycloak OIDC, an exact API audience and protected server sessions. Explicit local bootstrap is restricted to its configured loopback mode. Current identity, project and repository authority must permit execution and later effects; a queued request does not renew that authority.

Host coding and verification execute with the service account's filesystem and process access and require current administrator authority. Project users need an available isolated runner. Use a dedicated service identity and private retained storage. Confined file tools and a coding CLI's permission controls do not establish operating-system isolation for the complete factory. Keep unrelated production credentials and host sockets out of project execution environments.

Owner-private configuration files and immutable credential revisions protect selected service secrets through filesystem permissions. Connection OAuth credentials are separately encrypted in PostgreSQL under the installation's shared encryption key. Browser consent, token exchange, refresh and scoped resource use have retained policy, actor, project and effect checks. Unknown provider responses require reconciliation. Local disconnect stops factory use and erases active tokens; provider-side authorization must be revoked separately.

The implemented local OAuth service requires explicit development settings, different literal factory/provider hosts and its fixed read-only resource. Vendor OAuth templates remain inactive until their provider contracts and scoped adapters are admitted. Schema discovery, exact review and repository/tool grants remain mandatory even after consent. Current authorization also applies when returning cached tool receipts.

Write-ahead intents and receipts retain execution evidence. A database rollback, restart or timeout cannot establish that an external effect did not happen. Observe the exact remote outcome before another publication, deployment or knowledge write. Passing checks and matching source evidence precede delivery; forge does not merge generated changes automatically.

Keep factory PostgreSQL, private credential revisions, source workspaces, flycanon data and original objects in a consistent backup. Test restoration and unresolved-effect recovery in an isolated installation. Production cluster isolation, live vendor integrations and generated-product deployment need their own acceptance evidence. See the [security guide](docs/security.md) and [factory infrastructure guide](docs/factory-infrastructure.md).
