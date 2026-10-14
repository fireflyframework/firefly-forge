---
description: Review wireframes by customer journey and retain approved design references, states and acceptance criteria for implementation.
---

# Review customer journey wireframes

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

Generate references before implementation so customers and developers can check each journey, its acceptance criteria, and the shared layout. forge renders a deterministic wireframe from the current product definition and project brand kit. The result is a review template, not an AI-generated finished product or proof of final visual acceptance.

The product wizard shows every journey at desktop and mobile sizes in light and dark themes. Review all four frames for every screen before acknowledging the bundle. If the customer definition, reviewed architecture, or project brand changes, generate and review a new bundle.

## Create a saved preview

With the operator's authenticated API session, send the same complete definition that will be saved:

```http
POST /api/v1/repositories/{repository_id}/design/preview
Content-Type: application/json
```

The body is `{"product_definition": {...}}`, using the [product definition](product-definition.md) schema. The response includes the repository ID, `digest`, `definition_digest`, optional `brand_digest`, `method: "wireframe"`, `font_family`, requirements, and coverage counts.

Each screen includes a stable `journey-01` style ID, its customer journey title, an interaction contract, and four static SVG fields:

| Field | Reference size and theme |
| --- | --- |
| `desktop_svg` | 1440 × 900, light |
| `desktop_dark_svg` | 1440 × 900, dark |
| `mobile_svg` | 390 × 844, light |
| `mobile_dark_svg` | 390 × 844, dark |

The interaction contract links the journey to acceptance criterion IDs and names default, loading, empty, and error states. A frame displays bounded excerpts; the bundle retains the full acceptance, user stories, security requirements, and nonfunctional requirements for implementation.

## Save the reviewed definition

Add this metadata to the unchanged definition, using the actual returned digest and every returned screen ID:

```json
{
  "design_review": {
    "digest": "<returned 64-character digest>",
    "reviewed": true,
    "screen_ids": ["journey-01", "journey-02"],
    "viewports": ["desktop", "mobile"],
    "themes": ["light", "dark"]
  }
}
```

This is an excerpt to merge into the full product definition, not a standalone product save request. Save through `PUT /api/v1/repositories/{repository_id}/product`. forge checks that the immutable saved bundle belongs to that repository, matches the definition and current brand digest, and includes all screens, viewports, and themes. Invented digests, missing references, duplicate screen attestations, and stale reviews are rejected.

Existing brief-only workflows can omit `design_review`. The product wizard requires the reference acknowledgement before its final product save. Acknowledgement records the operator's review; it does not replace accessibility testing or a customer's acceptance of the implemented application.

## Inspect and implement the references

Retrieve a saved bundle with authenticated `GET /api/v1/repositories/{repository_id}/design/{digest}`. Bundles are private, content-addressed, and bounded to 2 MB, with at most 50 journeys. There is no arbitrary host-file lookup endpoint.

A queued run freezes the validated bundle with its product and brand context. The implementation workspace receives controlled files under `docs/design/reference-journey-01-desktop-light.svg` and corresponding variants, plus `docs/design/interaction-contract.json`. Coding instructions reference these files instead of putting the complete SVG content into the prompt. Later product or brand edits cannot silently replace a queued run's reviewed references.

Use the owned project tokens, logo, and font family in the implementation. If no project kit exists, the template uses the shared Manrope and amber defaults. Static SVG escaping prevents customer labels from becoming active code. Imported logos use the same bounded static-vector validation as the [project brand kit](product-design.md).

Inspect the actual application separately at wide and narrow widths, both themes, keyboard focus, zoom, and the customer journey's loading, empty, and error states. Record screenshots, meaningful behavioral tests, and unmet acceptance criteria with the run. A reviewed wireframe gives the implementation a reference; only the implemented application can demonstrate that the customer flow works.

## Review the grid before the journeys

The product definition carries a typed, editable `visual_design` contract. Each product saves its own settings; references freeze those values together with the product's brand digest. Logical project components may inherit their project's canonical product definition. Factory interface settings do not change customer products.

Defaults use a 4 px spacing baseline, 12 desktop columns with 24 px gutters and 32 px outer margins, and four mobile columns with 16 px gutters and 20 px margins. The desktop shell defaults to a 76 px header, 248 px rail and 64 px footer. Reference canvases default to 1440 × 900 and 390 × 844 px. The preview is a review template for the customer product; it does not reproduce the forge portal or claim to be a finished interface.

| Product setting | Supported values |
| --- | --- |
| Spacing baseline | 2, 4 or 8 px |
| Desktop canvas | Width 1024–1920 px; height 900–1600 px |
| Desktop grid | 6–16 columns; gutter 0–48 px; margin 12–64 px |
| Mobile canvas | Width 320–600 px; height 844–1400 px |
| Mobile grid | 2–6 columns; gutter 0–32 px; margin 12–40 px |
| Header | 0 to omit, or 48–128 px |
| Desktop rail | 0 to omit, or 160–360 px |
| Footer | 0 to omit, or 40–100 px |
| Maximum content width | 640–1920 px |

Validation rejects combinations that leave less than 600 px of desktop content or 240 px of mobile content, columns narrower than 36 px on desktop or 24 px on mobile, or less than 740/680 px of usable desktop/mobile height. A taller header or footer may therefore require a taller canvas. The content region is centered when the maximum width limits it. Two-column desktop panels span the saved grid; gutters, insets and optional shell regions control the rendered SVG geometry.

The product's generated or imported brand kit supplies typography and light/dark colors. Manrope references embed the licensed font when materialized; other named font families require the operator or consumer environment to provide that font. The renderer never downloads fonts from arbitrary URLs. Atomic layers, required interaction states and accessibility policies remain safety defaults, while `guidance` accepts additional product requirements.

Actual settings are retained in `bundle.requirements.grid`, `docs/design/layout-contract.json`, and `docs/design/grid.svg`. Changing product geometry, guidance or branding invalidates an earlier preview digest and requires a new review. Earlier frozen runs retain their original reference files.

## Define an API-only service process

For a reviewed architecture with `frontend: none`, preview returns `method: process_map`. Desktop and mobile are readable document canvases, not proposed application screens. The diagrams show the declared actor, trigger, ordered service steps, ownership, outputs, acceptance references and failure paths. A mobile diagram is an overview; `functional-contract.json` and `processes.md` retain the full process, input/output and security contracts.

Add explicit `processes` to the product definition. For example, merge this excerpt into a definition whose journey is `Resolve ticket` and whose first acceptance criterion requires account authorization:

```json
{
  "processes": [{
    "id": "resolve",
    "name": "Resolve assigned ticket",
    "actor": "Support agent",
    "trigger": "Authenticated resolve request",
    "journey_refs": ["Resolve ticket"],
    "acceptance_refs": ["acceptance-1"],
    "steps": [{
      "id": "authorize",
      "action": "Check account access",
      "owner": "Ticket service",
      "inputs": ["Actor ID and ticket ID"],
      "outputs": ["Authorized ticket scope"],
      "failures": ["Return forbidden without disclosing ticket data"]
    }]
  }]
}
```

Workflow IDs must be unique, and step IDs must be unique within their workflow. Up to 20 workflows with 12 steps each fit the supported contract, subject to the complete definition's 80 KB limit. Journey references must match actual journey entries; acceptance references use existing `acceptance-N` IDs. Owners, triggers, input/output contracts and failure paths come from the customer definition, not invented behavior.

Unlinked journeys and missing step inputs, outputs, failure paths or acceptance references produce `functional_definition_status: pending` and explicit `pending_fields`. A pending process map can be previewed, but it cannot be saved as an approved design review. Complete the definition, regenerate the bundle, and review it with `design_review.method: process_map`. Wireframe reviews use `method: wireframe`, the compatibility default.

A run freezes these reviewed documents and instructs the worker to implement the service contracts without adding a browser frontend. The SVGs and grid describe process-document readability for an API-only product. They do not turn a service into a UI project or replace contract, authorization, transaction and failure-path tests.

## Trace the journey to expected behavior

Every new bundle includes `feature_contract` and materializes `functional-contract.json`, for interface references and API process documents alike. Each journey identifies its relevant features and expected given/when/then behaviors, with acceptance references and failure paths. Process-definition readiness and feature-trace readiness are recorded separately: a complete process map does not silently complete missing feature definitions. Older briefs remain explicitly pending where that data has not been supplied.
