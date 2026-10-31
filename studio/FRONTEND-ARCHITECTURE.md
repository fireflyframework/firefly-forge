# Studio frontend foundation

Studio preserves Firefly's Manrope typography, semantic theme variables and native accessible controls. Its foundation composes real forms and conversations without introducing another UI library or replacing the application shell.

## Responsibilities

| Layer | Implementation | Responsibility |
| --- | --- | --- |
| Tokens | `FormFoundation.css` and existing theme variables | Share spacing, color, typography and state contrast. |
| Atom | `StepIndicator` | Keep the ordinal in every state. A separate check indicates validated completion; a visited step does not claim validation. |
| Molecule | `FormField` | Associate native controls, labels and hints while preserving input types, existing descriptions and browser behavior. |
| Molecule | `ProgressHeader` | Compose a step title, visible description and contextual action. |
| Molecule | `ScrollRegion` | Leave page fields in natural flow. Only bounded dialogs receive a named, keyboard reachable field viewport and remaining-content cue. |
| Layout | `.form-stack`, `.form-grid`, `.form-divider` | Compose groups, responsive columns and action separators with shared spacing. |
| Composite | `Wizard` and `Wizard.css` | Compose progress, heading, fields and actions. Its explicit rail variant provides settings navigation; its ordinary variant serves product documents and existing workflows. |
| Workspace | `ProductWorkspace` | Allocate the product document and independent chat panel without measuring their height in JavaScript. |
| Conversation | `ProductAssistant` and `ProductAssistant.css` | Own conversation state, history, transcript, attachments and composer. Product authority and native request contracts remain separate. |

## Connections

Connections uses one continuous theme surface and the existing main page scroller. A compact page heading introduces a 208px desktop step rail, a vertical divider and the full remaining content column. The rail retains numbers, labels and short descriptions. Below 760px, a native labeled step selector replaces the rail. Future steps remain unavailable and editing locks step navigation.

Provider rows and editing are separate states. Editing replaces the rows in the main form area. Fields use two equal columns with 22px row gaps and 20px column gaps, becoming one column below 700px. Endpoint and secret fields span both columns. No inner card or measured field viewport constrains the form.

Save and Cancel form the only action row while editing; the outer Wizard actions are absent from presentation and keyboard navigation. A failed save keeps the editor and input intact. Save or Cancel returns the provider summary and normal Wizard navigation. A step or editor transition focuses its heading and returns the actual page to a complete section boundary, without leaving a partially hidden page heading.

## Product workspace

On desktop widths of at least 1100px and heights above 680px, CSS allocates a full available workspace after any actual notices. The header occupies an automatic row. The next row contains a document column of approximately two thirds and a chat column of one third, separated by a single line and gutter.

The entire product document owns its scrollbar. Its progress, instructions, fields and actions form one natural document; the Wizard and its fields have no inner scrolling viewport. The chat has a separate transcript and reserved composer. Collapsing it releases document space and retains the conversation draft. The product selector remains secondary to the page title.

Compact layouts return the product document to the main page flow and use the existing viewport bounded chat drawer. The shell footer stays outside content scrolling. Desktop uses exactly three shell rows: header, content and footer. No global scroll-status row changes the available space or creates a resize feedback loop.

Step instructions and required inputs remain visible. Redundant explanations are consolidated in source for each step rather than hidden through generic CSS.

## Bounded dialogs

A bounded native dialog reserves heading and action space and scrolls its named field region. That region becomes keyboard focusable when it overflows. Short-height rules preserve this contract; they cannot turn its fields into unbounded page content. This is distinct from scrolling a whole product document or a Connections page.

## Verification

Unit coverage checks native label and hint bindings, numeric progress, completion semantics and rail access rules. Browser coverage checks Save/Cancel failures, step focus, mobile selection, natural field heights, whole-document scrolling, bounded-dialog actions, assistant modes and stable component selection in a 5,000-component project.

Visual fixtures mirror the real provider schema and use a clearly synthetic ready host for the principal layout. A separate unhealthy-host case retains the actual warning and scroll behavior. Entry screenshots record the real opening state without changing scroll in the producer; additional action images show content reached through native focus. These images demonstrate interface behavior, not Original execution or product acceptance. Canonical documentation retains the existing source, helper, producer, version and image hash guards.
