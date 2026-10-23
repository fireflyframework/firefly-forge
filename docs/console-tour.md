---
description: Follow a product definition and execution through the forge console with current desktop and mobile screenshots and explained fixtures.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

# Follow a change through Studio

For the meaning of project, component, campaign and run, start with [concepts and factory journeys](factory-journeys.md). It also explains the repository conversation path and how publication, deployment and support follow execution.

Follow an inventory API change from connection and product definition through run review, verification and recovery. The run case ends with a failed check and an unresolved intent. Separate learning examples review an invalidation and verify an existing indexed source, each with exact-request recovery.

Screenshots use controlled fixtures.[^captures] Select an image to open its full capture; light and dark variants follow the page appearance.

## 1. Connect the factory

Open Administration, then Connections. Connect to your factory API; server sign-in and the coding provider's login are separate.

<figure class="forge-shot" markdown="1">

![Workspace connection](assets/screenshots/connect-workspace-desktop-light.png)

<figcaption>Workspace: this fixture session is connected. Confirm the API connection and signed-in identity on your factory. Continue to Coding AI to inspect the configured provider.</figcaption>
</figure>

<details class="forge-gallery" markdown="1">
<summary>Inspect the remaining connection steps</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot" markdown="1">

![Coding AI readiness](assets/screenshots/connect-coding-ai-desktop-light.png)

<figcaption>Coding AI: the fixture provider is unavailable and no real login has been tested. On your factory host, configure and verify a provider before admitting work.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Repository access](assets/screenshots/connect-repositories-desktop-light.png)

<figcaption>Repositories: grant access through the provider application, or register a local Generic Git repository. This fixture already contains Inventory API; it does not authorize a remote account.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Repository knowledge settings](assets/screenshots/connect-knowledge-desktop-light.png)

<figcaption>Knowledge: inspect the endpoint, required retrieval and automatic memory settings. These fields are configuration; authentication and a scoped cited search establish readiness.</figcaption>
</figure>

</div>
</details>

<details class="forge-gallery" markdown="1">
<summary>Follow connection setup on a phone</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot forge-shot--phone" markdown="1">

![Workspace connection](assets/screenshots/connect-workspace-mobile-light.png)

<figcaption>Workspace: this fixture session is connected. Confirm the API connection and signed-in identity on your factory. Continue to Coding AI to inspect the configured provider.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Coding AI readiness](assets/screenshots/connect-coding-ai-mobile-light.png)

<figcaption>Coding AI: the fixture provider is unavailable and no real login has been tested. On your factory host, configure and verify a provider before admitting work.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Repository access](assets/screenshots/connect-repositories-mobile-light.png)

<figcaption>Repositories: grant access through the provider application, or register a local Generic Git repository. This fixture already contains Inventory API; it does not authorize a remote account.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Repository knowledge settings](assets/screenshots/connect-knowledge-mobile-light.png)

<figcaption>Knowledge: inspect the endpoint, required retrieval and automatic memory settings. These fields are configuration; authentication and a scoped cited search establish readiness.</figcaption>
</figure>

</div>
</details>

Complete the [managed knowledge initialization](knowledge.md#initialize-the-managed-service) and verify a cited search before starting work with required retrieval. A fresh managed installation enables required knowledge and automatic verified memory. The connection capture remains unverified and cannot establish successful retrieval.

Knowledge and memory currently use a separate workspace for each repository: `forge-<repository_id>`. A shared framework workspace can supply common references. A logical ProductProject does not automatically create an aggregated memory scope across its components. See [project scopes](knowledge.md#keep-project-scope-explicit).

Internet search is a separate optional connection under Administration. Follow the [search setup walkthrough](search-providers.md#connect-through-studio) to choose a provider, store its key and restrict public sources. Saving settings does not run a provider test.

## 2. Define the reservation contract

Select Product definition and Inventory API. Describe who reserves stock, what happens when stock is insufficient, and which check proves the rejection. An API-only product still needs a customer or system process, feature behavior and failure paths.

<figure class="forge-shot" markdown="1">

![Product Context step](assets/screenshots/product-context-desktop-light.png)

<figcaption>Context overview: describe the warehouse operators and the over-reservation problem. Scroll the form to enter the Inventory team under Product owners, then continue to Journeys.</figcaption>
</figure>

<details class="forge-gallery" markdown="1">
<summary>Follow the product definition steps</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot" markdown="1">

![Product Journeys step](assets/screenshots/product-journeys-desktop-light.png)

<figcaption>Journeys: record the stock reservation process and its user story. Continue to Features to specify observable behavior.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Product Features step](assets/screenshots/product-features-desktop-light.png)

<figcaption>Features: link Reserve available stock to this journey and the 409 acceptance criterion. The architecture preference is pyfly with no frontend.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Reservation feature name and links](assets/screenshots/product-feature-mapping-desktop-light.png)

<figcaption>Feature mapping detail, naturally scrolled to Reserve available stock and its links. The phone excerpt focuses on these fields; Continue remains later in the form, shown in the preceding overview.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Mapped reservation behavior](assets/screenshots/product-behavior-desktop-light.png)

<figcaption>Features detail, naturally scrolled to the behavior fields: given five units, requesting eight returns 409 without changing stock. Record rejected quantities in the failure paths.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Product Integrations step](assets/screenshots/product-integrations-desktop-light.png)

<figcaption>Integrations: add an owned or third-party system contract when this change needs one. This bounded fixture has none; Review definition opens the complete summary.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Product Review step](assets/screenshots/product-review-desktop-light.png)

<figcaption>Review: inspect the mapped behavior and rule-based architecture before acknowledging them. The fixture saves a draft with reference views still pending; saving preserves that review state and starts no work.</figcaption>
</figure>

</div>
</details>

<details class="forge-gallery" markdown="1">
<summary>Inspect the product editor on a phone</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot forge-shot--phone" markdown="1">

![Product Context step](assets/screenshots/product-context-mobile-light.png)

<figcaption>Context: describe the warehouse operators and the over-reservation problem, then enter the Inventory team under Product owners. The form scrolls above the Continue action.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Product Journeys step](assets/screenshots/product-journeys-mobile-light.png)

<figcaption>Journeys: record the stock reservation process and its user story. Continue to Features to specify observable behavior.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Product Features step](assets/screenshots/product-features-mobile-light.png)

<figcaption>Features: link Reserve available stock to this journey and the 409 acceptance criterion. The architecture preference is pyfly with no frontend.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Reservation feature name and links](assets/screenshots/product-feature-mapping-mobile-light.png)

<figcaption>Feature mapping detail, naturally scrolled to Reserve available stock and its links. The phone excerpt focuses on these fields; Continue remains later in the form, shown in the preceding overview.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Mapped reservation behavior](assets/screenshots/product-behavior-mobile-light.png)

<figcaption>Features detail, naturally scrolled to the behavior fields: given five units, requesting eight returns 409 without changing stock. Record rejected quantities in the failure paths.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Product Integrations step](assets/screenshots/product-integrations-mobile-light.png)

<figcaption>Integrations: add an owned or third-party system contract when this change needs one. This bounded fixture has none; Review definition opens the complete summary.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Product Review step](assets/screenshots/product-review-mobile-light.png)

<figcaption>Review: inspect the mapped behavior and rule-based architecture before acknowledging them. The fixture saves a draft with reference views still pending; saving preserves that review state and starts no work.</figcaption>
</figure>

</div>
</details>

The assistant panel can prepare proposals and retain conversations. This case fills the form manually and uses a rule-based architecture response fixture. Preparing step context, applying a proposal and saving a definition have separate effects; see [product definition](product-definition.md).

The saved draft includes its pending reference review. For a visual product, [review every journey reference](design-review.md) before recording that acknowledgement. A saved revision freezes the agreed inputs for a run; customer acceptance and implementation verification still need their own evidence.

## 3. Review and admit the run

Start from the saved product revision. Keep the brief bounded: implement stock reservation and reject over-reservation. Inspect the foundation and choose checks that exist in the repository. Leave independent review unselected only when that matches the intended review policy.

<figure class="forge-shot" markdown="1">

![Start a run Review step](assets/screenshots/run-review-desktop-light.png)

<figcaption>Review: inspect the instructions, foundation, checks and saved revision. Start building admits work on a real factory; this capture uses an intercepted fixture response.</figcaption>
</figure>

<details class="forge-gallery" markdown="1">
<summary>Follow Brief, Foundation and Checks</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot" markdown="1">

![Start a run Brief step](assets/screenshots/run-brief-desktop-light.png)

<figcaption>Brief: the saved product revision is attached to the selected repository. State the reservation change, then continue to Foundation.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Start a run Foundation step](assets/screenshots/run-foundation-desktop-light.png)

<figcaption>Foundation: the saved architecture fixes pyfly and API only. Check the selected execution agent against the provider configured on your actual host.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Start a run Checks step](assets/screenshots/run-checks-desktop-light.png)

<figcaption>Checks: use the project's actual command. This example declares uv run pytest; the fixture supplies its later result and executes no command.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Independent review choice in Checks](assets/screenshots/run-review-provider-desktop-light.png)

<figcaption>Checks detail, naturally scrolled to the optional Independent review provider field. No independent review is selected in this fixture. Choose a separately configured provider when the review policy requires it.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Fixture policy choices in Checks](assets/screenshots/run-policies-desktop-light.png)

<figcaption>Checks detail, naturally scrolled to a fixture Behavior checks policy option. Override project policies exposes the available choices. This tour returns to inherited project defaults before Review run; core source and credential protections still apply.</figcaption>
</figure>



</div>
</details>

<details class="forge-gallery" markdown="1">
<summary>Review the run on a phone</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot forge-shot--phone" markdown="1">

![Start a run Brief step](assets/screenshots/run-brief-mobile-light.png)

<figcaption>Brief: the saved product revision is attached to the selected repository. State the reservation change, then continue to Foundation.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Start a run Foundation step](assets/screenshots/run-foundation-mobile-light.png)

<figcaption>Foundation: the saved architecture fixes pyfly and API only. Check the selected execution agent against the provider configured on your actual host.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Start a run Checks step](assets/screenshots/run-checks-mobile-light.png)

<figcaption>Checks: use the project's actual command. This example declares uv run pytest; the fixture supplies its later result and executes no command.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Independent review choice in Checks](assets/screenshots/run-review-provider-mobile-light.png)

<figcaption>Checks detail, naturally scrolled to the optional Independent review provider field. No independent review is selected in this fixture. Choose a separately configured provider when the review policy requires it.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Fixture policy choices in Checks](assets/screenshots/run-policies-mobile-light.png)

<figcaption>Checks detail, naturally scrolled to a fixture Behavior checks policy option. Override project policies exposes the available choices. This tour returns to inherited project defaults before Review run; core source and credential protections still apply.</figcaption>
</figure>



<figure class="forge-shot forge-shot--phone" markdown="1">

![Start a run Review step](assets/screenshots/run-review-mobile-light.png)

<figcaption>Review: inspect the instructions, foundation, checks and saved revision. Start building admits work on a real factory; this capture uses an intercepted fixture response.</figcaption>
</figure>

</div>
</details>

On a real factory, Start building admits the request. The retained source, product revision, provider configuration and policies belong to that run. Publication and deployment require their explicit operations; queueing work does not deliver a pull request or a live application.

## 4. Inspect the run before continuing

Choose the **Project** and **Manufacturing run**, then switch between **Floor** and **Board**. Studio keeps these choices when you inspect a run or visit another page and return to Factory. The selection stays in memory for this session; changing the signed-in user or factory API, or disconnecting, resets it. If a selected project or run is no longer in the accessible list, choose from the current records. On phones, each selector uses a full row.

Factory lists the work. Select the inventory run to open its state, timeline and evidence tabs. In this example, the fixture reports `needs_reconciliation`, a failed verification command and an intent without a completion receipt.

<figure class="forge-shot" markdown="1">

![Factory overview and fixture run](assets/screenshots/factory-overview-desktop-light.png)

<figcaption>Factory overview: select Fixture reservation verification to open its retained detail. The camera starts with the whole floor; use Focus selected stage for a closer view and Show full floor to return. Animation does not establish a job outcome.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Changed file with old and new line numbers](assets/screenshots/evidence-changes-desktop-light.png)

<figcaption>Changes detail, scrolled to the patch: inspect src/check.py and its removed and added lines. Copy patch retains the full recorded patch; it does not approve the change.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Failed verification command and its output](assets/screenshots/evidence-verification-desktop-light.png)

<figcaption>Verification detail: the fixture reports exit 2, 1.53 seconds, stdout and stderr. A returned command with a failed exit status does not establish a passing run.</figcaption>
</figure>

<details class="forge-gallery" markdown="1">
<summary>Inspect journal integrity and retained artifacts</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot" markdown="1">

![Execution journal with an unresolved intent](assets/screenshots/evidence-plan-desktop-light.png)

<figcaption>Plan detail, scrolled to Execution journal: sequence, record hash, previous hash and intent key stay visible. The server's chain validation and the missing outcome are separate facts.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Retained artifact metadata](assets/screenshots/evidence-artifacts-desktop-light.png)

<figcaption>Artifacts detail: inspect the reported name and size. This fixture lists changes.zip; it supplies no real downloaded build or deployment evidence.</figcaption>
</figure>

</div>
</details>

<details class="forge-gallery" markdown="1">
<summary>Read run evidence on a phone</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot forge-shot--phone" markdown="1">

![Changed file with old and new line numbers](assets/screenshots/evidence-changes-mobile-light.png)

<figcaption>Changes detail, scrolled to the patch: inspect src/check.py and its removed and added lines. Copy patch retains the full recorded patch; it does not approve the change.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Failed verification command and its output](assets/screenshots/evidence-verification-mobile-light.png)

<figcaption>Verification detail: the fixture reports exit 2, 1.53 seconds, stdout and stderr. A returned command with a failed exit status does not establish a passing run.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Execution journal with an unresolved intent](assets/screenshots/evidence-plan-mobile-light.png)

<figcaption>Plan detail, scrolled to Execution journal: sequence, record hash, previous hash and intent key stay visible. The server's chain validation and the missing outcome are separate facts.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Retained artifact metadata](assets/screenshots/evidence-artifacts-mobile-light.png)

<figcaption>Artifacts detail: inspect the reported name and size. This fixture lists changes.zip; it supplies no real downloaded build or deployment evidence.</figcaption>
</figure>

</div>
</details>

Commands and outputs have bounded previews and full-copy controls. Advanced record retains the original technical payload. In Plan, a completion receipt records an outcome; hash-chain validation checks record integrity. Neither can turn a failed check into a passing build. The journal is an operator-controlled database ledger, with the [integrity limits described in recovery](recovery.md).

## 5. Choose recovery or handoff from the evidence

Inspect the actual workspace or external service for the unresolved effect, then retain its identity and result before repeating an operation. Resume preserves the run and available session. Retry creates a separate run and can repeat earlier effects. Handoff keeps the existing source and freezes a new configured provider snapshot.

<figure class="forge-shot" markdown="1">

![Run state with resume and handoff controls](assets/screenshots/run-context-desktop-light.png)

<figcaption>Run context before the evidence tabs. The fixture shows needs reconciliation and an unavailable handoff provider. Inspect the unresolved intent first; this tour does not press Resume run or Hand off run.</figcaption>
</figure>

<details class="forge-gallery" markdown="1">
<summary>Inspect the continuation controls on a phone</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot forge-shot--phone" markdown="1">

![Run continuation controls on a phone](assets/screenshots/run-context-mobile-light.png)

<figcaption>Continuation controls, naturally scrolled when needed. The enabled or disabled state follows the returned run and provider records; a disabled provider is not proof of a configured alternate worker.</figcaption>
</figure>

</div>
</details>

Follow [recovery and handoff](recovery.md) for the accepted state and request contract. Review the exact diff and passing checks before [publication or deployment](deployment.md). Verified memory is written only after final verification; this failed fixture cannot establish a successful memory write.

## 6. Review saved evidence and its decision

Open **Knowledge > Learning records** and inspect a record in its current scope. A saved verified state does not establish current reuse eligibility. When its evidence no longer applies, a current reviewer can enter a reason, inspect the scoped decision and explicitly confirm invalidation.

<figure class="forge-shot" markdown="1">

![Review a saved evidence invalidation](assets/screenshots/learning-invalidation-review-desktop-light.png)

<figcaption>The separate learning fixture reviews one decision. Its original request identity is retained before the browser sends the intercepted write.</figcaption>
</figure>

If the response is lost, use **Check original request**. A missing event retains uncertainty; it cannot authorize another write. The [illustrated learning guide](learning.md#invalidate-saved-evidence) follows the same request from reason and review through an initial 404 and a later matching receipt, including filter retention and the phone layout. These are controlled fixtures with no external service writes.

## 7. Verify an existing indexed source

For an admitted source whose indexing needs verification, choose **Verify existing source**. Choose **Review source**, check the record and source, then acknowledge the operation before **Verify source**. This reads the existing content and citations and records a local observation; it does not create a source or trigger ingestion.

<figure class="forge-shot" markdown="1">

![Review verification of an existing indexed source](assets/screenshots/learning-observation-review-desktop-light.png)

<figcaption>The separate source fixture reviews one observation. Exact factory, actor and target references remain available in expandable details. Its pending identity is retained before the intercepted request.</figcaption>
</figure>

A lost response leaves the original verification unresolved. **Check original verification** only reads its exact receipt. The [illustrated source-verification guide](learning.md#verify-an-existing-indexed-source) follows one write, an initial 404 and a later matching event. The confirmation distinguishes historical verification from current evidence eligibility and stays open when the record leaves the active filter. These controlled fixtures make no provider or knowledge-service call.

## 8. Review an original queued command

In a run's **Plan** tab, choose **Request continuation** under **Original runner command**. Select the command, review its saved identity and acknowledge the leader's final check. The [illustrated continuation guide](sandbox.md#continue-one-queued-command-after-a-controller-restart) shows both steps on desktop and phone. Its fixtures stop before sending a request. Only the live leader and controller can establish admission and execution.

## 9. Inspect a product campaign

Open **Product projects**, select the product and choose **Load campaigns**. Select a campaign, then **View report**. Compare its recorded status with the retained evidence state. Select a component for its tasks, checks, source and journal references. The [campaign walkthrough](project-campaigns.md#read-a-report-in-studio) shows the summary and component details; its fixtures do not establish a live campaign run.

## 10. Delegate specific source-control requests

Open Administration → Project access, choose the product project, then find **Source-control delegation**. Choose the repository that contains the relevant components. **Read current delegations** loads the server's current head records. The displayed state distinguishes a recorded active setting, revocation, a changed project and a changed application connection. An active setting does not establish current provider grants or the delegated person's permissions.

A factory administrator can select **Review a delegation** and complete three steps:

1. **Account:** choose a retained signed delivery. The factory verified the configured webhook sender's signature; the account was asserted by that sender. This is separate from a personal OAuth login. If no current delivery is available, open Connections and check the application setup before continuing.
2. **Scope:** find an existing registered person, then select components in this repository and the allowed requests: answer an agent question, read run status or request cancellation. Clear the active setting to record revocation. This form creates no user or project membership.
3. **Review:** check the person, component scope and actions, review the application trust, then acknowledge the webhook assertion and select **Save delegation**. The request includes the reviewed project cut and previous revision; the server can refuse a changed cut. Saving does not answer a question, resume a run or cancel work.

Studio retains the request identity and input digest before sending once. It confirms the result through the exact receipt read, including after a successful save response. If delivery is lost, return to the same project and repository and select **Check original delegation**. A receipt records the original administrator and revision; authorized readers can inspect that historical evidence even when the current head is revoked or stale. Later actions still require current native permissions and application access.

A conflict may be refused before a receipt is inserted, leaving the exact read unavailable. Studio preserves that original request and holds further saves. Keep the reference and ask an administrator to check the current project and delegation. A missing receipt, access error or timeout does not authorize a replacement request or an automatic retry. The retained list contains this browser's references, not the complete server history.

The following screenshots use constructed, model-bound fixtures. They demonstrate the Studio forms and exact readback, without establishing a live provider delivery or a provider account grant.

<figure class="forge-shot" markdown="1">

![Select the reported account](assets/screenshots/scm-delegation-account-desktop-light.png)

<figcaption>Choose an account from a retained signed delivery. The configured sender asserted the account; this is separate from personal OAuth.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Choose the person and scope](assets/screenshots/scm-delegation-scope-desktop-light.png)

<figcaption>Find an existing registered person and select repository components and allowed requests. No project membership is created.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Review the delegation](assets/screenshots/scm-delegation-review-desktop-light.png)

<figcaption>Check the person, actions and application trust, then acknowledge the assertion before saving once.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Read the exact receipt](assets/screenshots/scm-delegation-confirmed-desktop-light.png)

<figcaption>The exact request read confirms this recorded revision. Its historical receipt does not establish current permissions or provider grants.</figcaption>
</figure>

<details class="forge-gallery" markdown="1">
<summary>Follow delegation review on a phone</summary>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot forge-shot--phone" markdown="1">

![Select the reported account](assets/screenshots/scm-delegation-account-mobile-light.png)

<figcaption>Choose an account from a retained signed delivery. The configured sender asserted the account; this is separate from personal OAuth.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Choose the person and scope](assets/screenshots/scm-delegation-scope-mobile-light.png)

<figcaption>Find an existing registered person and select repository components and allowed requests. No project membership is created.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Review the delegation](assets/screenshots/scm-delegation-review-mobile-light.png)

<figcaption>Check the person, actions and application trust, then acknowledge the assertion before saving once.</figcaption>
</figure>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Read the exact receipt](assets/screenshots/scm-delegation-confirmed-mobile-light.png)

<figcaption>The exact request read confirms this recorded revision. Its historical receipt does not establish current permissions or provider grants.</figcaption>
</figure>

</div>
</details>

## Review whole-product SCM editing consent

In **Project access**, read the current delegation and its exact revision for the project's product repository. The **Whole-product edit consent** section is separate from answer and resume permissions. A native factory administrator can review the mapped person, every project component and the product repository, then explicitly allow or revoke signed product edits.

Review the whole scope before recording consent once. This grants no membership. Each signed change still needs the mapped person's current read and edit access, a current application grant and the original product revision/digest. A successful product edit clears its functional and design approval, and architecture approval when present; it starts no execution.

Studio retains the original consent request before sending it. Use a browser with Web Locks on HTTPS or localhost. If coordination or storage cannot be checked, Studio holds the write. After a lost response, conflict or reload, use **Check original consent**. A missing exact receipt keeps the original request unresolved; it does not permit another write.

**Read an original signed product-edit result** accepts the original change request reference from the signed command. It reads the immutable result and its recorded delegation. The displayed revision is historical; the current product may have advanced. This read sends no SCM command. The administration flow does not configure provider delivery, create a project or implement GitFlow.

## Review GitHub continuation consent

A GitHub issue answer leaves the run paused. To let a mapped teammate request continuation through issue polling, a factory administrator must record a separate permission for that account and the selected components. The teammate still needs current execute access when the command arrives. This flow applies to an answered clarification on the same run; failed-provider recovery has its own admission.

Open **Project access**, choose the project, then use **Read GitHub accounts**. Select an active account already linked to a registered person through [polling account consent](scm.md#use-github-issue-comments-from-a-local-factory). **Read current GitHub consent** inspects an existing continuation permission. To create or revoke one, choose **Review GitHub continuation**.

1. **Mapping.** Check the GitHub account and its registered person. The account comes from the retained polling binding; the wizard does not create a user or grant membership.
2. **Scope.** Select the components where that person may request continuation. Keep **Allow this person to continue answered work** selected to allow it, or clear it to revoke an existing permission.
3. **Review.** Read the account, components and decision, acknowledge them, then choose **Save permission** once. The browser retains that exact request before sending it. Saving permission does not resume a run.

<figure class="forge-shot" markdown="1">

![Review the mapped GitHub account before choosing its continuation scope.](assets/screenshots/scm-poll-continuation-mapping-desktop-light.png)

<figcaption>Mapping: confirm the existing account and registered person before continuing.</figcaption>
</figure>

<div class="forge-shot-grid" markdown="1">

<figure class="forge-shot forge-shot--phone" markdown="1">

![Select the components allowed for GitHub continuation on a phone.](assets/screenshots/scm-poll-continuation-scope-mobile-light.png)

<figcaption>Scope: select the components and review whether continuation is allowed or revoked.</figcaption>
</figure>

<figure class="forge-shot" markdown="1">

![Acknowledge the reviewed GitHub continuation permission before saving.](assets/screenshots/scm-poll-continuation-review-desktop-light.png)

<figcaption>Review: acknowledge the selected account and scope before Save permission. This records one consent revision.</figcaption>
</figure>

</div>

<figure class="forge-shot forge-shot--phone" markdown="1">

![Inspect the exact continuation consent receipt on a phone in the dark theme.](assets/screenshots/scm-poll-continuation-confirmed-mobile-dark.png)

<figcaption>The receipt confirms the recorded permission revision. It does not prove that a run resumed or completed.</figcaption>
</figure>

If a save response is lost, conflicts or cannot be verified, keep the original request. **Check original consent** reads that request's exact receipt after reload; a missing receipt keeps the write unresolved. **Read current GitHub consent** answers a different question: which consent is current now. Current access loss clears private details while preserving the unresolved request. Use HTTPS or localhost in a browser with Web Locks so tabs can coordinate writes.

After consent is confirmed, read the paused run's [current clarification continuation cut](scm.md#continue-an-answered-clarification-through-polling) and send one new `/forge resume` issue comment with those exact references. The factory rechecks the current account, application and native permissions before queuing the original run. A saved historical consent, a queued run and a finished implementation are separate outcomes.

## Keep captures current

For project tool permissions and provider prerequisites, follow the [external tools walkthrough](external-tools-tour.md).

The [capture inventory](assets/screenshots/walkthrough-manifest.json) records each path, fixture state, theme, actual viewport, image SHA-256, action bounds and frontend source hashes. Each entry retains its producer, shared-helper and browser-configuration hashes with the Studio version. The strict build rejects missing capture inputs, changed image bytes and mixed frontend versions until the complete inventory is refreshed.

From the repository root, with the existing Studio dependencies installed, capture through the isolated fixture browser configuration:

```sh
UPDATE_DOC_SCREENSHOTS=1 FORGE_DOCS_PORT=5227 \
  node studio/node_modules/@playwright/test/cli.js test \
  --config studio/playwright.docs.config.ts
```

The configuration captures the product journey, learning decisions and source recovery, external tool setup, queued-command review, campaign evidence, provider activity and GitHub continuation consent. For complementary views against unchanged frontend and capture inputs, set `CAPTURE_STAGES` to a comma-separated list of state names. Existing captures keep their bytes and recorded provenance. After a frontend, Studio version or capture-input change, refresh all complete journeys.

Choose an unused local port. The configuration starts its own server and refuses to reuse another checkout. Captures wait for fonts, use the configured viewport and check visible wizard actions against the footer and click target. Native scrollbar painting stays enabled. Review the actual images and rendered guides after regeneration. Fixture screenshots remain presentation evidence, even when the browser checks pass.

[^captures]: These screenshots render the Studio source and version recorded in the capture inventory with controlled API fixtures. Provider readiness, product receipts, command results and journal hashes are fixtures. The journey makes no real model, command, remote repository or knowledge-service call. It demonstrates navigation and presentation; browser success does not establish execution, authenticated provider access or customer acceptance. Phone captures use their recorded viewport and remain bounded in the guide. See [capture maintenance and source hashes](#keep-captures-current).
