# Define the product and customer

A brief describes a change. A product definition gives the agent a durable customer contract: who needs it, what problem it solves, how the journey works, and which acceptance and security checks determine success.

Save the definition at `PUT /api/v1/repositories/ID/product`. Every save creates an immutable revision; `GET` returns the latest revision with its digest and creation time.

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

Optional architecture and design settings retain the backend/frontend choice, product brand, tokens, grid and review references. A service-only product can describe API processes without adding a frontend journey.

On a wide screen, the AI assistant opens beside the editor in a collapsible panel. Closing it gives the form the available width and preserves the conversation and selected files. On a narrow screen, **AI assistant** opens a dialog; closing it returns to the same draft.

Choose the next task and a configured provider or model profile, then start a conversation. Follow-up messages and replies stay in that product's recorded conversation.

Use **Ask AI** within a step to prepare a message containing its current, unsaved draft. Review and edit this message before sending. If you already have a message in progress, choose whether to append the step context or replace it. Preparing this context does not invoke a model or save the definition. The assistant's ordinary retained context uses the saved product revision; it cannot see unsaved form edits unless you include them in a message.

For example, ask: “Warehouse operators need to reserve available stock without overselling. Use PyFly and PostgreSQL; this product exposes an API and has no frontend. Map the reservation and rejection paths to acceptance criteria.” The assistant can ask for missing decisions and propose a definition. Its proposal remains unreviewed.

Review the proposal comparison before applying it to the document. If the draft already has edits, replacement requires an explicit acknowledgement; **Keep draft** returns without replacing them. Applying a proposal does not save it or approve its architecture. The source badge records the provider, recorded model, generation time and conversation receipt. **View AI source** opens that conversation. When an applied definition is saved, the server verifies the source receipt and records whether the proposal was edited.

Use Markdown formatting for longer text and switch to **Preview** when checking its rendering. Journeys, owners and acceptance criteria have separate entries. Map each feature to its journeys and expected behavior, including failures. Attach notes, contracts or wireframes through **Files** when needed; [Conversation files](assistant-files.md) explains their scope and model-input limits. Review the functional contract, architecture and attached design references before saving.

API clients create a conversation at `POST /api/v1/repositories/{id}/assistant/conversations` with `{"agent":"bridge"}` or `{"agent":"agentic"}`. Send a turn at `POST .../conversations/{conversation_id}/turns` with a 32-character hexadecimal `request_id` and `message`. Replaying that ID with the same message returns the existing turn; changing the message under that ID is rejected. Inspect a quota-paused or interrupted turn before making a new explicit request. The factory does not repeat it automatically.

## Freeze the run's contract

Repository commands copy the latest saved revision into `RunInput.product_definition` when they create a run. The snapshot includes `repository_id`, `revision`, `definition`, `digest`, and `created_at`. New saves also retain an `origin` record for manual or AI-assisted definitions. Later edits create another revision; they do not change an existing run's input or its audit history.

Studio/API callers can include that returned snapshot in a run request. A normal `brief` without a definition remains supported. A snapshot from another repository or an incomplete revision is rejected. To require the current revision in a repository comment, use:

```text
/forge run {"brief":"Implement the ticket search journey","product_revision":3}
```

The agent receives the frozen definition alongside its brief and execution policies. It must implement the journey, verify acceptance criteria, and report unmet security or nonfunctional requirements with evidence. The definition is product data; it does not grant extra tools, access to secrets, or deployment permission.

## Review an architecture proposal

`POST /api/v1/architecture/propose` accepts `product_definition` plus optional `backend` and `frontend` preferences. The proposal uses supported factory profiles and explicit rules; `method: rule_based` distinguishes it from an AI assessment. PyFly/React is the default. A LaraFly preference uses Laravel/PHP guidance. Alternatives retain their upstream profile references and verification limits.

Review the rationale and proposed domain, application, data, HTTP, operations, and frontend components. Edit responsibilities, decisions, and nonfunctional requirements to fit the customer journey. Set `architecture.reviewed: true` after review, then save that architecture inside the product definition. Saving an unreviewed supplied architecture is rejected. Existing brief-only work can still omit architecture.

The frozen architecture includes selected backend/frontend, components with layer and responsibility, decisions, nonfunctional requirements, and its review state. Frontend guidance calls for atomic design, shared tokens, an owned generated or imported brand kit, and accessible focus, loading, empty, and error states. The proposal identifies module boundaries; it does not establish that the generated implementation or a third-party framework meets your workload.

## From definition to support

Use issues for customer problems and acceptance changes, then use trusted `/forge run`, `/forge test`, or `/forge maintain` comments to request work. The resulting run retains the originating issue and actor, its product revision, verification evidence, isolated branch, and publication result. Queued and completion feedback returns to that conversation through the provider's application grant.

A definition does not turn a passing build into customer acceptance. Review the implemented journey and its evidence before publication or deployment. For a support change, update the contract only when the expected behavior changes; preserve the original revision on previous runs.

See [repository commands and their trust rules](repository-workflow.md), [execution policies](policies.md), and [deployment controls](deployment.md).
