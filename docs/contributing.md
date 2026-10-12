<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Contribute a working change

Read the relevant source and existing patterns before editing. Keep changes
focused, add meaningful behavior tests and update the task guide or reference
that explains the changed public contract. Preserve unrelated work and keep
credentials, local data, dependencies and disposable renders out of publication.

## Verify the backend

From the repository root:

```sh
uv sync --locked --group dev --group docs
uv run pytest
uv run ruff check src tests
uv run mypy
```

Use the root project's check command when present to run its complete configured
gates. Live provider execution requires an explicitly configured provider account
and owned repositories; offline tests do not establish live model success.

## Verify Studio

```sh
cd studio
npm ci
npm run check
npm run lint
npm run format:check
npm test
npm run build
npx playwright install chromium
npm run test:browser
```

Inspect the rendered UI at desktop and phone widths. Exercise authentication
errors, loading and empty states, repository registration, creation, cancellation,
retry and evidence tabs with the real API. Verify keyboard focus and modal/tab
navigation. A successful production build is only the compilation gate.

## Verify documentation

```sh
uv run --group docs mkdocs build --strict
uv run --group docs mkdocs serve --dev-addr 127.0.0.1:8000
```

Use relative source links. Add new public pages to `mkdocs.yml`. Keep directions
task-based: prerequisites, ordered steps, expected result and actionable failure
diagnosis. Run new documented commands and inspect the rendered page.

Diagrams are editable SVGs with a viewBox, title and full description. Use labels
at least 16 pixels, an accessible reading guide and a full-size link. Check text
and arrows at native and page widths; XML validation does not prove legibility.

The public repository contains product source and user/developer documentation.
Keep internal planning, agent instructions, private bridge source and execution
evidence outside it.
