---
description: Manage factory users, project roles and invitations through readable administration flows and current server authorization.
---

# Manage users and invitations

Factory administrators can suspend registered identities and, when a dedicated Keycloak service client is configured, provision new accounts. Project membership remains a separate action. Creating an account or matching an email address grants no project access.

The administration API accepts no passwords, administrator roles or project-role shortcuts. Keycloak owns credential setup and account verification. Browser administrators authenticate through the normal [identity flow](identity.md).

## Suspend a registered identity

Send `{"enabled": false}` to `PUT /api/v1/identity/users/{id}`. The server locks the identity, disables it, revokes every local browser session and appends an immutable audit receipt in one PostgreSQL transaction.

Suspension blocks subsequent forge authentication, including otherwise valid API tokens for that identity. It does not disable the account across every Keycloak-connected application. Send `{"enabled": true}` to allow future forge authentication again; revoked browser sessions remain revoked.

An administrator cannot suspend themselves through this endpoint. The explicit local bootstrap identity cannot be modified. Use another registered administrator to manage an administrator's account.

## Configure provider-managed provisioning

Create a separate confidential Keycloak client with service accounts enabled. Grant its service account the `realm-management` client roles `view-users` and `manage-users`. Keycloak's composite `view-users` role also includes `query-users` and `query-groups`; forge accepts those read-only query roles. Do not grant realm or client administration roles. forge validates the service token's issuer, audience, authorized client and role set before allowing a user mutation.

Set these server-side secrets:

| Variable | Purpose |
| --- | --- |
| `FORGE_KEYCLOAK_ADMIN_CLIENT_ID` | Dedicated least-privileged service client. |
| `FORGE_KEYCLOAK_ADMIN_CLIENT_SECRET` | Its private client credential. |

The service uses the same exact Keycloak realm issuer configured for identity login. It cannot accept an arbitrary administration URL from a request. Provisioning is disabled when these credentials are missing or the application uses local bootstrap authentication.

Configure the realm's user profile to permit administrator-managed attributes `forge_request_id` and `forge_request_digest`, for example through the `ADMIN_EDIT` unmanaged-attribute policy. These provider-owned markers link an account to its durable creation intent. Missing or mismatched markers require reconciliation; the server does not infer identity from email alone.

`GET /api/v1/identity/administration` returns redacted configuration and required permissions. A configured client is not proof that provisioning or SMTP delivery has succeeded. Keycloak's [Admin REST API](https://www.keycloak.org/docs-api/latest/rest-api/index.html#_users) documents account creation and action-email requests.

## Provision an account

Post to `/api/v1/identity/users`:

```json
{
  "request_id": "76287aeb2a014ed2887ba85a480a045f",
  "username": "new-product-owner",
  "email": "owner@example.test",
  "first_name": "Product",
  "last_name": "Owner"
}
```

Generate a fresh request ID for each deliberate operation. The server saves the request digest and encrypted configuration snapshot before contacting Keycloak. A confirmed creation requires HTTP 201, a canonical account location at the configured provider, and a fresh read of that exact account with matching request markers.

The receipt contains the stable local `user_id` and provider `subject`. The local identity is keyed by issuer and subject. Its contact email is not treated as a verified authentication claim, and no project memberships are assigned automatically.

Repeating an identical request returns its existing receipt. Reusing the request ID with different content fails. A timeout, interrupted process or uncertain response never causes an automatic second account-creation request.

## Invite separately

After creation, post a fresh `request_id` to `/api/v1/identity/users/{id}/invite`. The target must be enabled, belong to the configured realm and have an email address at Keycloak.

This action requests `UPDATE_PASSWORD` and `VERIFY_EMAIL` through Keycloak's action-email endpoint. Configure and test the realm's SMTP service first. A successful HTTP 204 produces `invitation_status: "provider_accepted"`; `mail_delivery_verified` remains false. That response proves provider acceptance, not mailbox delivery, link use or completion of account setup.

SMTP errors remain visible as failed receipts. Unknown invitation outcomes require inspection before any deliberate resend. Replaying the same request ID does not send another message.

## Inspect and reconcile

`GET /api/v1/identity/operations` returns bounded recent operations with immutable journal entries and chain validation. Remote credentials, access tokens and raw provider error bodies are not returned. Each receipt distinguishes its operation status, invitation outcome and project-access grant state.

The active execution owner recovers interrupted operations as `needs_reconciliation` during startup. It does not replay them. Reconciliation is rejected while an operation remains in flight.

Post an explicit decision to `/api/v1/identity/operations/{id}/reconcile`:

- `created`: supply the exact provider subject and a note. The server freshly checks that account's request markers before completing the receipt.
- `not_created`: record an administrator's inspection and close the intent without repeating it.
- `provider_accepted` or `not_accepted`: record an explicit invitation assessment. The receipt labels it as an operator attestation and still does not claim email delivery.

A new request after reconciliation is a separate administrator action. It cannot silently reuse an old uncertain operation. Preserve PostgreSQL backups and the shared identity encryption key so encrypted operation snapshots remain readable after recovery.

Continue with [server identity](identity.md), [durable recovery](recovery.md), [security boundaries](security.md) and [product lifecycle](product-lifecycle.md).
