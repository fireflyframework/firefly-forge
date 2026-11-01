---
description: Define customers, journeys, features and integration contracts, then review architecture and functional acceptance before implementation.
---

# Define the product and customer

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

A brief describes a change. A product definition gives the agent a durable customer contract: who needs it, what problem it solves, how the journey works, and which acceptance and security checks determine success.

Start in Studio or a configured GitHub issue with ordinary language: “@forge, help define a CRM for our support team. Which customer decisions are missing?” The configured coordinator can hand the request to the native ProductAssistant under a separate current model allowance. Its questions and proposed definition stay in the recorded product conversation. You can answer a confirmed pending question later in the same issue without another mention; the server checks its producer, publication and current account access rather than inferring a question from arbitrary prose.

A product reply does not approve or save a definition, create a coding run, run tests or deploy. Review and save the proposal in Studio, then use the available run controls for implementation. If conversation context is ambiguous or an allowance is exhausted, resolve that hold before another model turn or effect. See [natural repository conversations](repository-workflow.md).

For API clients, save the definition at `PUT /api/v1/repositories/ID/product`. Every save creates an immutable revision; `GET` returns the latest revision with its digest and creation time.

```json
{
  "customer": "Customer support teams handling account inquiries",
  "problem": "Agents cannot find previous replies without searching separate tools",
  "journey": ["Open a customer ticket", "Review earlier replies", "Resolve the inquiry"],
  "user_stories": ["As a support agent, I can search tickets for my assigned accounts"],
  "acceptance": ["Search returns only assigned-account tickets", "Replies appear in chronological order"],
  "nonfunctional_requirements": ["Search completes within 300 ms at the agreed workload"],
  "security_requirements": ["Enforce account access on the server", "Do not log ticket contents"],
  "owners": ["support-product-owner"]
}
```

`customer`, `problem`, `journey`, `user_stories`, `acceptance`, and `owners` are required. Nonfunctional and security requirements default to empty lists. Write criteria that a reviewer can verify, including the workload or boundary when it matters. Do not place credentials, customer records, or provider tokens in the definition.

The [Studio walkthrough](console-tour.md#2-define-the-reservation-contract) illustrates the form steps and records the pending review state separately from saving.

## Design with the assistant in Studio

Open **Product definition** and select the product project. Work through five steps:

| Step | Decide |
|---|---|
| Context | Who needs the product, which problem it solves and who owns the decisions. |
| Journeys | The processes customers or systems follow, including failure paths and expected behavior. |
| Features | Which capabilities support those journeys and what proves each one works. |
| Integrations | The owned or third-party systems involved and their exchange and failure contracts. |
| Review | Check the complete definition, architecture and any product-specific design evidence before saving. |

Each step states its required inputs. Back preserves the working draft. Saving creates a product revision; it does not start a coding run.

Studio saves through `PUT /api/v1/repositories/ID/product/conditional`, with the definition and its expected prior revision and digest. A new product uses revision `0` and a null digest. The server checks both values under the repository lock before inserting. A conflicting save is refused. The original `/product` endpoint keeps its existing unconditional behavior for current clients.

A readable authentication, permission, missing-endpoint or validation rejection leaves the fields editable. Business validation and stale-cut refusals from the conditional endpoint carry `code: product_save_not_applied`, emitted only before inserting a revision; an unqualified conflict remains uncertain. A rejected retry does not settle an earlier uncertain request. Studio never retries automatically.

If a response is lost or cannot be read, Studio retains the submitted input and holds further edits from being saved. Navigating away and back does not remove that hold. **Inspect saved definition** performs a GET without repeating the write. An unchanged or unavailable result keeps the hold. A later matching definition confirms the content currently saved; it does not establish which request wrote it. The original conditional request can no longer overwrite that later revision. If the later definition differs, your edits remain held for review before you discard or replace them.

**Retry original conditional save** is an explicit choice. It sends the retained original input and revision condition, including after you type newer edits. Two requests with that same condition can insert at most one revision. Studio does not retry automatically or fall back to the unconditional endpoint. Reloading or signing out clears the input retained in this tab. These conditions protect conditional writes; they do not add an immutable request identity, exact operation-receipt lookup or general idempotency to the older endpoint.

Optional architecture and design settings retain the backend/frontend choice, product brand, tokens, grid and review references. A service-only product can describe API processes without adding a frontend journey.

On a wide screen, the AI assistant opens beside the editor in a collapsible panel. Closing it gives the form the available width and preserves the conversation and selected files. On a narrow screen, **AI assistant** opens a dialog; closing it returns to the same draft.

Use **New conversation** to prepare a draft and choose its task. Type in the message composer, then use **Send** or press Enter; Shift+Enter adds a line. The first send creates the conversation once and submits that message. **Files** can prepare the conversation before you attach a file, without calling the model. Later messages stay in the same recorded conversation, with its original task and model. **History** opens the recorded conversations without changing the product draft.

If conversation creation cannot be confirmed, refresh History and explicitly choose its recorded conversation before sending. An uncertain message keeps its request identity until that exact request appears in the recorded dialogue. Switching history does not clear it or repeat the request.

Use **Ask AI** within a step to prepare a message containing its current, unsaved draft. Review and edit this message before sending. If you already have a message in progress, choose whether to append the step context or replace it. Preparing this context does not invoke a model or save the definition. The assistant's ordinary retained context uses the saved product revision; it cannot see unsaved form edits unless you include them in a message.

For example, ask: “Warehouse operators need to reserve available stock without overselling. Use pyfly and PostgreSQL; this product exposes an API and has no frontend. Map the reservation and rejection paths to acceptance criteria.” The assistant can ask for missing decisions and propose a definition. Its proposal remains unreviewed.

Review the proposal comparison before applying it to the document. If the draft already has edits, replacement requires an explicit acknowledgement; **Keep draft** returns without replacing them. Applying a proposal does not save it or approve its architecture. The source badge records the provider, recorded model, generation time and conversation receipt. **View AI source** opens that conversation. When an applied definition is saved, the server verifies the source receipt and records whether the proposal was edited.

Use Markdown formatting for longer text and switch to **Preview** when checking its rendering. Journeys, owners and acceptance criteria have separate entries. Map each feature to its journeys and expected behavior, including failures. Attach notes, contracts or wireframes through **Files** when needed; [Conversation files](assistant-files.md) explains their scope and model-input limits. Review the functional contract, architecture and attached design references before saving.

API clients create a conversation at `POST /api/v1/repositories/{id}/assistant/conversations` with `{"agent":"bridge"}` or `{"agent":"agentic"}`. Send a turn at `POST .../conversations/{conversation_id}/turns` with a 32-character hexadecimal `request_id` and `message`. Replaying that ID with the same message returns the existing turn; changing the message under that ID is rejected. Inspect a quota-paused or interrupted turn before making a new explicit request. The factory does not repeat it automatically.

After saving a clean AI-assisted definition, the header uses the saved server origin. An `ai_assisted` origin with `edited: true` shows **AI-assisted · human-edited**. A false or missing edit flag shows **AI-assisted definition**; it does not establish that the proposal was unchanged or identify who edited it.

Further typing returns the header to **AI-assisted draft · review needed**. An applied proposal keeps its recorded provider, model, time and source while you edit. **View AI source** opens the conversation and turn belonging to that same displayed origin. The saved editing label is not inferred from local typing or an unsaved proposal.

## Keep and discard a working draft

The form keeps unsaved fields and the current step while you move to another product or Studio page. Returning to that product restores its draft, including an applied AI proposal's source. Drafts are separate for each repository within the current factory connection and signed-in user.

Use **Discard edits** to start again from the saved definition. Review the product name in the confirmation dialog. **Keep editing** or Escape returns to the draft; discarding affects only that product.

Reloading, closing the tab, changing the factory connection or signing out clears working drafts. The browser shows an unload warning if any product has unsaved edits, including when you are on another Studio page. Save a revision before leaving when you need to keep the work.

When Studio reads a newer saved revision, your earlier fields and base revision stay in place and saving is held. Discard the draft when you are ready to load the current saved version. The conditional save checks that same base under the server's repository lock, including concurrent saves the browser has not yet observed. An uncertain submission must be inspected or explicitly retried before its draft can be discarded.

## Freeze the run's contract

Repository commands copy the latest saved revision into `RunInput.product_definition` when they create a run. The snapshot includes `repository_id`, `revision`, `definition`, `digest`, and `created_at`. New saves also retain an `origin` record for manual or AI-assisted definitions. Later edits create another revision; they do not change an existing run's input or its audit history.

Studio/API callers can include that returned snapshot in a run request. A normal `brief` without a definition remains supported. A snapshot from another repository or an incomplete revision is rejected. To require the current revision in a repository comment, use:

```text
/forge run {"brief":"Implement the ticket search journey","product_revision":3}
```

The agent receives the frozen definition alongside its brief and execution policies. It must implement the journey, verify acceptance criteria, and report unmet security or nonfunctional requirements with evidence. The definition is product data; it does not grant extra tools, access to secrets, or deployment permission.

## Review an architecture proposal

`POST /api/v1/architecture/propose` accepts `product_definition` plus optional `backend` and `frontend` preferences. The proposal uses supported factory profiles and explicit rules; `method: rule_based` distinguishes it from an AI assessment. [pyfly](https://fireflyframework.org/frameworks/python/)/React is the default. A [larafly](https://fireflyframework.org/frameworks/php/) preference uses Laravel/PHP guidance. Alternatives retain their upstream profile references and verification limits.

Review the rationale and proposed domain, application, data, HTTP, operations, and frontend components. Edit responsibilities, decisions, and nonfunctional requirements to fit the customer journey. Set `architecture.reviewed: true` after review, then save that architecture inside the product definition. Saving an unreviewed supplied architecture is rejected. Existing brief-only work can still omit architecture.

The frozen architecture includes selected backend/frontend, components with layer and responsibility, decisions, nonfunctional requirements, and its review state. Frontend guidance calls for atomic design, shared tokens, an owned generated or imported brand kit, and accessible focus, loading, empty, and error states. The proposal identifies module boundaries; it does not establish that the generated implementation or a third-party framework meets your workload.

Architecture component, decision and nonfunctional-requirement lists have no fixed item count. Each entry still has its own validation, and the surrounding product definition retains its separate size and collection limits. See [large-product capacity](large-products.md#current-capacity-boundaries) before assuming an architecture description can be admitted or executed as one campaign.

## From definition to support

Use the issue conversation to describe customer problems, answer product questions and discuss acceptance changes. Review the saved definition before starting implementation in Studio. Natural text does not create a new run, start verification or maintenance, or deploy; trusted `/forge run`, `/forge test` and `/forge maintain` comments remain an advanced compatibility interface. A repository-admitted run retains its originating issue and actor, product revision, verification evidence, isolated branch and publication result. Feedback returns through the provider's application grant.

A definition does not turn a passing build into customer acceptance. Review the implemented journey and its evidence before publication or deployment. For a support change, update the contract only when the expected behavior changes; preserve the original revision on previous runs.

See [repository commands and their trust rules](repository-workflow.md), [execution policies](policies.md), and [deployment controls](deployment.md).
