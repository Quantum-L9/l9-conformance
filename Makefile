.PHONY: sync lint fmt typecheck test check check-law ci

sync:
	uv sync --frozen

fmt:
	uv run ruff format l9_conformance scripts tests

lint:
	uv run ruff format --check l9_conformance scripts tests
	uv run ruff check l9_conformance scripts tests

typecheck:
	uv run pyright

test:
	uv run python -m pytest tests

check:
	uv run python scripts/check_core_profile.py
	uv run python scripts/check_catalog.py

# LAW_ROOT=<Quantum-L9/.github checkout at the pinned revision>
check-law:
	uv run python scripts/check_catalog.py --law-root $(LAW_ROOT)

ci: lint typecheck test check
