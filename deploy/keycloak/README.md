# Local organization sign-in

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

This sandbox runs Keycloak on your developer machine with forge's logo, Manrope type and light and dark colors. It connects to forge's existing OIDC flow through a confidential client, authorization code with PKCE S256, an exact callback and a separate API audience. Create users and assign their access after the realm starts; the sandbox creates no application users, project memberships or administrator grants.

Use this sandbox for local development and testing. Its HTTP listener is published only on `127.0.0.1`, and its embedded development database persists in a dedicated Docker volume. Production needs HTTPS, a supported external database, backups and reviewed provider administration. See [server identity](../../docs/identity.md) and [factory infrastructure](../../docs/factory-infrastructure.md).

## Prepare without starting anything

Run from the repository root with Python 3.13 or later:

```sh
mkdir -p "$HOME/.local/share/firefly-forge"
python scripts/configure_local_keycloak.py "$HOME/.local/share/firefly-forge/local-identity"
```

The command creates a new directory with owner-only access. It refuses to overwrite an existing directory. `keycloak.env` holds a randomly generated bootstrap administrator password and client secret; `forge.env` holds the matching server settings and a new identity encryption key. Both files have mode `0600`. The realm JSON contains a client-secret environment placeholder, not the secret. Keep these files outside repositories and preserve the encryption key with the factory's private backups.

The default addresses are:

| Service | Address |
| --- | --- |
| Keycloak | `http://127.0.0.1:18080` |
| Realm issuer | `http://127.0.0.1:18080/realms/forge` |
| forge and Studio | `http://127.0.0.1:8787` |
| OIDC callback | `http://127.0.0.1:8787/api/v1/auth/callback` |

For Vite development, pass `--browser-origin http://127.0.0.1:5173` when preparing the directory. The callback remains at the API origin. Use `--api-origin` or `--keycloak-origin` to choose other explicit loopback ports before the first import. This generator accepts HTTP loopback origins only; it is not a production configuration tool.

## Start the identity provider

After checking that the chosen port and local resources are available:

```sh
docker compose --env-file "$HOME/.local/share/firefly-forge/local-identity/keycloak.env" \
  -f deploy/keycloak/compose.yaml up -d
```

The pinned image is `quay.io/keycloak/keycloak:26.8.1`. The compose file mounts the canonical repository logos and Manrope font into a small theme that extends `keycloak.v2`. It retains Keycloak's login, recovery, validation and accessibility templates. See Keycloak's [container setup](https://www.keycloak.org/server/containers), [realm import behavior](https://www.keycloak.org/server/importExport) and [theme documentation](https://www.keycloak.org/ui-customization/themes).

Open the Keycloak address. Read the bootstrap password from the private `keycloak.env` file in your local editor and sign in as `forge-admin`. Do not paste that password into issues, screenshots or application configuration forms.

Select the `forge` realm. Create a user, give that user a password through Keycloak's credentials screen and complete any required account actions. For a deliberately chosen factory administrator, assign the **client role** `forge-api` → `forge-admin`. Ordinary users need no administrator role. The scope mapping allows a role to appear in an access token only when it has actually been assigned; it grants no role on its own. There is no default administrator application user.

## Connect forge and sign in

Load the generated server settings into the process that runs forge, keeping your existing PostgreSQL and knowledge configuration:

```sh
set -a
. "$HOME/.local/share/firefly-forge/local-identity/forge.env"
set +a
forge serve --host 127.0.0.1 --port 8787 --data-dir /YOUR/EXISTING/FORGE/DATA
```

Use an explicit data directory belonging to your intended local factory. This command does not provision a database or migrate retained data. Follow [installation](../../docs/installation.md) for the factory's other dependencies. A forge container cannot reach a host identity provider through its own `127.0.0.1`; this recipe runs forge on the host. Review issuer, routing and TLS separately for a container or cluster topology.

Open Studio, choose **Sign in** and authenticate on the branded Keycloak page. A successful sign-in creates a durable, HttpOnly forge session. Reloading Studio checks that session. **Your account → Sign out** revokes the forge session; it does not log you out of other applications or terminate the provider's SSO session.

Have an authorized project owner assign memberships in forge. A successful login or an administrator role alone does not give a personal API token access to every project. Use **Your account → Personal API tokens** for explicitly selected project observations. Issuance remains unavailable to local bootstrap sessions.

## Check the complete flow

1. Sign in, reload Studio and confirm the expected identity and project memberships.
2. Confirm that an ordinary user cannot administer users or read another project's data.
3. Create a read-only project token with a chosen expiry. Save it once, read its selected project through the API, then rotate it and verify that the old secret is rejected.
4. Revoke the replacement and verify that it can no longer authenticate. Remove a membership and confirm that the corresponding project read is denied.
5. Sign out of Studio and confirm that the old forge cookie no longer authorizes requests.

These are live acceptance steps. Configuration generation and frontend tests alone do not establish that the running provider, SMTP, federation or token delivery has passed them.

## Stop and preserve the sandbox

```sh
docker compose --env-file "$HOME/.local/share/firefly-forge/local-identity/keycloak.env" \
  -f deploy/keycloak/compose.yaml down
```

This stops the sandbox and retains its named data volume. Do not delete that volume to apply configuration changes. Keycloak skips startup import when the realm already exists; edit the existing realm deliberately through its administration console. Changing a generated secret without changing the existing client will break login. The generator refuses replacement for this reason.

Microsoft Entra ID and Google Workspace can be configured as identity brokers inside the realm. Keep forge's exact issuer and client contract. This sandbox does not register external identity-provider applications, configure their credentials or claim federation acceptance. Follow [identity federation](../../docs/identity.md#federation-and-operating-limits).
