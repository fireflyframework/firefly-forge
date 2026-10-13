<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Review user journeys in an isolated browser

Declare the user's goal, exercise the product's controls and record the observed states. A landing-page screenshot alone does not establish that the product works. The native `browser_journey` tool runs Playwright inside the repository's configured Docker browser image. It shares the component workspace with the application server and starts a fresh browser context for each call.

Grant `browser_journey` to the implementing role and configure the [browser runner](agent-tools.md#configure-real-browser-checks) before creating a run. Native Firefly Agentic and bridge workers receive only configured, granted tools. Official Codex and Claude Code CLI tools retain their separate permissions.

## Plan the journey before executing it

Specify the route, expected states, viewport, theme and bounded actions. Prefer accessible role locators; a CSS selector is available for controls without a suitable accessible name. This example fills a reservation form, submits it and checks the resulting status:

```json
{
  "journey_id": "reserve-stock",
  "name": "Reserve two units",
  "path": "/",
  "width": 390,
  "height": 844,
  "theme": "dark",
  "zoom": 100,
  "expected_states": [
    {"id": "reserved", "expected": "Reservation succeeds and stock decreases"}
  ],
  "actions": [
    {"kind": "fill", "state_id": "quantity-entered", "role": "textbox", "name": "Quantity", "value": "2"},
    {"kind": "click", "state_id": "submitted", "role": "button", "name": "Reserve"},
    {"kind": "assert", "state_id": "reserved", "selector": "#status", "assertion": "text", "value": "Reserved", "screenshot": true}
  ]
}
```

Supported actions are `navigate`, `click`, `fill`, `select`, `press`, `scroll` and `assert`. Assertions cover visibility, text, input value, enabled state, checked state, element count and the exact local route. Keyboard actions use a limited set of navigation and editing keys. The tool does not accept arbitrary JavaScript evaluation.

Each call allows at most 20 actions, with a combined action timeout of 120 seconds. Large plans must be split into smaller calls. `browser.max_calls` defaults to eight and accepts values from one to 20. The frozen browser and overall command budgets apply across repairs, resumes and provider handoffs.

## Read the evidence

Each observed state records the action, success or failure, current route, visible DOM text and rectangles, focus and bounded layout findings. Execution stops after a failed action or assertion. Expected states not reached remain in `not_reviewed`. Large DOM snapshots explicitly report truncation.

Screenshots identify the journey, state, viewport, theme, zoom, artifact ID and SHA256 of their actual PNG bytes. `zoom` applies CSS document zoom; it does not simulate the browser's native zoom setting. Exercise keyboard access, validation and error states, loading behavior, narrow viewports and both themes where the product supports them. These observations do not establish full accessibility conformance.

The browser can access only the declared container-local application origin. Docker networking is disabled; external resources, databases and WebSockets are unavailable in this profile. No host browser, user profile, Docker socket or host credentials are mounted. See [execution boundaries](sandbox.md) before choosing a runner.

## Distinguish DOM evidence from image review

A model receives actual PNG attachments only when image input has been verified for the selected provider and model revision. Unknown or unsupported image input produces `dom_only` evidence. Configuration presence and image output support do not prove image input support.

A model request contains at most five images and six million decoded image bytes, including any approved reference. A matching reference reserves one attachment slot, leaving four actual screenshots. History retains the most recent images within those limits and marks older images as no longer attached. A model cannot claim current visual inspection from a retained caption alone.

An approved reference must identify the exact raster bytes, state, viewport, theme and zoom. The source SVG's hash is not the PNG's hash. A mismatched or unavailable reference remains `reference_not_reviewed`; a model cannot choose its own reference and call it approved.

The structured QA report identifies reviewed and unreviewed states and findings with severity, evidence IDs, path, expected behavior and observed behavior. The runtime rejects claims about unseen states, unknown images and unmatched references. A visual judgment does not override failing verification, source boundaries, independent review or delivery controls.

High or critical findings in a worker's QA report block delivery. The report remains in the run's recorded usage so an operator can inspect the problem and its evidence before requesting another attempt. Absence of a report does not establish visual acceptance.

Native workers can request references by the frozen descriptor's `artifact_id`, such as `journey-01-desktop-light`. The plan must use its journey ID, default-state ID, exact viewport, theme and 100% zoom. The factory validates the saved review and source digest, then renders the approved SVG into PNG inside the isolated runner. The first render consumes a browser and command call; the checked per-run cache avoids repeating it on a repair or handoff. Source and raster hashes remain separate. Other states, changed fonts, custom font families without loaded assets, and mismatched viewports remain unreviewed.

Authenticated operators can download saved screenshots through `GET /api/v1/runner-evidence/{id}/screenshot`. Keep artifact hashes with the QA report when reviewing a product. Assess visual fidelity against the specific references and states reviewed; a browser or model result alone cannot establish pixel perfection.
