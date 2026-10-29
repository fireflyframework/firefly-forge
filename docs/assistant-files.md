---
description: Attach documents and images to product conversations and retain source receipts, limits and explicit provider image capabilities.
---

# Use files in an AI conversation

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

Attach customer notes, integration contracts or a wireframe when discussing a product with the assistant. Files belong to the conversation where you upload them. Each model turn records the exact selected files and their hashes alongside its response.

## Upload, select and send

1. Open the product assistant and choose the conversation you want to work in.
2. Choose **Files**, then select or drop your documents and images.
3. Review the filename, format and size. Choose **Upload and attach** to save the files to that conversation.
4. Check the selected files beside the message composer. Remove a selection if it should not be used for the next response.
5. Write what you want the assistant to do with the files, then choose **Send**.

Uploading a file does not call a model. Sending a message does. The assistant can propose journeys, features, architecture or design changes; its response still needs your review before you apply a product definition or start development.

Saved files remain available when you reopen the conversation. Detaching a file changes the next turn's selection and preserves earlier responses and their evidence. Uploading to a conversation does not index the file in Flycanon or make it available to other projects. Knowledge indexing is a separate action described in [Cited knowledge and memory](knowledge.md).

## Supported files and limits

| Content | Supported formats | Processing |
|---|---|---|
| Notes and structured text | UTF-8 TXT, Markdown, JSON, YAML and CSV | Readable text becomes model context. |
| Documents | Unencrypted PDF and DOCX | Text is extracted in a separate, bounded parser process. Scanned PDFs need OCR before upload. |
| Images | PNG, JPEG and WebP | A single-frame image is decoded, orientation corrected and metadata removed before model use. |

Each original file must be nonempty and at most **1,000,000 bytes**. A conversation stores at most twenty files. Select up to eight files per turn, including at most five images. Document extraction retains up to 30,000 characters per file and marks truncation in the receipt; selected document text must total at most 60,000 characters. The complete conversation context also has a 100 KB limit.

PDFs may contain at most forty pages. DOCX extraction has archive limits and rejects macros, encryption and external entities. Images may contain at most four million pixels; the canonical decoded image must also fit the file limit. A filename cannot contain a path or control characters.

## Before sending images

The selected provider must have a verified image-input capability for its current configuration. Text readiness alone is insufficient. An unverified image selection is rejected before a model turn is admitted; the uploaded files remain saved so you can change the selection or verify the provider.

Image bytes are passed as multimodal content to the model. Their receipts record the original and canonical-image hashes. The transcript displays file metadata and citations; it does not embed private binary content in its JSON response.

## Recover an interrupted upload or response

If an upload loses its reply, refresh the saved-file list before retrying. An exact retry uses the original request ID, filename and content and returns the same receipt. Changing the filename or bytes under that request ID is rejected. The UI keeps the pending file available for an explicit retry.

For a model response with an uncertain outcome, inspect the retained turn and its journal before sending again. Reusing the request ID requires the same message and file selection. forge does not automatically repeat an uncertain model request.

Each active turn has a persisted owner, an ownership epoch and a renewable lease measured against PostgreSQL's clock. Reading it from another API instance leaves the active request alone. If the lease expires, an authorized editor can explicitly recover that epoch for reconciliation. Recovery fences late replies and records the observed expiry; it does not repeat the model call. See [Conversation recovery](recovery.md#recover-a-product-conversation).

## Response duration

New conversations freeze a versioned work-duration policy. The default has no model-work deadline. To choose one, include `work_duration: {"protocol": "forge-work-duration-v2", "timeout_seconds": 900}` when creating the conversation. A positive integer selects a deadline; `null` leaves it unlimited. The saved policy and creation receipt must agree before another turn can start. Changing repository settings does not change an existing conversation's policy.

The owner keeps renewing its finite security lease and checking the editor's current permission while a new-policy response runs. Revocation or ownership loss stops the wait and retains an uncertain result for reconciliation. These checks do not impose a maximum response duration. New-policy requests do not automatically retry an uncertain model submission. Their owned model clients are closed after completion, failure or cancellation; if cleanup cannot be confirmed within its separate control budget, further sends are disabled and the result remains uncertain. A late cleanup attempt does not prove that a remote job stopped.

Retained conversations without the policy marker keep the original model deadline, including the 300-second maximum. The Python SDK reads the frozen conversation policy before sending a turn: new-policy conversations have no fixed HTTP response-wait limit, while unmarked conversations retain the original 360-second SDK wait. Bridge-backed new conversations require Bridge's advertised versioned duration capability; an older Bridge is refused before a completion is submitted.

## HTTP contract

All paths below begin with `/api/v1/repositories/{repository_id}/assistant/conversations/{conversation_id}` and require the caller's current conversation and project permissions.

| Method and path | Result |
|---|---|
| `POST /attachments` | Admit `{request_id, filename, content_base64}` and return a ready receipt. |
| `GET /attachments` | List saved-file metadata without loading binary content. |
| `GET /attachments/{attachment_id}` | Read one file's metadata within this conversation. |
| `GET /attachments/{attachment_id}/content` | Download the original bytes as an attachment. |
| `GET /capabilities` | Read image-input capability for this conversation's frozen provider; no model request. |
| `POST /turns` | Send `{request_id, message, attachment_ids}` using saved file IDs. |
| `POST /turns/{turn_id}/recover` | Explicitly fence an expired turn using `{request_id, expected_epoch, legacy_confirmed_stopped}`. |

Omitting `attachment_ids`, or passing `null`, carries the previous turn's selection forward. An empty list detaches all files for the new turn. Explicit IDs replace that selection. Foreign-conversation IDs, duplicate IDs, changed upload replays and unsupported content are rejected.

See [CLI, SDK and MCP clients](agent-integrations.md) for the available client interfaces and [Security](security.md) for identity and project access controls.
