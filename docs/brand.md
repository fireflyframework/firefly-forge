<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

# Use firefly forge visual assets

The product name is **firefly forge**, in lowercase. Its identity combines the
unmodified Firefly parent artwork, a measured hairline separator and a Forge
wordmark with an original lowercase **f** emblem. The factory scene comes from
the actual Firefly website geometry, rather than a separate flowchart style.

## The forged f

The emblem is a formed stem and two cut billet arms. Its ascender is 148 drawing
units, its stem and crossbar are 26 units thick, and its arm terminals have
45-degree cuts. The curved upper junction makes it read as lowercase f. A short
hot edge occupies the upper billet: amber on charcoal, gold on paper. The rest
of **orge** is unaltered Manrope Medium outlines, aligned to the Firefly baseline.

The standalone mark and the first letter in the wordmark use the same shape.
The wordmark applies a 74% horizontal optical scale, giving its stem the same
weight as Manrope Medium, and a 64-unit advance before **o**. These adjustments
keep **forge** a single word; the standalone icon retains its original width.
Small versions use one color, so the letter's shape stays clear at 16 and 24
pixels. Do not type an ordinary f in its place, reshape the billets, add a dot,
rotate the mark or alter the parent wordmark.

| Placement                         | Asset                                                 |
| --------------------------------- | ----------------------------------------------------- |
| Dark header and navigation        | `forge-lockup-dark.svg`                               |
| Light header and navigation       | `forge-lockup-light.svg`                              |
| Compact dark slot                 | `forge-mark-mono-dark.svg`                            |
| Compact light slot                | `forge-mark-mono-light.svg`                           |
| Standalone symbol at larger sizes | `forge-mark-dark.svg` or `forge-mark-light.svg`       |
| Browser/application icon          | `forge-favicon-dark.svg` or `forge-favicon-light.svg` |
| Dark/light introduction           | `banner-dark.svg` or `banner-light.svg`               |

The source assets live in `assets/brand/`; application-ready copies live in
`studio/public/assets/` and documentation-ready copies in `docs/assets/`.
`forge-lockup.svg` and the root favicon remain dark compatibility aliases.

## Geometry and alignment

Use one Firefly x-height, X = 108 drawing units, as clear space around a lockup.
The divider sits 1X after the parent chevron and the product starts 1X after the
divider. Its stroke is 0.04X, its height is 1.25 times the parent compact logo,
and it is centered on the parent's x-height. Preserve the supplied viewBox and
aspect ratio; clear space belongs to the layout rather than the file.

Use the full parent lockup when its Firefly part is at least 80 pixels wide.
Use a small lockup, with a solid parent chevron and one-color f, when that part
is 48–80 pixels wide. Use the standalone f for narrower slots. Align the actual
SVG box to the shared shell grid, not a hand-typed product title.

## The actual Firefly factory

`forge-factory-dark.svg` and `forge-factory-light.svg` preserve the website's
onboarding composition: projected machines, module platforms, connected
conveyors, side lane, and separate API/event backbone pipes. Only material
colors change between themes. Website business-step numbers and decision badges
are omitted so they do not imply a Forge runtime operation.

The authoritative renderer is `src/scripts/factory-drawing.ts` in
`firefly-oss/firefly-software-website`, pinned to
`6a2d519ab917fb183e607bcc5b4ab13f3819748f`. It uses the same scenario layouts and
machine geometry as the website's 3D scene. The public asset is self-contained
SVG: it requires no website JavaScript, private schemas, external image or font
service. Its description labels it conceptual, not a live execution graph.

Use the scene with an adjacent explanation. The illustration does not prove a
runtime dependency, a provider's readiness or the result of a run. Actual
progress and verification remain visible in the application controls.

## The operational Studio scene

Studio loads the generated `forge-operational-model.json.gz` snapshot through
its first-party Three.js renderer. Seven original website machine types represent
prepare, implement, verify, checkpoint, publish, deploy and health checks. The
snapshot contains geometry, materials and mechanical keyframes; the private
website constructors and brand schemas are not distributed.

Actual plan receipts and deployment records supply station states. Only active
stations animate. Paused work, reduced-motion preferences and a background tab
stop motion. The output carrier moves after an observed stage-state change;
mechanical cycles do not estimate completion. Selecting a station opens its
recorded detail, with a closer camera on narrow screens. Keyboard stage controls
remain available when WebGL fails or loses its context, alongside the static SVG.

`assets/factory/operational-model-provenance.json` records the source revision,
machine layout, source hashes and compressed/uncompressed snapshot digests.
The brand generator preserves that pinned snapshot and synchronizes application
copies and notices. Keep the Three.js MIT notice with the application bundle.

## Light and dark colors

| Role                  | Dark                           | Light                          |
| --------------------- | ------------------------------ | ------------------------------ |
| Page ground           | Charcoal `#10110F`             | Paper `#F3F1EB`                |
| Surface               | `#1A1B17`                      | White `#FFFFFF`                |
| Main text             | Paper `#F3F1EB`                | Ink `#272820`                  |
| Secondary text        | Stone `#BFB8AB`                | Muted `#62645B`                |
| Rules/dividers        | Graphite `#474A42`             | Line `#D8D4CA`                 |
| Accent text/focus     | Amber `#FFB34A`                | Gold `#855414`                 |
| Filled primary action | Amber `#FFB34A`, charcoal text | Amber `#FFB34A`, charcoal text |

Serve Manrope from the application's own files. Gold supplies readable accent
text on paper; amber stays a measured action and artwork accent. The factory's
material shades come from the source design system and retain face depth rather
than flattening to a single color. Communicate status in text as well as color.

## Ownership and provenance

| Asset                                           | Owner and licence                                                                                         |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Firefly parent marks                            | Firefly Software Solutions Inc., proprietary artwork and trademarks; used with permission, not Apache-2.0 |
| Factory geometry and graphic system             | Firefly Software Solutions Inc., proprietary; generated authorized product assets only                    |
| Original forged f and standalone Forge wordmark | Firefly Software Foundation; first-party copyright artwork Apache-2.0, trademark rights reserved          |
| Combined logos and banners                      | Include proprietary parent artwork; retain its separate notice                                            |
| Manrope                                         | The Manrope Project Authors; SIL Open Font License 1.1                                                    |
| Studio Lucide icons                             | ISC, with MIT for Feather-derived portions                                                                |
| Three.js renderer library                       | three.js authors; MIT                                                                                     |

`assets/brand/inventory.json` records owners, rights, source hashes, pinned
revisions and SHA-256 values for generated assets and application copies. Keep
`NOTICE` with the marks, `OFL.txt` with fonts and the icon notices with their
bundles. The separate Agent Bridge uses its own connected-span
identity; its assets are not part of the public Forge inventory.

## Regenerate and review

Use external authorized checkouts of Firefly Brand Kit 2.0.0 and the pinned
website source. Install Studio's declared dependencies for the renderer
bundler. No private source or schema module enters the public output:

```sh
node scripts/brand-assets.mjs \
  --kit /path/to/private/firefly-brand-kit \
  --website /path/to/firefly-software-website \
  --out /path/to/firefly-forge
```

The generator verifies the parent logo/type source hashes and website revision
before composing the artwork. Review the regenerated source and inventory.
Render both themes at native size, inspect marks at 16, 24, 32 and 64 pixels,
and check header/compact placements at their real size. A valid SVG or matching
hash does not establish visual quality or correct alignment in the application.

## Documentation illustration

The documentation home loads the same authorized station snapshot through a separate first-party Three.js adapter. One 14-second demonstration clock moves the document between stations, holds it while that station's clip works, and fades the reset before starting again. No machinery works during transit or reset. Two first-party operators share coffee in a bay joined to the factory floor and alternate short console visits; the terminals show the same demonstration sequence. Their walking joints, aisle routes and cup clearance are checked against the actual mesh geometry. The illustration never reads run data or estimates progress. The live Studio renderer continues to use recorded station states.

Install Studio dependencies with `npm --prefix studio ci`, then run `make docs`. The strict MkDocs hook bundles `scripts/docs-scene/` into ignored `build/docs-scene`, copies the pinned factory snapshot and provenance, and includes the Three.js MIT and artwork notices. Local CSS and scene script URLs include a content hash so rebuilt styles and scenes are distinguishable in browser caches. Rendering uses local assets and needs no CDN or private source checkout.

`make docs-check` adds motion-gate, bundle, cache-version and real Playwright checks. The home illustration supports play/pause, static reduced motion, light and dark appearance, phone resizing, viewport and background-tab suspension, and a static SVG if WebGL fails. Motion is capped at 30 frames per second, with a bounded pixel ratio and GPU cleanup when the page closes or loses its context. Review the actual page in both themes and at phone widths before publishing; passing tests alone does not establish visual quality.
