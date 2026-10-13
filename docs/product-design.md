<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Give each product an owned design system

The product brand kit belongs to the customer product, independently of the factory’s own branding. Save a generated kit or import reviewed tokens and a static SVG. A queued coding run freezes that kit so its design instructions remain reviewable even after the project’s next brand revision.

Start with the [customer definition and reviewed architecture](product-definition.md). Identify the real journey, audience, content and acceptance criteria before choosing a visual treatment. A framework recommendation or a generated logo does not establish customer acceptance.

## Generate a kit

Send authenticated `PUT /api/v1/repositories/REPOSITORY_ID/brand`:

```json
{
  "name": "Inventory workspace",
  "font_family": "Manrope",
  "primary": "#855414",
  "background": "#f3f1eb",
  "text": "#272820",
  "dark_background": "#10110f",
  "dark_text": "#f3f1eb"
}
```

The result includes a revision, content digest, tokens and files: `tokens.json`, `logo.svg`, `theme.css` and `brand-guide.md`. Read the latest kit with `GET /api/v1/repositories/{id}/brand`. The generated spacing scale uses four-pixel increments and a control radius. Review and tune the tokens for the customer's interface before adopting them.

Generated Manrope wordmarks use outlined glyphs, and the kit embeds the licensed Manrope font in `theme.css`. Other selected families produce text-based SVG wordmarks and require licensed local font assets in the product. Naming a family does not install it or establish its license. Check those SVGs with the intended font loaded and review logo contrast on the actual surfaces. An imported or generated mark still needs review for each placement.

## Import the supported token subset

Supply `tokens` in the same brand request to use a reviewed token document. The renderer supports an explicit subset of the DTCG 2025.10 format:

- `color` values with `colorSpace: srgb`, three components between 0 and 1, and optional alpha.
- `dimension` values with a finite nonnegative value and `px` or `rem` units.
- `fontFamily` values containing a single explicit family name.

Aliases must be resolved before import. Other token types and automatic reference resolution are not implemented. Token documents are bounded to 64 KB, nesting and variable count are limited, and group names must be lowercase bounded CSS-safe names.

Both `color.light.background`/`text` and `color.dark.background`/`text` are required and must be opaque. Normal text/background contrast must reach 4.5:1 in each theme. That check covers those semantic pairs, not every button, chart, border or disabled state in the final product.

An explicit color token looks like:

```json
{
  "$type": "color",
  "$value": {"colorSpace": "srgb", "components": [0.153, 0.157, 0.125], "alpha": 1}
}
```

The generated CSS exposes token paths as variables and maps light/dark background and text semantics through `data-theme="dark"`. Use the reviewed semantic variables consistently; implement the product’s actual theme preference control and verify every interactive state.

## Import a static SVG safely

Provide `logo_svg` only for artwork the product is authorized to use. Import accepts bounded static vector/text elements and local fragment references used by gradients or clipping. Scripts, event handlers, external resources, embedded images, styles and unsupported active elements are rejected. Declarations and entities are not accepted.

Validation prevents active code and remote-resource loads; it does not grant trademark rights or prove that the image is legible. Check its view box, scaling, font availability and appearance against both backgrounds. Keep the customer’s license and attribution requirements with the asset.

## Build the interface in reusable layers

Use the frozen kit as an implementation contract:

| Layer | Example in the inventory product | Acceptance focus |
| --- | --- | --- |
| Atoms | Button, input, quantity text, status marker | Token usage, contrast and visible keyboard focus |
| Molecules | Labelled quantity field with validation, item summary | Accessible names, errors and feedback connected to the control |
| Organisms | Stock table, reservation form, navigation | Keyboard order, responsive layout and loading/empty/error states |
| Templates | Shared page shell and list/detail layout | Consistent spacing, headers and content boundaries |
| Pages | Create item, reserve stock, inspect conflict | The complete customer journey with actual service behavior |

Keep business rules in typed domain/application services. Frontend components should display and invoke those contracts; controllers should validate and delegate. Use native PyFly or LaraFly dependency injection and data/web modules for the chosen backend. A neat component tree cannot substitute for authorization, transaction integrity or meaningful API tests.

## Freeze and inspect the coding inputs

When the factory creates a run, it captures the current project brand revision as `branding_snapshot`. The worker receives those files and explicit instructions to use tokens, atomic design, light/dark modes, labelled controls, focus and actual journey tests. The journal retains the frozen customer/architecture/brand context with its plan. A later brand save does not alter a queued or resumed run’s kit.

Review the resulting files and UI. A copied token document is not evidence that components use it; passing a build does not prove that a mobile layout is readable. Inspect light/dark views, narrow and wide widths, keyboard flows, zoom, errors and the actual customer journey. Keep screenshots and behavioral checks with the run’s evidence.

For a visual maintenance change, preserve the accepted customer behavior and add focused regression coverage. Update the project’s next kit revision deliberately, then queue the new work. [Policies](policies.md) can add reusable design recommendations and executable gates; [DevSecOps](devsecops.md) and [application operations](application-operations.md) cover release and support evidence.
