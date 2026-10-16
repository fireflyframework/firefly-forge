---
description: Connect GitHub, GitLab or Bitbucket applications and retain project scope, private repository access and publication authority.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Connect source-control applications

A source-control connection lets forge read granted repositories and publish reviewed changes through the provider's API. GitHub Apps, GitLab OAuth applications and Bitbucket OAuth consumers supply that authority. Your factory sign-in controls who may configure forge; it does not grant access to the provider account.

## Connect one repository

A factory administrator completes these steps in **Connections → Repositories**:

1. Select **GitHub App**, **GitLab OAuth application** or **Bitbucket OAuth consumer**. Use **Create application** to open the provider's registration instructions. Create and approve the application in the organization or workspace that owns the repository.
2. Supply the application fields described below and choose **Save application settings**. Saving those values records configuration; it does not create the provider application or establish a repository grant.
3. Install the GitHub App on the intended repositories. For GitLab or a Bitbucket authorization-code grant, choose **Authorize in…** and finish consent in the same browser. Bitbucket client credentials use the configured consumer and workspace instead.
4. Choose **Check access**. This makes a real provider request. A successful check describes the current grant, not a permanent permission.
5. Choose **Find granted repositories**, inspect the returned repository name and URL, then **Connect NAME**. forge checks discovery again and registers the selected remote repository ID with its provider URL and default branch. It does not accept a caller-substituted URL or credentials in this action.
6. Select that registered repository when defining a product or starting work. Enable [signed conversation commands](repository-workflow.md) or [local GitHub issue polling](#use-github-issue-comments-from-a-local-factory) separately; connecting an application alone does not enable either transport.

<figure class="forge-shot" markdown="1">
![Studio repository-access step in the factory connection wizard.](assets/screenshots/connect-repositories-desktop-light.png)
<figcaption>The Repositories step is the entry point for application access. This controlled Studio screenshot shows the setup stage, with no provider catalog populated; it is not evidence of a live installation or grant.</figcaption>
</figure>

## Understand the two directions

| Direction | Implemented behavior | What the result establishes |
| --- | --- | --- |
| Provider → factory | Discover granted repositories and clone their configured default branch when preparing a fresh run; a campaign can supply its frozen source commit. | The run's recorded source base, not a continuously synchronized workspace. |
| Provider conversation → factory | Accept an enabled signed command, or poll GitHub issue comments for separately authorized `respond` and `status` actions. | A retained delivery and its allowed action; ordinary discussion does not start work. |
| Factory → provider | Push verified source to the run branch and request a pull or merge request; optionally publish separate review-reference files. | A recorded publication outcome, not a merged default branch or deployment. |
| Factory → conversation | Post queued, stopped or terminal status and supported worker questions using the conversation's original identity. | Feedback about retained work; an uncertain reply remains unresolved until checked. |

These are explicit source and conversation flows. forge does not mirror every issue edit into a product definition, continuously merge remote changes into an active workspace, or merge its own pull requests. Resuming a run inspects its retained workspace. New upstream work and merge conflicts require a reviewed source decision; see [recovery](recovery.md) and [repository commands](repository-workflow.md).

<div class="forge-flow forge-flow--dependencies" role="region" aria-label="Source publication flow" aria-describedby="scm-publication-guide" tabindex="0" markdown="1">

```mermaid
flowchart LR
  S[Granted repository] --> W[Isolated run workspace]
  W --> P[Verified review branch]
```

</div>

<p class="forge-flow-caption" id="scm-publication-guide">Source enters an isolated run. Publication requires passing checks and current release authority; the provider still owns review and merge. On a narrow screen, focus the diagram and scroll horizontally.</p>

## GitHub App

[Register a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
under the account that should own it. Request repository **Contents: read and
write** and **Pull requests: read and write**. Install it on the selected
repositories. Save its app ID, installation ID and unencrypted RSA private key
in forge. The optional app slug provides a direct installation link.

### Walk through registration

Start in your organization's **Settings → Developer settings → GitHub Apps →
New GitHub App**. Give the application a name that identifies its purpose and
set its homepage to your factory's documentation. For a local acceptance test,
use a separate test application and a repository containing fictional data.

<figure class="forge-shot" markdown="1">
![GitHub confirms registration of the firefly forge local acceptance application.](assets/provider-guides/github-app-registration.jpg)
<figcaption>Captured from GitHub during the local acceptance setup. The registration banner confirms that the application exists. Installation and a successful connection check are separate steps. Use your own application identifiers; the values shown belong to this test application.</figcaption>
</figure>

Configure these repository permissions before installing the application:

| Permission | Access | Purpose |
| --- | --- | --- |
| Contents | Read and write | Clone source and publish reviewed branches. |
| Pull requests | Read and write | Open pull requests and post feedback in pull-request conversations. |
| Issues | Read and write, when using issue conversations | Post status and questions back to the issue that requested the work. |
| Metadata | Read-only | Discover the repositories granted to the installation. GitHub marks this permission as mandatory. |

Organization and account permissions are not needed for this installation-token
flow. GitHub user authorization is separate from the factory's sign-in and from
the repository's signed conversation-command settings.

Complete setup in this order:

1. In **Credentials → Key pairs → New key**, generate an RSA private key. Keep
   the downloaded key out of source control, screenshots and shared support
   reports. Supply it only to forge's write-only private-key field. If the
   download is interrupted, check the browser's downloads and the application's
   existing key list before creating another key.
   [Verify the downloaded key](#verify-the-downloaded-key) before supplying it to forge.
2. Open **Install App**, select the intended account and choose **Only select
   repositories**. Select the test repository and inspect the permission summary
   before confirming the installation. Record its installation ID.
3. In forge, save the app ID, installation ID, private key and optional app slug.
   Choose **Check access**, then **Find granted repositories**. Check the returned
   repository name before connecting it.
4. Choose the incoming conversation transport. [Signed inbound commands](repository-workflow.md)
   need an HTTPS route that GitHub can reach and a separate webhook secret.
   [Local issue polling](#use-github-issue-comments-from-a-local-factory) reads through
   the App API and needs no incoming webhook route. Its supported actions are
   limited to `respond` and `status` on existing scoped runs. Registration and
   repository discovery alone establish neither transport.

### Verify the downloaded key

In the GitHub App's **Credentials → Key pairs**, find the public SHA256 fingerprint for the key pair. On your own machine, run this in Bash or Zsh with the path to the downloaded PEM file:

```bash
set -o pipefail
openssl rsa -in "/path/to/downloaded-key.pem" -pubout -outform DER |
  openssl sha256 -binary |
  openssl base64
```

This fingerprints the public key's DER SubjectPublicKeyInfo bytes; it does not print the private key. Compare the base64 result with GitHub's displayed fingerprint. If any command fails or the values differ, stop and check the file, application and selected key pair. Do not retry authentication or generate another key to work around a mismatch. A matching fingerprint identifies the key pair; installation and repository access still require their own checks. See [GitHub's private-key verification instructions](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps#verifying-private-keys).

<figure class="forge-shot" markdown="1">
![The installed GitHub App has read and write permissions on one selected private acceptance repository.](assets/provider-guides/github-app-installed.jpg)
<figcaption>After installation, GitHub shows the effective permissions and the selected repository. This acceptance application is restricted to one private repository containing fictional data. The image records the provider installation; a forge grant check and webhook delivery still need their own evidence.</figcaption>
</figure>

These captures record registration and installation. A complete acceptance
record also needs a real grant check, a confirmed inbound command and its
outbound feedback. Replace screenshots when the provider changes its screens
or the documented workflow changes; review every image for credentials before
publishing it.

forge signs a short-lived app JWT. Repository discovery uses an installation
token with metadata permission. Git execution and pull requests use a token
restricted to the selected repository ID, with the two write permissions above.
Tokens are refreshed before expiry. Organization approval, branch protections
and workflow-file permissions can still prevent an operation. See
[GitHub's installation-token contract](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

For GitHub Enterprise Server, set **Host** to its HTTPS origin. forge derives
`/api/v3` from that origin and rejects clone URLs or pagination links from another
host. App installation and permissions must exist on that same server.

## GitLab OAuth application

[Register an OAuth application](https://docs.gitlab.com/integration/oauth_provider/)
with the redirect URI displayed in forge:

```text
https://your-forge.example/api/v1/scm/connections/gitlab/callback
```

Use `api`, `read_repository` and `write_repository` scopes for merge requests and
Git access. Save the application ID and redirect URI; save its client secret
when the application is confidential. Select **Authorize** to complete the
provider's consent screen.

forge uses authorization code with PKCE, a browser-bound state that expires
after ten minutes, and server-side rotating refresh tokens. State can be used
once. Discovery requests non-archived projects where the authorized user is a
member with developer access or higher. GitLab OAuth follows the user's grants;
it does not provide GitHub's per-repository installation-token restriction.
Self-managed GitLab uses its configured HTTPS origin. See the
[GitLab OAuth API](https://docs.gitlab.com/api/oauth2/).

## Bitbucket OAuth consumer

Create an [OAuth consumer in the workspace](https://support.atlassian.com/bitbucket-cloud/docs/use-oauth-on-bitbucket-cloud/)
and enable `repository`, `repository:write`, `pullrequest` and `pullrequest:write`.
Save its client key, client secret and workspace slug. The default grant is
**client credentials**: forge obtains a short-lived token representing the
consumer's owner and lists repositories in the configured workspace.

For user consent, choose **authorization code**, register the forge callback
URI and select **Authorize**. This uses browser-bound, single-use state and
rotating refresh tokens. Client-credentials tokens are obtained again on expiry;
that grant does not require a refresh token.

API calls use bearer authentication at `api.bitbucket.org`. App passwords are
deprecated and are not used by this connection. See the current
[Bitbucket authentication contract](https://developer.atlassian.com/cloud/bitbucket/rest/intro/#authentication)
and [OAuth changes effective May 2026](https://community.developer.atlassian.com/t/oauth-2-0-and-api-authentication-changes-for-bitbucket-cloud/99003).

## Protect and revoke access

Private keys, client secrets and OAuth grants stay in `configuration/` under the
server's private data directory. Files have `0600` permissions and the directory
has `0700` permissions. API summaries return configured flags for secrets;
secret values cannot be read back. Blank secret fields preserve saved values.
Use the explicit clear action to remove them. Changing application settings
invalidates saved grants, cached tokens and pending authorization state.
Changing an OAuth host or client ID also requires an explicitly supplied client
secret or an explicit clear action. forge refuses to carry a confidential
client's saved secret to that new destination. A public GitLab client can clear
the old secret before starting a new authorization.

Before refreshing a rotating token, forge persists a dispatch marker in its
current private SCM grant storage. A timeout, invalid response or interrupted
process leaves that marker unresolved; subsequent requests cannot send the old
refresh token again, including after restart. The connection reports
**Needs authorization**. Complete a new provider consent flow to establish a
fresh grant. This recovery does not claim whether the previous provider request
succeeded. SCM storage remains owner-private configuration. The [connected-account OAuth lifecycle](connected-accounts.md) uses a separate policy, client and credential store; configuring it does not register an SCM application or move these grants into that store.

Token issuance and repository discovery also check that the application revision
did not change while a provider request was in flight. An obsolete response cannot
cache credentials or register a repository. Repeat the operation after saving the
current application configuration. Malformed credential or repository responses
fail closed; they do not mark an application as connected.

Git credentials are passed only to the clone or push process. They are not
written into repository URLs, `.git/config`, global Git configuration or generated
products. Coding and verification workers do not receive those credentials.
Provider errors omit response bodies that might contain sensitive data.

Revoke an installation or OAuth grant at the provider to remove its authority.
forge's next grant check fails if access is revoked. Clearing the local secret
removes forge's copy; it does not uninstall the provider's application. Existing
remote branches and pull requests remain.

## Connection API

All routes below use `/api/v1` and require current factory-administrator authority, except the two OAuth callback routes. Hosted clients authenticate through the configured OIDC identity; explicit local bootstrap uses its loopback operator credential. Callbacks require the saved one-use state and browser cookie. Factory credentials never appear in a redirect URL. See [identity and project access](identity.md).

| Method | Path | Result |
| --- | --- | --- |
| GET | `/scm/connections` | Application summaries and setup fields |
| PUT | `/scm/connections/{provider}` | Write-only settings and explicit secret clearing |
| POST | `/scm/connections/{provider}/test` | Live grant-test summary |
| GET | `/scm/connections/{provider}/repositories` | Repositories in current grants |
| POST | `/scm/connections/{provider}/repositories/connect` | Register a freshly discovered repository ID |
| POST | `/scm/connections/{provider}/authorize` | GitLab or Bitbucket browser authorization URL and private cookie |
| GET | `/scm/connections/{provider}/callback` | Complete GitLab or Bitbucket consent |

```json
{
  "values": {"app_id": "123", "installation_id": "456"},
  "clear_secrets": []
}
```

The connection request is `{"repository_id":"42"}`. It does not accept a URL
or credentials supplied by the caller.

## Publish a verified change

Studio runs begin with `publish: false`. After verification succeeds, **Publish
pull request** requests publication of the verified checkpoint to the run's branch and invokes the provider's pull-request API. An API-only server retains the publication request for its execution owner; an accepted request is not yet a remote pull request.
Operators can also use `forge publish RUN_ID` or explicitly request `publish:
true` when creating a run. Publication rechecks project release permission, the application revision, source/checkpoint evidence and any required review. A test-only run cannot publish an implementation branch. forge does not merge a pull request or deploy a product through this action. Project reviews and branch protections still apply.

If a publication intent has no confirmed outcome, inspect the retained run and the provider's branch and pull request before another action. A timeout does not establish that nothing was published. [Recovery](recovery.md) explains the separate publication and execution intents.

Generic Git, Azure DevOps and Gitea retain the manual repository path. HTTPS URLs
must not contain credentials. Configure an appropriate Git credential helper or
SSH agent for those sources. Their existing provider-specific publication settings
remain available; GitHub, GitLab and Bitbucket application connections provide
the managed grant flow described above.

Contract tests check request construction, state handling and failure boundaries.
Loopback HTTP acceptance runs a real forge API server and a provider fixture
server for GitHub Enterprise `/api/v3`, self-managed GitLab `/api/v4`, and
Bitbucket Cloud. It verifies operator authentication, GitHub JWT signatures,
GitLab PKCE and refresh rotation, bearer-authorized repository discovery, signed
or secret-token ingress, outbound conversation feedback, revocation, and receipt
reconciliation without duplicate comments. Test-only routing sends the configured
provider paths and Host headers to the local fixture; it does not test provider
DNS, TLS, organizational installation, or consent on a real provider account.
Live acceptance requires the real application registration, provider consent,
repository grant and successful remote operation for each account.

## Explicit SCM delegation

After a delegated `respond`, `status`, or `cancel` action completes, Forge retains
its result and schedules a comment in the same issue or pull-request conversation.
The comment includes the action result and delivery receipt reference. Answering
a worker question records the answer; it leaves the run paused and does not grant
permission to resume, publish, or merge.

Later status comments show the current run status separately from the original
action result. A recorded answer can still say that the run was paused when it
was accepted while the current status reports a subsequent failure. A cancellation
acknowledgment does not by itself prove that the worker has drained.

Forge records the outbound intent before contacting the provider. If delivery is
uncertain, the completed action and its original receipt remain available with
feedback marked `needs_reconciliation`. Processing the same receipt again does not
post another comment. An operator must check the provider outcome and record an
explicit reconciliation; an unknown delivery is never an invitation to retry it.

Signed comments identify a configured channel; they do not sign a user into Forge.
A factory administrator can explicitly map a verified provider account to an existing,
enabled Forge user for selected components and actions in one project and repository.
The mapping grants no project membership or global administrator privileges.

The first scoped adapters support answering a paused worker question, reading a specific
run's status, and requesting cancellation through the existing delivery-request runtime.
Answering leaves the run paused. It does not resume work. Cancellation is admission of a
request, not proof that the worker has drained. Other actions in a repository with SCM
delegation configured report setup required until their native authorization adapter is
available. Creating a new project from an unmapped provider account is separate work.

Use `GET /api/v1/projects/{project_id}/repositories/{repository_id}/scm-actor-bindings/candidates`
to select an already retained, verified account reference. This read does not contact the
provider. Submit the reviewed `version: 1` mapping to the same path without `/candidates`.
Include one durable request ID, the reviewed project-binding digest, the verified-reference
digest, the existing native user, explicit component IDs, allowed actions, and the previous
mapping digest for an update.
The webhook trust acknowledgment must be `true`: anyone holding that channel's webhook
credential can construct a sender claim. The mapping does not establish personal OAuth
authentication.

After a lost response, read `/scm-actor-bindings/requests/{request_id}` using the original
project, repository and request ID. A timeout, denial or missing receipt does not authorize
another write identity. Permission to return a receipt is checked after commit; denial
can therefore leave a committed mapping. The base GET lists current-head summaries with
an optional `cursor` and pages of at most 50 items, rather than a partial revision history.
Each item reports active, revoked, stale-project or stale-connection status. Active means
a saved mapping head; it does not establish current provider grants or native permissions.
Historical requests remain available through their exact request GET. Mapping revisions are immutable; limits are 100 bindings
per repository, 50 revisions per binding, 5,000 requests and 16 MiB of retained mapping
history. Each receipt envelope is limited to 64 KiB. Limits refuse rather than prune.

Connection or webhook-key rotation invalidates the mapping's pinned verification cut.
Review a fresh signed candidate and explicitly update the mapping. Legacy receipts remain
readable, but cannot be promoted into verification evidence. Legacy actor-label-only worker
responses are refused as `scm_authority_setup_required` before an action intent is recorded.
An uncertain outcome after an admitted action remains uncertain and is not replayed.

SCM worker answers carry `responding_actor` version 2 and a mapping/delivery reference.
Ordinary authenticated answers retain version 1; historical answers without attribution
retain their original bytes. A delegated answer cannot confirm a pending manual version-1
answer marker. Historical delegation is recorded evidence, not a statement that the
mapping remains active or that provider grants were freshly checked by the reader.

Provider verification follows the documented raw-body contracts:
[GitHub delivery signatures](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries),
[GitHub App issue-comment installations](https://docs.github.com/en/webhooks/webhook-events-and-payloads#issue_comment),
[GitLab signing tokens](https://docs.gitlab.com/user/project/integrations/webhooks/#signing-tokens),
and [Bitbucket Cloud signatures](https://support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/).
New GitLab delegation requires its timestamped signing-token signature; the legacy shared
token remains supported for old ingress but does not qualify a new delegation candidate.
Local signed-fixture tests are separate from live provider installation acceptance.


## Opt-in same-run resume (V2)

V1 mappings still allow only `respond`, `status` and `cancel`. A separate V2 grant lets an explicitly mapped native user request continuation of the same answered, paused clarification. It grants no membership, provider login, publication, merge or handoff authority. Administrators can configure consent through the API, the separate Studio panel or the [SDK, CLI and MCP grant adapters](agent-integrations.md#consent-to-signed-same-run-resume).

<div class="forge-flow forge-flow--dependencies" role="region" aria-label="Same-run continuation journey" aria-describedby="scm-resume-guide" tabindex="0" markdown="1">

```mermaid
flowchart LR
  A["Administrator:<br/>review consent"] --> Q["Mapped person:<br/>answer question"]
  Q --> S["Signed status:<br/>inspect sealed cut"]
  S --> R["Explicit resume:<br/>recheck cut"]
  R --> C["Original run:<br/>queue continuation"]
```

</div>

<p class="forge-flow-caption" id="scm-resume-guide">Consent and continuation are separate decisions. Status reads leave the run paused. A changed plan, answer, permission or journal head refuses the reviewed continuation. Focus and scroll the diagram on a narrow screen.</p>

1. Review the current V1 receipt and its exact project/component cut. The mapping must be active, include `respond`, and identify an enabled native user. That user needs current project execution permission when requesting continuation.
2. As a current factory administrator, submit `SCMResumeGrantInputV2` to `POST /api/v1/projects/{project_id}/repositories/{repository_id}/scm-resume-grants`. Set `version: 2`, a fresh durable `request_id`, the expected previous V2 digest (null for the first grant), the project-binding digest, the exact V1 request ID and digest, selected component IDs, `allowed_actions: ["resume"]`, `active: true` and `acknowledge_same_run_resume: true`. The server derives the native user from V1. Replacing or revoking that V1 mapping invalidates the V2 grant.
3. Retain the request ID before sending. Recover a lost response through `/scm-resume-grants/requests/{request_id}` under the original project and repository. A timeout, 409 or 404 does not authorize a replacement request. The `/current/{binding_request_id}` read returns the retained grant head for that binding; its receipt alone does not establish current execution permission or provider grants. V2 resume uses this separate consent and does not require enabling the legacy `resume` channel command. Collaboration must still be enabled for the trusted actor. V2 status also requires the existing `status` command permission.
4. After the user answers the question, request its review cut with `/forge status {"version":2,"run_id":"RUN_ID","expected_grant_digest":"GRANT_DIGEST"}` through configured, verified SCM ingress. The result contains the exact question, answer, journal head and sealed development-plan references. Reads leave the run paused.
5. Explicitly send `/forge resume` with `version: 2`, the same `run_id` and `expected_grant_digest`, plus `expected_question_id`, `expected_response_digest` and `expected_cut_digest` from that review. These placeholders stand for the original canonical IDs and digests. The command accepts no agent, model, stack, automatic-retry, publication or handoff options. A changed question, answer, grant, source/configuration cut or journal head requires review before another explicit delivery.

The service checks current native identity, project permission, trusted SCM actor, active V1/V2 heads and the pinned connection. It checks the application's repository grant before acquiring transaction locks, then rechecks the retained local cut under those locks. Both the original execution identity and the delegated identity must still be registered and enabled. Remote revocation is not atomic with a local database transaction. `Journal.resume` separately revalidates the original execution actor and keeps that actor and its frozen execution configuration.

A successful continuation queues the original run and retains its native resume and completion records. It does not prove that the resumed work succeeded. Reprocessing that same delivery checks the recorded completion and current private-read authority before returning it; it cannot resume a later question. Unknown intents remain held for reconciliation. Outbound status feedback follows the existing durable intent and no-repost rules.

In Studio, open **Project access → Source-control delegation**, read the current delegations, then open the exact revision for the mapped person. The **Same-run resume consent** panel provides current and original-receipt reads. A native factory administrator can choose **Review resume consent**, inspect the mapping, choose components, and acknowledge the separate permission before recording it. Clearing **Allow this person to request same-run resume** records a revocation.

On a short screen, scroll through the review to its action buttons. **Back** returns to the previous step and keeps the review open. Recording consent still requires the final acknowledgment.

Studio retains the complete request binding before one POST and confirms it through an exact receipt read. After a lost response, choose **Check original consent**. A conflict, 403 or missing exact receipt holds the original request; there is no reset or automatic resend. The browser must support Web Locks on HTTPS or localhost to coordinate retention across tabs. These controls do not create signed provider ingress or resume a run from the browser; the explicit status and resume commands above remain the continuation path.

This first slice refuses any cancellation request, publication state, unfinished sandbox, unresolved external effect or contradictory plan. It requires an original plan with a matching journal reference, so a prior handoff without such a reference is unavailable here. The cut is bounded to 2,000 journal records, tasks and run-summary events per category. It admits at most 4 MiB of combined journal/task/plan evidence, a 1 MiB run snapshot and plan, and a 3-second statement timeout. Limits refuse instead of returning partial evidence. Consent history has separate V2 retention limits of 100 heads, 50 revisions per head, 5,000 requests and 16 MiB, with 64 KiB receipt envelopes.

Configured ingress remains a prerequisite. This adapter does not add authenticated provider polling, a local GitHub relay or an automatically configured webhook. Controlled host tests of the command and service do not establish live GitHub delivery or native database/worker acceptance.

## Change an existing functional product

An existing project can separately authorize a mapped SCM account to change its functional product definition. A factory administrator must record an explicit whole-product edit capability. Reply delegation and same-run resume consent do not grant this permission. The mapped native user still needs current read and edit access in every project that shares the product repository.

This capability covers the project's complete component graph and exact `product_repository_id`. It changes features, integration contracts, journeys, acceptance criteria and user stories through the existing incremental product-change format. The saved result clears functional and design approval, and architecture approval when present. It retains unchanged features and their impact references. It does not change project topology, create a project, start a run or approve the resulting definition.

Record the capability with `POST /api/v1/projects/{project}/repositories/{repository}/scm-product-change-grants`. Its version-1 body contains an original `request_id`, the reviewed previous grant digest, the full project binding digest, the exact V1 mapping request and digest, every component ID, `allowed_actions: ["product_change"]`, `active`, and `acknowledge_product_definition_edit: true`. The mapping identifies the registered native user; the request cannot supply another user or actor label. Saving consent checks that user's current edit access and grants no membership.

Read the exact consent through `/scm-product-change-grants/requests/{request}` or its current reference through `/scm-product-change-grants/current/{binding_request}` under the same project/repository path. Historical receipts remain readable with current native read access after revocation. A historical active setting does not establish current permission to act.

Send one signed `/forge product_change` command with the following fields in its JSON options:

| Field | Value to retain before sending |
| --- | --- |
| `version` | Integer `1` |
| `project_id` | The existing project |
| `expected_project_binding_digest` | The reviewed full project cut |
| `expected_grant_request_id`, `expected_grant_digest` | The exact edit capability |
| `change.request_id` | The original 32-character hexadecimal operation identity |
| `change.base_revision`, `change.base_digest` | The exact saved functional product being edited |
| Other `change` fields | The existing `FeatureChangeInput` patch, with no execution or review-approval options |

The SCM command cannot override the verified provider, installation, repository or account. Forge checks the application's repository grant before acquiring database locks. It then checks current native permissions, mapping, capability, project cut and product base under the native locks. A stale base or known missing authority refuses before a product revision or action result is written. A successful change records the immutable product revision and delegated action receipt in one transaction.

Recover the original result with `GET /api/v1/projects/{project}/repositories/{repository}/scm-product-changes/requests/{change.request_id}`. The receipt retains the original source, mapped native user and resulting product revision/digest. A later product edit does not rewrite it. Current product GET returns the latest definition, which may have advanced since that receipt. Keep the original request identity after a lost response, conflict or missing exact receipt; none permits an automatic resend. Permission to return a response can be revoked after the product transaction commits, so a denied response does not establish that no product was saved.

The Studio consent flow and SDK/CLI/MCP adapters use these same API contracts. Local source or controlled transport checks do not establish native, installed or live provider acceptance. This cut does not post an automatic SCM comment for a product change. Inspect the retained result through the exact API read. Provider delivery configuration remains a prerequisite; local signed fixtures do not establish live provider acceptance, project bootstrap or GitFlow behavior.

Product changes accept at most 80,000 UTF-8 input bytes. Saved product revisions and project cuts are bounded to 1 MiB each. Consent retains at most 100 heads, 50 revisions per head and 5,000 original requests; product edits retain at most 5,000 operations per repository. The combined consent and edit-event history has a 16 MiB budget, with 64 KiB consent and 128 KiB action envelopes. Limits refuse the operation; they do not omit evidence or report partial success.

## Use GitHub issue comments from a local factory

Authenticated polling lets an execution-owning worker read GitHub issue comments through the configured App installation when GitHub cannot reach the factory. It uses the server-derived transport `github_rest_poll_v1`, without a webhook signature or personal OAuth sign-in. GitLab and Bitbucket continue to use their existing webhook paths.

The polling workflow supports an existing run with an explicit project and component. `status` reads its current state; `respond` answers its exact pending worker question and leaves the run paused. Run creation, resume, cancellation, model/source changes, publication and merge are outside this transport. The [same-run resume grant](#opt-in-same-run-resume-v2) and [product-change capability](#change-an-existing-functional-product) require their separate signed-ingress contracts.

<div class="forge-flow forge-flow--dependencies" role="region" aria-label="Local GitHub polling and clarification flow" aria-describedby="scm-polling-guide" tabindex="0" markdown="1">

```mermaid
flowchart LR
  subgraph SetupPhase["Setup"]
    direction TB
    App["Connect App"] --> Config["Enable polling"]
  end
  subgraph ConsentPhase["Review"]
    direction TB
    Baseline["Finish baseline"] --> Consent["Record consent"]
  end
  subgraph ReplyPhase["Use"]
    direction TB
    Comment["New comment"] --> Evidence["Check reply and WAL"]
  end
  Config --> Baseline
  Consent --> Comment
```

</div>

<p class="forge-flow-caption" id="scm-polling-guide">Existing comments establish the baseline and account observations. They do not dispatch actions. After readiness and consent, publish a new comment and inspect its retained result. Focus and scroll the diagram on a narrow screen.</p>

### 1. Prepare the App and native permissions

Connect the selected repository through the [GitHub App](#github-app). The installation needs **Issues: read** for polling and current collaborator checks, and **Issues: write** to send feedback. Repository discovery uses the installation's metadata grant. forge requests a separate issue-read token for scans and an issue-write token for replies; it does not use an owner token or increase publication permissions.

Register the intended native user and assign [project access](identity.md#grant-project-access). In the repository's collaboration settings, enable the workflow, enter the exact numeric GitHub account in `trusted_actors`, and permit `status` in `allowed_commands` and/or `respond` in `channel_commands`. Keep unrelated settings unchanged. A GitHub login name or `author_association` is not an authorization rule.

| Decision | Required authority |
| --- | --- |
| Save polling configuration or API-observed consent | Current factory administrator |
| Read setup and candidate observations | Current factory administrator |
| Read polling status, heads or an exact configuration/consent | Current project/repository read access |
| `/forge status` | Active polling consent for the component and action; mapped user's current project `read` permission |
| `/forge respond` | Active polling consent for the component and action; current project `execute`, plus `release` when the run has publication enabled |
| Inspect global delivery receipts or reconcile feedback | Current factory administrator |

The mapping grants no membership. The mapped user must remain registered and enabled. A webhook V1 mapping, a same-run resume grant and this API-observed V2 consent are separate records; saving one never upgrades another.

### 2. Save one polling configuration

Configure the CLI's `FORGE_URL` and `FORGE_API_TOKEN` through the [operator environment](agent-integrations.md). The token belongs to forge, not the GitHub App. Keep it out of comments and request documents.

```sh
forge project scm-polling setup PROJECT_ID REPOSITORY_ID
```

Inspect the returned project and repository. Copy its `project_binding_digest` and `connection_revision` into a local `configuration.json` document. Use the returned `expected_previous_digest` for an update, or JSON `null` for the first configuration. Replace the uppercase placeholders below with the exact returned values and a new 32-character lowercase hexadecimal request ID:

```json
{
  "version": 1,
  "request_id": "NEW_CONFIGURATION_REQUEST_ID",
  "expected_previous_digest": null,
  "expected_project_binding_digest": "PROJECT_BINDING_DIGEST",
  "expected_connection_revision": "CONNECTION_REVISION",
  "enabled": true,
  "interval_seconds": 60
}
```

Save the document before sending it. Choose either CLI or SDK for each write. Send once and keep that request ID for exact recovery:

```sh
forge project scm-polling configure PROJECT_ID REPOSITORY_ID configuration.json
forge project scm-polling configuration PROJECT_ID REPOSITORY_ID CONFIGURATION_REQUEST_ID
```

The setup read does not contact GitHub. The configuration write checks current repository access and the reviewed connection/project cut. An interval must be between 60 and 3600 seconds. Each repository has one current polling project. Moving its configuration to another project invalidates the previous project's action snapshots while preserving authorized historical reads. A concurrent change refuses the reviewed request instead of silently using another cut.

### 3. Finish the baseline before posting commands

The execution-owning worker scans issues and comments through its normal lifecycle. An API-only process can configure and read polling, but does not run the consumer. Read status:

```sh
forge project scm-polling status PROJECT_ID REPOSITORY_ID
```

| Status field | What to check |
| --- | --- |
| `ready` | Wait for `true` before consent and new command comments. |
| `baseline_complete` | The initial scan finished; current availability can still make `ready` false. |
| `phase`, `completed_scans`, `last_success_at` | Retained scan progress, not proof of command success. |
| `availability`, `retry_at`, `next_due_at` | A blocked local cut or failed poll is unavailable; provider backoff delays reads. |
| `source_conflicts` | An observed comment changed; inspect the retained evidence before proceeding. |

This status read checks retained progress and current local configuration. The next scan or action performs the provider requests; `ready` is not a fresh GitHub grant check.

Existing comments are retained without dispatch during the initial baseline. A new configuration starts another baseline and retains earlier evidence; restarting the same configuration resumes its saved cursor. After `ready: true`, the operator should tell participants to publish **new comments**. Editing a baseline comment does not turn it into an action.

If the intended account has no retained observation, ask that person to post an ordinary, nonsecret comment in an issue. Wait for a successful scan, then inspect the candidates. Discussion does not dispatch work.

### 4. Review a separate account consent

```sh
forge project scm-polling candidates PROJECT_ID REPOSITORY_ID
```

Select an observation from the exact repository and review its numeric author ID. Use its `id` and `reference_digest`, plus the page's `project_binding_digest`, in `consent.json`. Replace the placeholders with reviewed values; `component_ids` contains actual component IDs from the project:

```json
{
  "version": 2,
  "request_id": "NEW_CONSENT_REQUEST_ID",
  "expected_previous_digest": null,
  "expected_project_binding_digest": "PROJECT_BINDING_DIGEST",
  "observed_receipt_id": "OBSERVED_RECEIPT_ID",
  "expected_reference_digest": "REFERENCE_DIGEST",
  "native_user_id": "REGISTERED_NATIVE_USER_ID",
  "component_ids": ["api"],
  "allowed_actions": ["respond", "status"],
  "active": true,
  "acknowledge_api_observed_identity": true
}
```

Use `null` only for the first consent for this exact account/project/provider scope. For an update, copy the previous receipt's digest. Keep only the components and actions the person needs. Save the complete document before one submission:

```sh
forge project scm-polling consent PROJECT_ID REPOSITORY_ID consent.json
forge project scm-polling binding PROJECT_ID REPOSITORY_ID CONSENT_REQUEST_ID
forge project scm-polling heads PROJECT_ID REPOSITORY_ID
```

The server re-reads the comment, issue and current collaborator permission, compares the numeric author ID, and checks the native user, project cut and configuration. The request accepts references to server observations; it does not accept a caller-created trusted observation. Current heads are saved consent revisions, not certification of current provider grants. `heads --after BINDING_KEY` continues after the exact returned key.

The SDK uses the same documents and original IDs. At the corresponding step above, `configure_scm_polling(project, repository, configuration)` submits the configuration and `configure_scm_polling_binding(project, repository, consent)` submits the separate consent. Use exact reads after an uncertain response. For example, set `PROJECT_ID` and `REPOSITORY_ID` to the reviewed IDs in your environment, then recover a consent already sent at this step:

```python
import json
import os
from pathlib import Path
from firefly_forge.client import ForgeClient

consent = json.loads(Path("consent.json").read_text())
with ForgeClient(os.environ["FORGE_URL"], os.environ["FORGE_API_TOKEN"]) as factory:
    receipt = factory.scm_polling_binding(
        os.environ["PROJECT_ID"], os.environ["REPOSITORY_ID"], consent["request_id"]
    )
```

The other SDK reads are `scm_polling_setup`, `scm_polling_configuration`, `scm_polling_status`, `scm_polling_candidates` and `scm_polling_heads`, with the same project/repository arguments. The exact configuration read also takes its original request ID. An SDK exception does not authorize another POST.

### 5. Publish a new status or answer comment

From the consented GitHub account, post one complete command in an issue of the configured repository. Replace the placeholders with the existing run ID and its exact pending worker-request ID:

```text
/forge status {"run_id":"RUN_ID"}
/forge respond {"run_id":"RUN_ID","request_id":"PENDING_QUESTION_ID","answer":"Keep the existing contacts."}
```

These are separate comments and separate decisions. A status read changes no run WAL. An answer records one `worker_response` with additive `responding_actor.version: 3`, the mapped native user and exact polling-consent/delivery references. The original execution actor remains unchanged. In Studio, inspect the run's [agent question and answer](agent-lifecycle.md) and its historical attribution. `forge journal RUN_ID` exposes the retained chain. Answering leaves the run paused; any continuation needs its own supported, explicit authority.

Pull-request conversations, bot accounts, issue bodies and edited comments cannot dispatch these polling actions. The worker rechecks the current comment and account access before admission, then rechecks local authority around the retained write. Configuration changes and collaboration trust/enable changes share the project membership fence used by Journal responses. These checks do not make remote GitHub revocation atomic with a local database transaction.

### 6. Inspect the reply and preserve unknown outcomes

The App posts feedback to the originating issue with the delivery receipt reference and original action result. Later feedback reports the current run status separately. A saved answer and a delivered comment are different outcomes.

A current factory administrator can inspect `GET /api/v1/collaboration/receipts?repository_id=REPOSITORY_ID`, or call `ForgeClient.repository_receipts(repository_id)`. Check `status`, `action_status`, `feedback_status` and the associated run rather than treating an accepted receipt as completed execution. Project-reader polling/history routes do not grant access to this global administrative list.

After a timeout, 403, 409 or missing exact configuration/consent receipt, retain the **same request ID and complete input**. Use the exact `configuration` or `binding` read. A missing receipt does not prove that no write occurred and does not authorize a new ID or automatic resend. An administrator can inspect current setup/heads while preserving the original operation.

For feedback, forge records `writing` before the provider request and `sent` only after a confirmed result. A lost reply, interrupted process or late authority/credential refusal can leave `feedback_status: needs_reconciliation`. Repeated ticks do not repost that write. Inspect the provider conversation, then use [explicit feedback reconciliation](repository-workflow.md#receipts-and-feedback) with `posted` or `not_posted` and an observed-outcome note. Reconciliation records `sent` or `not_sent` and sends no remote request. If no reply was posted, a new explicit status comment is the supported next request; replaying the unknown write is not.

Current consent, native permission and provider access are checked before reading private feedback. After token preparation and the final local authority check, a synchronous check rejects any payload containing a currently registered credential before POST. Historical receipts are not rewritten. To revoke consent, submit a new reviewed request with `active: false` and the exact previous digest. Authorized historical reads remain available; the old receipt does not assert that authority is still active.

### Polling limits and acceptance boundaries

| Resource | Limit |
| --- | --- |
| Scan | 50 objects per page, 20 pages and 16 MiB across issue/comment streams |
| Page response | 4 MiB |
| Inbox evidence | 5,000 source identities and 16 MiB |
| Consent history | 100 heads, 50 revisions per head, 5,000 requests, 64 KiB per receipt |
| Retained polling configuration/consent events | Combined 16 MiB budget; 5,000 configuration request identities |

A limit refuses the operation and preserves its cursor/evidence. Known sensitive content is withheld with its digest and source identity and cannot become a command or consent reference. The shared `(repository, provider, source_key)` identity also deduplicates a comment observed through a webhook. Edits retain a source-conflict record instead of creating another action; publish a new comment only after reviewing that conflict.

Complete rescans can observe resources that remain present after pagination shifts. REST polling cannot recover a comment deleted before observation or every intermediate edit. Controlled transport tests and Studio vectors do not establish a live GitHub issue round trip. Verify native operation and live delivery against the actual deployed source and granted repository. Source publication, repository bootstrap and full GitFlow remain separate workflows.
