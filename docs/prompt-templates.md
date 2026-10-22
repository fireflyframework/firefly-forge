---
description: Maintain the versioned YAML instructions rendered by native Firefly Agentic templates.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Maintain model instructions

forge's built-in model instructions live in `src/firefly_forge/resources/prompts/v1/`. Python selects a known template and supplies already validated text. Firefly Agentic 26.10.1 loads the YAML, registers its exact version and renders it with its native Jinja2 implementation.

The resources cover implementation profiles, default roles and skills, product dialogue, independent review, repair, task guidance and model-facing tool descriptions. The catalog contains 286 templates, including 162 factory MCP descriptions. The other resources cover model instructions, opt-in execution evidence, the scoped configured-check tool, factory verification and source-control feedback. The seven community adaptations remain in their existing licensed JSON catalog. Runtime commands, SQL, general UI labels, provider credentials and user messages are not template source.

## Find the right resource

| Directory | Instructions |
| --- | --- |
| `implementation/` | Framework profiles, implementation context, repair, retained continuation and factory verification feedback |
| `collaboration/` | Source-control replies and proactive milestone feedback from native run facts |
| `assistant/` | Tool-free product dialogue and frozen instruction skills |
| `assurance/` | Independent review of an immutable source copy |
| `roles/`, `skills/` | First-party defaults and composition framing |
| `tasks/` | Preparation, verification, checkpoint, review and effect reconciliation guidance |
| `tools/` | Granted workspace tool descriptions and untrusted evidence framing |
| `mcp/` | Factory MCP descriptions exposed to external coding agents |

`manifest.json` lists each ID, version, relative YAML path, source SHA256, native output field and string-variable contract. Forge checks that manifest before registering or rendering a resource. The catalog digest identifies those packaged bytes; it is not a receipt proving which prompt a historical model call received.

`implementation/verification-contract` gives every initial and ordinary resumed implementer the frozen command argument arrays and timeouts, separate policy checks and existing final-verification ceiling. It describes requirements without granting execution, tools, credentials or network access. Empty command lists retain the existing project-detection behavior after implementation. Exact checkpoint restoration preserves its original bound instructions. The separately granted configured-check channel keeps its own authority and limits.

`implementation/automatic-verification` describes the typed capability and its finite continuation allowance. `implementation/verification-result` projects known command receipts into bounded factory feedback and marks execution output as untrusted data. The deterministic `collaboration/` resources format recorded actions, questions, checks, recovery states and publication references. When a communication profile is selected, the separately versioned system instructions and complete-card resources format the AI-authored title, explanation and exact human-decision quotation. The pinned Humanizer adaptation is injected through the existing skill catalog. Server-owned reference controls stay separate from generated prose. Provider recovery cards distinguish current checks from saved results. These resources preserve existing factory MCP wire contracts and transport-bound consent.

## Edit a template

`collaboration/conversation-status` holds the explicit recorded-mode English and Spanish status-context copy. `collaboration/conversation-record` composes its native record disclosure. With a communication profile selected, system version `1.0.4` and `collaboration/communication-language` provide controlled English or Spanish instructions for every ticket selection, including missing or ambiguous links. Review-system version `1.0.1` applies the semantic classification to both languages. These resources cannot select a run or authorize a remote write.

A resource uses the native Agentic field names:

```yaml
# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
name: implementation/request
version: 1.0.0
description: Forge product brief framing.
system_template: ''
user_template: |-
  PRODUCT BRIEF:
  {{ brief }}
required_variables:
  - brief
metadata:
  defaults: {}
```

Keep whitespace deliberate: paragraphs, leading newlines and the final newline affect the rendered text. Each resource has one consumed native output field; its other field must be empty. That field does not choose a model-message priority. Existing call sites retain their system/instructions/user placement and ordering.

Only plain `{{ variable }}` insertion is supported. Every used variable must be declared in the manifest and either required or given an explicit string default in `metadata.defaults`. Extra arguments, missing values, non-string values, undeclared names, includes, calls, filters and template control flow are refused. Domain decisions remain in typed Python code. User briefs, role overrides, answers and retrieved context are inserted once as opaque text: `{{ ... }}` inside a value stays literal.

For an intentional new instruction version, update the resource version and matching manifest entry, then select that exact version at the caller. Registering a newer version does not silently change a caller using `1.0.0`. Preserve retained role, skill, plan and WAL records; do not rewrite their text or digests.

After editing a resource, update only its manifest SHA256 using the actual file bytes. For example, from the repository root:

```python
import hashlib
import json
from pathlib import Path

root = Path('src/firefly_forge/resources/prompts/v1')
manifest_path = root / 'manifest.json'
manifest = json.loads(manifest_path.read_text())
entry = next(item for item in manifest['templates'] if item['id'] == 'implementation/request')
entry['sha256'] = hashlib.sha256((root / entry['path']).read_bytes()).hexdigest()
manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
```

Run `uv run pytest tests/test_prompt_templates.py tests/test_mcp_prompt_templates.py` and the configured Python checks before submitting the change. The golden vectors deliberately detect instruction changes; update expected text only as part of a reviewed behavior change. The tests also inspect actual inputs supplied to local FunctionModel transports, including tool schemas and message order. The MCP golden checks also compare the actual serialized tool metadata and preserve callback code independently of the removed docstrings. MCP descriptions have no variables and live only in YAML; decorators select their packaged IDs. These checks do not call a model provider or establish model-output quality.

## Package and authority boundaries

Templates are package data in both wheel and source distributions. Loading uses `importlib.resources`, including zip-imported wheels, and the native `PromptLoader`, `PromptTemplate` and a private `PromptRegistry`. Forge adds strict catalog validation because the pinned native registry allows replacement and its renderer accepts extra arguments by default.

Each YAML resource is limited to 64 KiB. The manifest has its own 256 KiB read limit, and the complete catalog is limited to 512 entries and 1 MiB total. Duplicate identities or paths, traversal, resource symlinks, digest mismatches and unsupported YAML declarations fail before rendering. Rebuild a distribution after changing resources; there is no live template-editing endpoint or automatic registry reload.

Instructions confer no authority. Existing project access, tool grants, credential admission, cancellation, verification and publication guards still decide what may happen. The catalog does not publish knowledge automatically or add a new invocation receipt. Bridge's runtime prompts and operator workflow DSL are a separate compatibility boundary.


The complete SCM message writer uses `collaboration/communication-system` version `1.0.3`. Its independent classifier uses `collaboration/communication-review-system` and `collaboration/communication-review-request`. These YAML resources keep instructions and candidate/fact inputs separate. The structured review classifies meaning, including grounded denials, rather than enforcing a word list. Both calls share a finite profile deadline; a rejected or uncertain classification feeds one final correction through `collaboration/communication-repair-request`. The corrected AI response then proceeds through native framing and current-authority checks; there is no repeated semantic veto.
