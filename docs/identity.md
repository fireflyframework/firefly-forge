---
description: Configure OIDC sign-in and durable server sessions, then enforce current identity and project access on factory operations.
---

# Sign-in and server identity

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

firefly forge authenticates people through Keycloak using an authorization code and PKCE. The browser receives an opaque session cookie. ID tokens, access tokens and refresh tokens stay on the server, encrypted in PostgreSQL.

Authentication identifies the caller. Project memberships determine which products that caller can access. A new identity receives no project memberships automatically; a matching email address does not grant access.

## Configure a Keycloak realm

Create a confidential OpenID Connect client for the Studio. Enable the standard authorization-code flow, require PKCE S256 and register the exact callback URL, such as `https://forge.example/api/v1/auth/callback`. Disable direct password grants and implicit login.

For Keycloak 25 and later, open the Studio client's **Client scopes** and assign the existing `basic` scope as **Default**. Its `Subject (sub)` mapper supplies the signed access-token subject that forge validates alongside the ID token. Retain `profile`, `email` and `roles`, or equivalent explicit claim mappers, when configuring client scopes. Check that an actual signed access token contains `sub`, the API audience and the required client roles; a valid ID token can still accompany an access token missing `sub`. See the [Keycloak client-scope and subject-mapper guide](https://www.keycloak.org/docs/26.5.7/server_admin/index.html) and [Keycloak 25 upgrade changes](https://github.com/keycloak/keycloak/blob/main/docs/documentation/upgrading/topics/changes/changes-25_0_0.adoc).

Configure a separate API audience, for example `forge-api`. Include that audience in access tokens issued to Studio and authorized API clients. Client roles `forge-admin` and `forge-worker` must appear under `resource_access["forge-api"].roles`. forge ignores realm roles and email when deciding these server roles.

Enable refresh-token revocation with a maximum reuse count of zero. The application also claims each refresh operation in PostgreSQL before contacting Keycloak. An uncertain response requires a new sign-in; it cannot silently repeat a possibly consumed refresh token.

In **Realm settings → User profile**, make first name, last name or email optional when the installation does not require those attributes. Keep the username requirement, attribute validators and profile read/write permissions. Use real user information when an attribute remains required. Keycloak documents these controls under [user-profile attribute requirements](https://www.keycloak.org/docs/26.5.7/server_admin/index.html).

Set these server variables through your deployment's secret manager:

| Variable | Purpose |
| --- | --- |
| `FORGE_AUTH_MODE=oidc` | Production authentication mode; this is the default. |
| `FORGE_OIDC_ISSUER` | Exact realm issuer, without a trailing slash. |
| `FORGE_OIDC_CLIENT_ID` | Studio's confidential client. |
| `FORGE_OIDC_CLIENT_SECRET` | Server-side client credential. |
| `FORGE_OIDC_API_AUDIENCE` | Required access-token audience and client-role namespace. |
| `FORGE_OIDC_TRUSTED_ID_TOKEN_AUDIENCES` | Optional JSON array of additional trusted ID-token audiences; defaults to `[]`. Studio's client ID is always trusted. |
| `FORGE_OIDC_REDIRECT_URI` | Exact externally reachable callback URL. |
| `FORGE_BROWSER_ORIGIN` | Optional exact browser origin when a proxy serves Studio at a separate origin. |
| `FORGE_IDENTITY_ENCRYPTION_KEY` | Shared Fernet key for all replicas; required in OIDC mode. |

Both issuer and callback require HTTPS. `FORGE_OIDC_ALLOW_LOOPBACK_HTTP=true` permits HTTP only for an explicitly configured loopback development issuer and callback. It does not permit arbitrary HTTP hosts.

Store the encryption key separately from database backups. Restoring encrypted session rows without their key cannot restore those sessions. Changing the key invalidates existing sessions and pending login attempts. The service does not generate a temporary production key or provide a token-reading API.

Keycloak documents its [OIDC endpoints and grant types](https://www.keycloak.org/securing-apps/oidc-layers). The signed ID-token checks follow [OpenID Connect Core's validation requirements](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation).

## Browser login

1. The Studio posts an empty object to `/api/v1/auth/login` with its application origin.
2. forge creates a single-use, ten-minute PostgreSQL attempt and returns `authorization_url` and `expires_in`, setting its HttpOnly attempt cookie. The Studio navigates the browser to that URL. The attempt's state and browser-binding cookie are hashed; the nonce and PKCE verifier are encrypted.
3. Keycloak returns to `/api/v1/auth/callback`. forge atomically consumes the attempt before exchanging the code, checks the signed ID token's issuer, client audience and nonce, and validates the access token's API audience and subject.
4. forge rotates the session identifier and sets `forge_session`, with `HttpOnly`, `SameSite=Lax` and `Secure` on HTTPS. The cookie has a bounded lifetime; PostgreSQL retains only its hash.

[pyfly](https://fireflyframework.org/frameworks/python/)'s native CSRF filter remains enabled. A safe initial request sets a readable `XSRF-TOKEN` cookie. The Studio copies that value into `X-XSRF-TOKEN` on every unsafe cookie-authenticated request, including login and logout. This CSRF cookie is separate from the HttpOnly identity cookie and is not an authentication token. Cookie-backed clients must satisfy both this check and the exact browser-origin check.

The configured issuer supplies signing keys through pyfly's bounded asynchronous JWKS validator. Tokens require an allowed asymmetric algorithm, key identifier and expiration. A token's own header cannot redirect key retrieval to another URL.

An ID token must include Studio's client ID as an audience. Additional audiences are rejected unless the installation explicitly lists them in `FORGE_OIDC_TRUSTED_ID_TOKEN_AUDIENCES`, for example `["trusted-companion-client"]`. List only clients trusted to share that identity token; this setting does not grant project membership or API access. An `azp` claim, when present, must still match Studio's client ID. Rejected login attempts remain consumed and require a new sign-in.

Cookie-authenticated mutations must carry the configured application's `Origin`. It defaults to the callback's origin in OIDC mode. Set `FORGE_BROWSER_ORIGIN` when a development proxy or separate Studio origin needs a different exact origin. Paths, credentials, query strings and wildcard origins are rejected; HTTP requires the explicit loopback development option. API and MCP clients instead use signed API access tokens with the same issuer and audience checks. They do not receive browser tokens from the session endpoint.

`POST /api/v1/auth/logout` revokes the local session before attempting refresh-token revocation at Keycloak. Local logout succeeds even if the provider is unavailable. It does not sign the user out of every application in the Keycloak realm. Independently issued API JWTs remain subject to their expiration and the provider's revocation policy; offline JWT validation is not continuous token introspection.

## Grant project access

After sign-in or account provisioning, assign a membership to the intended product project. A factory administrator or that project's owner can manage its members. Use the stable local user ID from the authenticated identity APIs; email alone is not an authorization identity.

| Project role | Permissions |
| --- | --- |
| `owner` | Read, edit, execute, review, release and manage memberships |
| `maintainer` | Read, edit, execute, review and release |
| `developer` | Read, edit and execute |
| `reviewer` | Read and review |
| `viewer` | Read |

These project roles differ from the [factory's agent instruction roles](agent-roles.md). A project membership does not grant factory-administrator authority, host CLI access or a configured execution environment. Run, review and delivery controls still apply.

1. Inspect `GET /api/v1/auth/session` for the current identity, administrator state, memberships and role permissions.
2. Read `GET /api/v1/projects/{project_id}/members`. Find an existing local identity through `GET /api/v1/projects/{project_id}/eligible-users?query=NAME`, using a search term between 3 and 120 characters. For a new account, complete [user provisioning](user-administration.md) or ask the person to sign in first.
3. Send `PUT /api/v1/projects/{project_id}/members/{user_id}` with `{"role":"developer"}` or the reviewed role appropriate to that person's work. An explicit `{"role":null}` removes the membership.
4. Read the members list again, then have the user inspect their own session and project list. A successful login with an empty project list can mean that no project membership has been granted.

A denied request needs current administrator or project membership-management authority. A missing user needs local registration or provisioning. Assign another owner before removing the project's last active owner. After access changes, verify the affected user's actual permitted action; queued work also rechecks its authority before protected effects. Repository operations spanning several projects require permission in every project sharing that repository.

## Local bootstrap

`local_bootstrap` is an explicit loopback-only development mode. Submit the configured operator credential once to the login endpoint; the server issues the same opaque, durable cookie. Do not persist that credential in browser storage. An explicitly configured loopback API bearer credential remains available for development tooling.

Browser login and loopback bearer access use the same persistent local operator identity. Bearer requests do not create browser sessions. Suspending that identity blocks both paths; signing in does not enable it again. Run admission checks the current identity before accepting work.

This mode stores an owner-only encryption key beside the factory's private configuration. It does not replace production OIDC and is not suitable for a shared cluster. When starting the application through Python, an explicitly supplied bootstrap credential selects local mode only when no authentication mode has been configured.

## Federation and operating limits

Microsoft Entra ID and Google Workspace can be brokered through a configured Keycloak realm. forge currently trusts one configured Keycloak issuer; it does not independently implement or automatically configure those external identity providers. See Keycloak's [identity brokering guide](https://www.keycloak.org/docs/latest/server_admin/#_identity_broker).

Disabling a local identity blocks its browser and API authentication. forge retains the configured API-client roles from validated access tokens, with their signed issuance time (`iat`), expiry (`exp`) and audience. A newer token replaces that state. Tokens with the same issuance time can only remove roles; an older token cannot restore a removed grant. Role expansion requires a newly issued token. ID-token roles do not establish API or background execution authority.

Queued work checks those current grants before protected effects. Administrator authority must still be unexpired and match the audience recorded at admission. Without it, current project membership may permit isolated project work, but host coding providers and host verification require current administrator authority. Old OIDC administrator snapshots do not establish a verified grant after upgrading; admit a new authorized run when the old snapshot lacks its audience binding. Explicit local bootstrap keeps its separate development contract.

Administrative routes, project lists, host-run admission and user provisioning use the same current-grant check. User administration checks again after obtaining its provider token and before creating an account or submitting an invitation. A principal captured earlier in a request does not retain a removed administrator role.

Removing a role only at Keycloak becomes observable when forge validates a new token or the retained grant expires; this is not continuous provider introspection. Suspend the identity in forge when immediate interruption is required. Login and refresh failures return generic responses without token bodies or provider exceptions. Database sessions and attempt rows are durable across replicas; consumed or uncertain attempts require a fresh login.

Continue with [security boundaries](security.md), [installation](installation.md) and [operations](operations.md).
