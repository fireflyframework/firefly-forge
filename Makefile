.PHONY: check python-check studio-check docs package serve

check: python-check studio-check docs

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

package: studio-check
	uv build
	uv run python scripts/check_distribution.py dist

serve:
	uv run forge serve
