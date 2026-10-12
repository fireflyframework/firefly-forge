<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Use Firefly Forge visual assets

Forge shares Firefly's charcoal, paper and amber palette and Manrope typography.
The product identity combines the official Firefly compact artwork, a measured
hairline separator and an outlined lowercase Forge wordmark.

| Asset | Ownership and licence |
| --- | --- |
| Firefly name, wordmark, spark, chevron and icon | Proprietary Firefly Software Solutions Inc. marks, used with permission; not Apache-2.0 |
| Forge name and standalone wordmark | Firefly Software Foundation trademark; first-party wordmark artwork under Apache-2.0 without trademark rights |
| Combined Firefly Forge lockups and banners | Include proprietary parent marks; preserve their separate notice |
| Manrope fonts | The Manrope Project Authors, SIL Open Font License 1.1 |
| Studio's Lucide icons | Lucide ISC, with MIT for Feather-derived portions |
| First-party diagrams and documentation | Firefly Software Foundation, Apache-2.0 |

`assets/brand/inventory.json` records the generated asset paths, owners, licences,
source revision and hashes. Do not claim the combined artwork is Apache-2.0.
Keep `OFL.txt` with redistributed font files and preserve icon dependency notices.

## Regenerate artwork

The public generator imports the official logo and type modules from a separate
private brand-kit checkout. It copies no private source into Forge:

```sh
node scripts/brand-assets.mjs --kit /path/to/private/firefly-brand-kit --out /path/to/firefly-forge
```

The source revision is Firefly Brand Kit 2.0.0,
`3fc4834068fc531dad6fcff4719af2bb5b6d5c9e`. Preserve parent geometry, trail stops,
aspect ratios and clear space. The product wordmark uses Manrope Medium outlines;
it is never live text in a logo. Inspect regenerated assets and hashes before
using them.

Use the reversed lockup on charcoal and the light lockup on paper or white.
The small reversed lockup uses a solid chevron. Reserve one Firefly x-height of
clear space around the logo. Keep the Firefly part at least 48 pixels wide for a
small lockup and 80 pixels for a full lockup; use the icon below those sizes.

## Apply colors and type

| Role | Value |
| --- | --- |
| Charcoal background | `#10110F` |
| Paper text / diagram ground | `#F3F1EB` |
| Amber action and focus | `#FFB34A` |
| Stone secondary text on charcoal | `#BFB8AB` |
| Ink on paper | `#272820` |
| Muted on paper | `#62645B` |
| Gold accent text on paper | `#855414` |

Serve Manrope from the application's own files. Amber is reserved for action,
focus and the parent mark; use gold for small colored text on light backgrounds.
Use Arial in standalone SVG diagrams so their labels do not depend on a page's
font loading. Keep status meanings clear in text as well as color.
