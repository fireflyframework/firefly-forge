---
description: Use repository conversations to define products, request changes and inspect factory results through source-control integrations.
---

# Work from your repository

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

Use ordinary language to discuss your product and inspect linked work. With [GitHub polling and account mapping](scm.md#ask-about-the-current-ticket) configured, write “@forge, how is this going?” or “@forge, help define the customer ticket journey.” forge uses the configured communication profile to understand the message and replies in the same conversation. A confirmed pending question can be answered later in that thread without another mention. Current access and the original question binding still apply.

Natural conversation supports product dialogue, linked status, answers and separately authorized same-run continuation. It does not create a coding run, start tests or maintenance, or deploy an application from free text. Use Studio for those actions. Signed structured commands remain available for advanced integrations on GitHub issues and pull requests, GitLab issues and merge requests, and Bitbucket Cloud pull requests. An accepted request is not evidence that its work passed verification.

Source-control integration has two directions: a signed conversation can request a permitted action, and forge can reply with its retained outcome or publish verified code. This is not continuous mirroring of issue text, product definitions and repository files. Your team still reviews requirements, resolves conflicts and merges pull requests. [Source-control connections](scm.md#understand-the-two-directions) describe the Git boundary.

| Provider | Inbound verification | Supported conversations |
| --- | --- | --- |
| GitHub | HMAC-SHA256 over the original body; `X-Hub-Signature-256` | Issues and pull requests |
| GitLab | Constant-time comparison of `X-Gitlab-Token` | Issues and merge requests |
| Bitbucket Cloud | HMAC-SHA256 over the original body; `X-Hub-Signature` | Pull requests |

<div class="forge-flow forge-flow--dependencies" role="region" aria-label="Signed repository command flow" aria-describedby="repository-command-guide" tabindex="0" markdown="1">

```mermaid
flowchart LR
  C[Signed new comment] --> R[Retained delivery receipt]
  R --> A[Enabled factory action]
```

</div>

<p class="forge-flow-caption" id="repository-command-guide">The signature identifies a delivery; current actor, command and repository checks decide whether its action may run. A receipt can be denied or unresolved. Focus and scroll the diagram on a narrow screen.</p>

Bitbucket Cloud [removed its native issue tracker API in August 2026](https://developer.atlassian.com/cloud/bitbucket/changelog/). forge rejects Bitbucket issue creation and legacy issue events. Use a pull request conversation; a Jira connector is not part of this integration.

## Connect and authorize

1. Connect the repository through [your GitHub App or provider OAuth application](scm.md). forge checks its current grants before remote writes. GitHub issue authoring needs the App's **Issues: write** permission; pull request comments also use **Pull requests: write**. GitLab needs its configured API grant. Bitbucket needs pull request write access.
2. Open **Administration → Repository workflow → Repository commands**, select the registered repository under **Workflow project**, and configure the Trust, Commands, Webhook and Review steps. Enable the workflow and enter exact trusted actor IDs: numeric GitHub/GitLab user IDs or Bitbucket account UUIDs. Names, organization membership, labels, and a comment mentioning forge do not grant execution rights.
3. Save a separate webhook secret. It is write-only; configuration responses show whether it is present. This secret is independent of the forge operator token and provider OAuth/App credentials.
4. Configure the provider webhook to send comment events to `https://YOUR-FACTORY/api/v1/webhooks/PROVIDER/REPOSITORY_ID`. Use `github`, `gitlab`, or `bitbucket` and the forge repository ID. Terminate HTTPS at your organization's gateway. The webhook route verifies provider credentials rather than accepting the operator token.
5. Select the allowed commands, agent, product stack, publish setting, and operator-owned verification commands. Save a [product definition](product-definition.md) when the work needs a customer contract.

GitHub and Bitbucket signatures are verified against raw bytes without JSON reserialization. GitLab's supported secret-token mode requires HTTPS; it does not provide a payload HMAC. GitLab's newer signing-token mode is not supported by this configuration. Consult [GitHub verification](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries), [GitLab webhook headers](https://docs.gitlab.com/user/project/integrations/webhooks/), and [Bitbucket webhook secrets](https://support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/).

### Keep the authorities separate

| Authority | What it controls |
| --- | --- |
| Factory administrator | Provider application settings, shared application onboarding, and the global delivery receipt process/reconciliation routes. |
| Current project/repository permissions | Read repository settings; change collaboration policy with repository `members` permission; author an issue with repository `edit` permission. A reviewed product save also requires `review`. |
| Exact provider actor and allowlists | Which signed comments may use the repository's enabled commands. Configuration actions also require `configuration_actors`; shared ingress rechecks its global actor lists. |
| Provider application grant | Whether forge can currently access the remote repository or write the requested comment, issue or branch. |

A provider user ID is separate from the factory session identity. Granting a comment author permission does not turn that author into a factory administrator or let a command replace operator-owned checks. A factory administrator should complete the full Studio setup and receipt-inspection journey; the repository policy API also enforces the narrower project permissions above.

## Read the bot's updates

The bot answers your current question with the relevant result, checks and next step. It includes product background only when you ask for it or a recorded change or failure makes it relevant. The selected communication model writes the title and explanation, chooses useful headings or bullets, and uses the Humanizer instructions. A separate reviewer checks both factual support and whether the reply is clear and useful; unrelated recaps and repeated caveats need correction.

When a recorded check helps explain a failure, the bot can include its command or an output excerpt in a code block. Those bytes come from the saved check, with privacy filtering; they are not code invented by the writer. Older results stay labeled as older results. If no diagnostic or cause is known, the bot must say what information is missing instead of guessing a faulty line. A status answer does not start another execution.

Use **Plan** and **Verification** for the full run evidence. A failed automation step can coexist with passing checks, and passing checks do not replace the remaining product journeys. Ask a specific follow-up in the same issue when you need the next level of detail.

## First conversation, then reviewed work

1. Finish repository connection, current account mapping and the scoped GitHub polling setup. The execution owner must be running; an API-only process does not process the conversation.
2. From the mapped account, ask “@forge, what is the current product and what still needs a decision?” forge resolves the current issue and linked context. If several questions or runs fit, clarify the choice in ordinary language.
3. Describe the product or answer the assistant's questions in that same thread. ProductAssistant model turns need their separate current allowance. A saved answer remains available after that allowance expires; another model turn or execution effect needs fresh authority.
4. Review the proposed definition in Studio before saving it. Use Studio to create a coding run or request configured checks. Inspect **Plan**, **Changes**, **Verification** and any required independent review before publication.
5. Keep publication disabled for a first test. Enable it only after the provider grant, project release authority and review gates are ready. Merging a pull request remains a provider-side action.

For GitLab, Bitbucket and signed webhook integrations, use the compatibility commands below; the natural GitHub polling adapter does not widen those transports.

<figure class="forge-shot" markdown="1">
![Studio run review with the selected product, saved definition and independent-review setting.](assets/screenshots/run-review-desktop-light.png)
<figcaption>Studio and signed commands use the factory's run machinery. This controlled setup screenshot illustrates reviewing a run before starting it; it is not a real webhook delivery or provider publication.</figcaption>
</figure>

## Compatibility commands for advanced integrations

These explicit commands are a separate integration interface, not the normal GitHub conversation syntax. Post one complete command as the comment, with JSON options on the same line. Edited comments, review annotations and shell command lines do not dispatch work. Ordinary language uses the separately configured GitHub polling conversation path above.

```text
/forge run {"brief":"Add customer ticket search with account-level access checks"}
/forge test
/forge maintain {"brief":"Fix the failed ticket search acceptance case"}
/forge status
/forge status {"run_id":"0123456789abcdef0123456789abcdef"}
```

`run` and `maintain` require a brief. `test` runs the operator-selected verification plan and reports failures. New runs receive the current saved product revision; add `"product_revision":3` to require that exact current revision. If it changed before dispatch, forge denies the command rather than silently using another contract.

Comments cannot supply executable arguments, provider credentials, arbitrary branches, publish flags, or deployment scripts. Agent, stack, verification commands, and publication come from the repository's operator settings. A command from an untrusted actor, another remote repository, or a revoked setting receives a denied receipt and creates no run.

Deployment requires an explicit command, trusted actor, allowed `deploy` command, `deploy_enabled`, and an allowlisted existing target:

```text
/forge deploy {"run_id":"0123456789abcdef0123456789abcdef","target_id":"staging"}
```

The deployment manager still checks repository ownership and the verified source snapshot. The command cannot create a target or bypass its deployment controls. Keep production targets disabled until an operator enables the required deployment policy.

## Receipts and feedback

forge records a delivery digest, provider identity, actor, command, decision, and associated run. GitHub delivery IDs, GitLab retry-stable `webhook-id`/`Idempotency-Key` headers, and Bitbucket request UUIDs prevent duplicate delivery processing. The originating comment ID also prevents a second run when a provider sends a new delivery ID for the same comment. A delivery ID reused for different bytes is rejected.

The inbox and run creation share a PostgreSQL transaction. The lifecycle worker runs under the factory's execution lease, dispatches accepted receipts once, and posts queued, paused or terminal status back to the originating conversation. An API-role process does not start this execution poller. `GET /api/v1/collaboration/receipts?repository_id=ID` exposes receipt state to a current factory administrator; it is not a project-reader history endpoint.

Inspect `status` separately from `feedback_status`. An action can have completed locally while its remote reply is uncertain. The receipt retains provider, actor, originating conversation, run/action references and any remote write intent. **Changes** and **Verification** show the associated run evidence; receipt processing never substitutes for those checks.

<figure class="forge-shot" markdown="1">
![Controlled Changes view showing a retained source patch in an interrupted run.](assets/screenshots/evidence-changes-desktop-light.png)
<figcaption>Inspect the retained patch and run status before resuming or publishing. The interrupted fixture shown here does not establish a successful run or a remote commit.</figcaption>
</figure>

Remote comments and issue creation record a durable write intent before calling the provider. A timeout, interrupted write, or uncertain response produces `feedback_status: needs_reconciliation`. forge does not repost it automatically. Check the provider conversation before taking a new action; do not treat an unknown outcome as a failed write. After checking, a current factory administrator can call `POST /api/v1/collaboration/receipts/ID/reconcile` with `outcome: posted` or `not_posted` and a note. This records the observed result and sends no remote request. If no comment was posted, request a new explicit `/forge status` rather than replaying the uncertain write.

Operators can author GitHub/GitLab issues through `POST /api/v1/repositories/ID/issues` with `title`, `body`, and an optional `idempotency_key`. Reading the same key returns its retained decision, including an uncertain outcome, without creating another remote issue. Keep that identity when checking a lost response. Without a key, identical title/body content shares a digest key. Changing content with the same key is rejected. Provider App/OAuth credentials authorize the outbound call; the forge operator token is never used as a provider credential.

### Choose the right receipt action

| Observation | Explicit next step |
| --- | --- |
| `status: received` | The execution owner may process the retained receipt. The administrator's `/collaboration/receipts/ID/process` route addresses that same receipt. |
| `feedback_status: needs_reconciliation` | Check the original provider conversation, then record `posted` or `not_posted` with a bounded evidence note. Reconciliation makes no remote request. |
| `status: needs_reconciliation` with an uncertain channel action | Check its local product/source/model result and any remote reference, then record `completed` or `not_completed`. This does not retry the action. |
| Run needs reconciliation or publication outcome is unknown | Use the run's own [recovery contract](recovery.md). A conversation receipt cannot clear an unknown execution or publication effect. |

Redelivery of the same originating comment is deduplicated, even with a different delivery ID. A newly posted comment is a new input and may request new work; do not use one as an automatic retry for an uncertain effect. After inspecting a missing reply, a new explicit status comment can request a fresh observation. Provider settings and actor permissions are checked again during dispatch, so an earlier accepted delivery can still be denied after revocation.

These flows are tested with isolated PostgreSQL schemas and provider HTTP transports. They do not claim a live customer repository has granted access or accepted a webhook. Validate those grants and one signed delivery on your deployment before enabling automatic work.

The loopback HTTP lifecycle test also starts the authenticated [pyfly](https://fireflyframework.org/frameworks/python/) API and
provider fixture servers. For each managed provider, a trusted project comment
reaches the inbox and receives one remote reply. A fixture disconnect after
accepting a second reply leaves the receipt uncertain; repeated processing and
operator reconciliation do not repost it. Revoking the fixture's repository
grant prevents the next outbound comment. This demonstrates the local request
and durable-receipt lifecycle, without claiming live provider installation or
permission acceptance.

## Register projects from a shared provider channel

For shared ingress, open **Repository workflow → Application onboarding** as a factory administrator. Configure the connected GitHub App or OAuth application once, then save operator-authenticated `PUT /api/v1/collaboration/bootstrap/PROVIDER`. The setup fields are `enabled`, `auto_register`, `trusted_actors`, `configuration_actors`, `defaults`, and the write-only `webhook_secret`. `configuration_actors` must be a subset of trusted actors. Both `enabled` and `auto_register` start false; enabling onboarding does not by itself authorize registration of new repositories. Defaults use the repository collaboration schema; verification commands, stack, agent, publication and deployment settings remain operator-owned.

Point the provider webhook at `https://YOUR-FACTORY/api/v1/webhooks/PROVIDER`. No forge repository ID is needed in this route. The first trusted structured comment can register its repository only after a fresh application-grant check confirms its exact remote identity and URL. Existing repository settings are preserved. GitHub installation and added-repository events must match the configured App installation; registration uses fresh granted repositories, not a repository list asserted by the payload. [GitHub documents these installation events](https://docs.github.com/en/webhooks/webhook-events-and-payloads).

`GET /api/v1/collaboration/bootstrap/PROVIDER` returns readiness booleans and a relative ingress path. It never returns a webhook secret or invents a publicly reachable URL. Operators provide their deployment's HTTPS address. `GET /api/v1/collaboration/bootstrap/PROVIDER/receipts` returns the latest global receipts, including denied events and the routed repository receipt ID. Dispatch rechecks global actor permissions and current repository grants, so removal after delivery prevents queued work.

## Define and operate a product in the conversation

Enable individual entries in the operator-owned `channel_commands` allowlist. Its default is empty for existing repository configurations; global onboarding defaults allow only `project` in this additional registry. Product saves, brand changes and design previews also require an explicit configuration actor in both repository and global settings when the delivery uses global ingress.

| Command | JSON options and actual action |
| --- | --- |
| `/forge project` | Return the registered repository and current product revision; `operation: repositories` discovers authorized registered repositories; `configure` saves a `project_definition` with optional `project_id`; `status` reads the specified `project_id` |
| `/forge analysis` | `operation: start`, `project_id` and optional `component_ids`; `status` with optional `analysis_id`; or `review` with `analysis_id` and its exact `digest` |
| `/forge assistant` | `operation: start` with optional `project_id`; `status` with optional `conversation_id`; or `turn` with `conversation_id`, a 32-character hexadecimal `request_id` and `message` |
| `/forge roles` | `operation: status`, or `configure` with validated `roles`; optional `project_id` selects logical-product defaults, otherwise repository defaults |
| `/forge product` | `definition`: complete product definition; save an immutable revision after normal architecture/design validation |
| `/forge brand` | `brand`: generated kit inputs or supported token/static SVG import; save the owned project kit |
| `/forge design` | `operation: preview` with optional complete `definition`, or `operation: status` with a saved `digest` |
| `/forge knowledge` | `operation: search` and `query`; `operation: index` with same-repository `run_id` and bounded `paths`; or `operation: remember` with a same-repository `run_id` |
| `/forge applications` | Return configured applications for this repository |
| `/forge maintenance` | `incident_id` and `brief`; queue maintenance for an existing same-repository incident with the operator's verification checks |
| `/forge resume` | `run_id`; use the execution journal's existing resume guards |
| `/forge handoff` | `run_id` and `agent`; hand off a paused/interrupted run through the journal |
| `/forge cancel` | `run_id`; cancel the same-repository run through the engine |
| `/forge publish` | `run_id`; require repository `publish` opt-in and the engine's final verification, source and review gates |

These commands supplement `run`, `test`, `maintain`, `status`, and the explicitly enabled `deploy` action. They accept data fields, not operator settings, shell arguments or provider credentials. Incident maintenance cannot provision a new application or deployment target. Knowledge indexing uses a validated run workspace; file paths are checked by the existing knowledge service. Brand import supports inline bounded JSON and static SVG, not arbitrary external attachment downloads.

Read-only search and status still require a trusted actor and an allowed command. A coding provider, configured application runtime or knowledge integration must actually be available for its action to succeed. Unsupported or uncertain effects remain visible in the receipt; they are not presented as completed capability.

## Publish review references without changing application source

The operator can enable `remote_artifacts` for a repository or its onboarding defaults. The flag is false by default. A permitted configuration actor's brand or design preview command can then publish the generated review files to `forge/references/DIGEST`, a separate deterministic branch. The originating conversation receives that branch link. A design status request only checks an existing reference; it does not create one.

Brand and design files live under `docs/forge-references/DIGEST/brand` or `design`, with a SHA-256 manifest. An assistant turn can publish its complete unreviewed proposal to `docs/product/proposals/TURN_ID.json` and `.md` on the same separate reference branch when `remote_artifacts` is enabled. The files mark the proposal as unreviewed for functional, architecture and design decisions. Publication accepts only generated kit and design reference names, at most 210 source files, 500 KB per file and 10 MB total. Provider reads are streamed with a 1 MB response cap.

The factory checks every expected file hash on an existing branch. A mismatch stops publication; it does not replace those files. The factory does not merge reference branches or alter the default branch. Repository collaborators can still edit branches unless the repository's own protection policy prevents it; subsequent hash checks detect changed reference files.

GitHub uses a repository-scoped **Contents: write** grant, separate from its issue-comment grant, and the [Git tree/reference APIs](https://docs.github.com/en/rest/git/trees). GitLab uses its authorized API grant and [create-only commit actions on a new branch](https://docs.gitlab.com/api/commits/#create-a-commit-with-multiple-files-and-actions). Bitbucket uses its OAuth **repository:write** scope and [branch/source upload APIs](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-source/). Provider-specific contracts are covered by HTTP transport tests; this does not claim your installation has accepted a live reference publication.

The repository receipt records the target branch and manifest before any content write. Ownership, actor roles, command permission and the artifact opt-in are checked before remote effects. A timeout or interruption leaves `status: needs_reconciliation` with the retained reference intent. Processing the same receipt does not replay the action. Check the provider branch, manifest and any local product/brand changes, then record `completed` or `not_completed` through the existing receipt reconciliation route with an evidence note. This records an operator observation and sends no provider request. It does not automatically retry missing files or replay a partial write.

## Answer a factory question from the repository

An issue-bound paused run can post its durable worker request through the same feedback intent used for lifecycle status. Enable `respond` in `channel_commands` and configure the author's native identity and current project execution permission. GitHub polling also requires its explicit current answer consent; status-only consent does not permit an answer. `/forge respond {"run_id":"0123456789abcdef0123456789abcdef","request_id":"request-1","answer":"Support account owners receive alerts."}` records the answer once under that authenticated identity. The run remains paused until an explicitly enabled resume action. An answer does not authorize deployment or publication. See [Factory roles](agent-roles.md) for the typed feedback contract and request budget.

The card supplies the actual question, context, choices and complete enabled command. If repository replies are disabled, it points to **Agent questions** in the authenticated forge portal when a public browser origin is configured. A service restart preserves the pending question without another coding call. Retained screenshots are opened in that protected portal; the bot does not invent image URLs or publish private image tokens. See [the question walkthrough](agent-lifecycle.md#answer-an-agents-question).

## Coordinate a logical product from signed comments

Project discovery returns at most 20 authorized registered repositories and a `next_cursor`. Supply that cursor in the next `project` request with `operation: repositories`. A repository appears only when the connected provider grants its exact remote ID, canonical name and URL, and the signed actor has configuration permission and the `project` command in that repository. The list does not reveal other registered projects.

A project configuration includes the native `ProjectDefinition` fields: name, description, topology, owners, components, and optional canonical `product_repository_id`, dependencies and roles. Component verification commands must remain omitted or empty in the comment. forge copies each component repository's operator-owned checks. Comments cannot replace shell arguments, webhook secrets, model credentials or the global role registry.

Every current project member must use the same connected provider as the signed origin, grant the exact actor and enable the requested action. Mutations, source inspection starts/reviews, repository discovery and model turns also require configuration permission across the graph. Reading older source reports or conversations checks their frozen member graph too. Removing a repository or revoking its grant does not authorize disclosure of its old evidence. A numeric actor ID from GitHub cannot stand in for a GitLab or Bitbucket identity.

Start a conversation with `/forge assistant {"operation":"start","project_id":"PROJECT_ID"}`. The operator's `assistant_agent` setting selects `bridge` or `agentic`; comment authors cannot override it. forge uses the project's canonical product repository after authorizing all members. Starting freezes configuration without invoking a model. A `turn` invokes the configured model and records its response under a caller-supplied request ID. Reusing the same ID and message retrieves the known response. A different message cannot reuse that ID. The reply remains unreviewed and cannot create a run, change the saved product, answer a worker, resume execution, publish code or deploy.

Use `analysis start` to inspect the authorized source components. `analysis review` acknowledges the exact observed-source digest, not functional behavior or a product design. Role configuration changes validated future-run defaults and preserves old run snapshots. Global role instructions and provider settings remain operator-only.

The repository inbox records an action intent before a model request, source inspection or configuration effect. Authorization failure before dispatch records a denial without an action intent. Unknown outcomes retain reconciliation state; the factory does not blindly retry a model turn or provider write. Comments contain a bounded summary, receipt and relevant IDs. Unreviewed status appears before shortened proposal text; an enabled reference publication supplies the complete proposal branch. Without that opt-in, the full result remains available through the authenticated receipt and assistant APIs.

## Register a first product from GitHub

`/forge intake` creates one logical project and its first product definition in
an already provider-verified GitHub repository. Send a complete definition and
one component; forge does not infer a product from an issue title or download
attachments. The result remains a manual, unreviewed definition. It creates no
run, source branch, build, publication or deployment.

A native factory administrator must first opt in to this action for one verified
GitHub account and repository. Existing reply, resume, product-edit and channel
allowlists do not grant intake. The administrator signs in through the existing
factory account and uses the API or Python client; the repository user can then
submit the intake from GitHub without opening Studio.

1. Connect the GitHub App and enable signed repository ingress. Keep the exact
   account in both `trusted_actors` and `configuration_actors`. Shared ingress
   also requires its existing enabled/global actor settings. Repository
   auto-registration remains a separate explicit setting.
2. Send `/forge project` from that account. Inspect the retained repository
   receipt and its non-null `verification`; it must bind the current App,
   installation, repository, signing key and numeric sender ID. A login name,
   URL or screenshot is not a verified identity.
3. As the native administrator, call
   `POST /api/v1/repositories/REPOSITORY/scm-product-intake/consents` with:

   ```json
   {
     "version": 1,
     "request_id": "11111111111111111111111111111111",
     "expected_previous_digest": null,
     "verified_receipt_id": "22222222222222222222222222222222",
     "expected_verified_reference_digest": "SHA256_OF_THE_EXACT_VERIFICATION_OBJECT",
     "active": true,
     "acknowledge_product_intake": true
   }
   ```

   Use your exact receipt ID and its verification digest. The digest is SHA256
   of UTF-8 JSON with sorted keys, compact separators and `ensure_ascii=False`.
   The server binds the consenting native account; the body cannot choose a
   different native user. For a revision, supply the exact previous consent
   digest. Consent history is bounded to 32 changes per repository, with the
   final slot reserved for revocation.
4. Retain the returned consent request ID and digest. Post this structured
   comment in the same granted GitHub repository, replacing both consent values
   with that receipt:

   ```text
   /forge intake {"version":1,"request_id":"33333333333333333333333333333333","expected_consent_request_id":"11111111111111111111111111111111","expected_consent_digest":"CONSENT_DIGEST","name":"Stock","description":"Track stock movements","component":{"id":"app","name":"Stock API","responsibility":"Track stock","backend":"pyfly","frontend":"none"},"definition":{"customer":"Warehouse operator","problem":"Stock counts drift","journey":["Count stock"],"user_stories":["As an operator, I count stock"],"acceptance":["The count is retained"],"owners":["Warehouse team"]}}
   ```

The execution owner processes the signed delivery and retains the new project,
product revision, owner membership and original action receipt together. Its
ordinary reply uses the existing conversation feedback custody. Registration
and a successful reply are separate outcomes. The native identity must remain
enabled and a current factory administrator; the App grant, repository metadata,
channel actor settings, consent and signing key are checked again before intake
commits. Receipt metadata never turns snapshot IDs into code verification.

Keep the original intake request ID, body and consent references when checking a
lost response. An identical admitted request returns its original project and
product receipt, including when another delivery carries it. A changed body or
an existing project/product is refused. Do not invent a new request ID to clear
an uncertain result or repost an uncertain bot reply. A current native
administrator can read the exact result through
`GET /api/v1/repositories/REPOSITORY/scm-product-intake/requests/REQUEST`.
The Python client methods are `configure_scm_product_intake_consent`,
`scm_product_intake_consent` and `scm_product_intake`.

To revoke intake, send a new consent request with `active: false`, the exact
previous digest and original verified source. Revocation is local and does not
require a reachable provider. It does not erase a completed project or receipt.
This slice has no GitLab/Bitbucket intake adapter, free-form model intake,
multi-repository intake, attachment retrieval or execution permission.

## Automatic conversation updates

After an authorized repository command binds a run to an issue or pull-request
conversation, forge posts concise Markdown cards for the admitted request,
meaningful native execution stages, a recorded worker decision, and the final
verification or publication outcome. An authorized `/forge status` request can
establish this binding for an existing run in its permitted project. A URL in a
brief does not establish a binding. The bot does not discover an issue from a
brief or start work merely because an issue exists.

Updates use the native run, worker-request, verification and publication records.
They do not interpret streamed model text as activity or completion. Packaged
Agentic YAML templates render the cards deterministically; writing a status
update does not call a model. Cards omit tool payloads, commands, private paths,
environment values and arbitrary action JSON. Questions are bounded and escaped,
and configured credentials are refused before delivery. Review links must belong
to the connected provider host and the exact repository.

The latest retained command receipt follows future milestones for each run and
conversation. Earlier explicit status replies remain recorded, but do not each
create another progress stream. A subscription permits at most 32 confirmed
posts, with the last two positions reserved for execution completion and a later
publication outcome. Repeated polls and token output do not create posts. A new
explicit status request can establish a new subscription. The peer lookup holds
when a conversation has more than 200 retained receipts; it does not guess which
receipt is current.

A valid native provider recovery adds its request ID and reserved implementation
attempt to the milestone. Two failed recoveries of the same run remain distinct
even when polling misses the queued and running states between them. The card
identifies the recovery attempt and the recorded attempts behind any retained
check totals. Checks from another attempt, or checks without complete attempt
provenance, do not verify the recovery. Runs without a valid provider recovery
keep their existing milestone fingerprints, so this change does not repost
unchanged legacy runs. Updates still depend on polling; every transient stage is
not guaranteed to be observed or delivered.

A typed automatic-verification handoff is shown as factory verification pending,
running, completed or held. Pending and running system work does not ask an owner
to run commands. Completed means command evidence was recorded, while held
requires operator attention. Legacy worker questions still require an authorized
answer. An answer, a successful check, a published pull request, product acceptance
and merge approval remain separate decisions.

Before a post, forge rechecks current repository, actor, project and application
permissions using the existing source-control authority. It records the exact
milestone and body digest before the provider request, then retains the confirmed
remote reference. An interrupted or uncertain write remains held for explicit
reconciliation. A newer command cannot bypass an unresolved write in the same
conversation. Confirming that a comment was posted acknowledges that exact
retained milestone; it does not post it again. See [receipts and feedback](#receipts-and-feedback)
for the operator recovery procedure.

## Human wording and recorded status

Issue and pull-request updates can use an explicit [communication model profile](model-profiles.md#write-project-updates-with-ai). The AI writes a short introduction from the recorded state; the native card keeps the run reference, checks, question and delivery evidence. A previous attempt's checks stay labeled as retained results. AI wording does not grant permission, answer a question or turn a provider claim into product acceptance.

With no communication profile, or when its provider is unavailable, forge sends its native update. The receipt identifies which path was used. Existing transport consent and current project access still apply before any comment is written. A missing provider response leaves the original delivery held rather than generating another update.

Human answers and continuation are separate operations. The existing answer command records the authorized person's response and keeps the run paused. Resume requires its own authorized action. Expanded decision cards, protected screenshot previews and a Studio profile selector are being delivered separately; selecting an AI profile alone does not add those interfaces.
