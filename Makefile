# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
.PHONY: check php-fixture-check python-check studio-check docs docs-check docs-pages-check package serve

FORGE_TEST_PHP_FIXTURE ?= php
# Preserve literal dollar signs before Make exports an executable path.
override FORGE_TEST_PHP_FIXTURE := $(value FORGE_TEST_PHP_FIXTURE)
export FORGE_TEST_PHP_FIXTURE

check: python-check studio-check docs-check

php-fixture-check:
	@command -v "$${FORGE_TEST_PHP_FIXTURE}" >/dev/null || { echo "PHP fixture missing: prepare an existing PHP 8.3+ CLI or set FORGE_TEST_PHP_FIXTURE to its executable." >&2; exit 78; }
	@"$${FORGE_TEST_PHP_FIXTURE}" -n -r 'if (PHP_SAPI !== "cli" || PHP_VERSION_ID < 80300 || !function_exists("proc_open") || !function_exists("hrtime") || !function_exists("getrusage") || !function_exists("memory_get_peak_usage")) { fwrite(STDERR, "PHP fixture requires PHP 8.3+ CLI with process and resource observation functions.\n"); exit(78); } echo "Prepared PHP fixture ", PHP_VERSION, "\n";'

python-check: php-fixture-check
	uv run python -m compileall -q src
	uv run ruff check src tests scripts/check_ci_gates.py scripts/check_release_payloads.py
	uv run ruff format --check src tests scripts/check_ci_gates.py scripts/check_release_payloads.py
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
