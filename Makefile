# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
.PHONY: check python-check studio-check docs docs-check docs-pages-check package serve

check: python-check studio-check docs-check

python-check:
	uv run python -m compileall -q src
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy src/firefly_forge
	uv run pytest

studio-check:
	npm --prefix studio run lint
	npm --prefix studio run format:check
	npm --prefix studio run check
	npm --prefix studio test
	npm --prefix studio run build
	npm --prefix studio run test:browser

docs:
	uv run --group docs mkdocs build --strict

docs-check: docs
	uv run --group docs python scripts/check_docs_discovery.py build/docs-site https://fireflyframework.github.io/firefly-forge/
	node studio/node_modules/eslint/bin/eslint.js -c scripts/docs-eslint.config.mjs scripts/docs-scene scripts/docs-browser scripts/*docs*.mjs scripts/test_operator_motion.mjs scripts/test_factory_sequence.mjs
	node studio/node_modules/prettier/bin/prettier.cjs --check scripts/docs-scene scripts/docs-browser scripts/*docs*.mjs scripts/test_operator_motion.mjs scripts/test_factory_sequence.mjs
	node --test scripts/test_docs_scene.mjs scripts/test_operator_motion.mjs scripts/test_factory_sequence.mjs
	uv run --group docs python -m unittest scripts.test_docs_scene_hook scripts.test_architecture_diagrams scripts.test_docs_discovery scripts.test_docs_server
	uv run --group docs node studio/node_modules/@playwright/test/cli.js test --config scripts/docs-playwright.config.mjs

docs-pages-check:
	uv run --group docs mkdocs build --strict --site-dir build/docs-pages/firefly-forge
	uv run --group docs python -m unittest scripts.test_docs_site
	uv run --group docs python scripts/check_docs_site.py
	node studio/node_modules/@playwright/test/cli.js test --config scripts/docs-pages-playwright.config.mjs

package: studio-check docs
	uv build
	uv run python scripts/check_distribution.py dist

serve:
	uv run forge serve
