---
description: Maintain canonical page metadata, local Manrope social previews and searchable documentation, with explicit publishing and campaign boundaries.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Publish discoverable documentation

Each documentation page has a descriptive title, a short summary and a canonical HTTPS URL. The build emits Open Graph and Twitter preview metadata, a local 1200 × 630 PNG card, and WebPage structured data with the publisher and site identity. MkDocs generates the sitemap. The 404 page is marked `noindex`.

## Keep page metadata current

Write a `description` in the Markdown front matter. Use 50 to 240 characters to explain the task the page helps a reader perform. Add a plain `title` when a visual home-page heading would make an unclear search result. Describe implemented behavior and preserve the limitations stated in the guide.

```yaml
description: Inspect retained execution evidence and recover interrupted work without repeating an action whose outcome is unknown.
```

Page titles and descriptions must be unique. Internal links and canonical URLs keep the repository's published base path. The validation step checks every canonical page against the sitemap and verifies the corresponding PNG, preview metadata and structured data.

## Review the sharing preview

Cards use the repository's local Manrope font and product lockup. Rendering blocks HTTP requests and checks that the text and footer fit the image. The image URL changes when its page metadata, font, logo or rendering template changes. All cards are generated during the documentation build; do not maintain a second set of exported images by hand.

Inspect the home-page, architecture and installation cards before publishing. Automated bounds checks catch clipping, but the editor still needs to review wording and hierarchy. Existing logo rights and the Manrope font license apply to the preview images.

## Search engines and campaigns

Descriptive headings, readable links and useful technical content support discovery. Canonical URLs and the sitemap identify the intended pages; they do not guarantee indexing or search position. WebPage structured data describes the documentation without inventing ratings, customer reviews or product pricing.

A search advertising campaign is a separate account and budget decision. Link a campaign to the relevant installation, integration or framework guide. Tracking parameters belong on the campaign link; the page's canonical URL remains unchanged. Advertising accounts, conversion tracking, analytics scripts and marketing cookies are not configured by this documentation build.

Search crawlers retrieve `robots.txt` from the origin root. A repository Pages site cannot set that origin-wide policy by placing a file inside its project path. Keep the generated page metadata and sitemap correct, and manage any origin-wide indexing policy at the hosting level.

## Build and verify

```sh
make docs-check
make docs-pages-check
```

GitHub Pages publishes the qualified main-branch site through the repository's documentation workflow. Check the live home page, a nested guide and a generated social image after deployment. A local successful build establishes the generated files; publication and third-party preview caches require their own checks.

Reference: [MkDocs configuration](https://www.mkdocs.org/user-guide/configuration/), [MkDocs deployment](https://www.mkdocs.org/user-guide/deploying-your-docs/) and [Material social-card metadata](https://squidfunk.github.io/mkdocs-material/setup/setting-up-social-cards/).
