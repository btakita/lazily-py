.PHONY: init build test lint format type-check clean publish-test publish bench bench-scale compile conformance-coverage

# Install development dependencies and package in editable mode
init: PY_VERSION = $(shell [ -f .python-version ] && \
	cat .python-version || \
	uv run python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" \
)
init:
	@echo "Using Python version: $(PY_VERSION)"

	@if command -v mise >/dev/null 2>&1; then \
		mise install; \
	fi

	uv venv .venv --python "$(PY_VERSION)" --no-project --clear --seed $(VENV_ARGS)

	@if [ -n "$(ALL)" ]; then \
		uv sync --python "$(PY_VERSION)" --all-groups --all-extras $(SYNC_ARGS); \
	else \
		uv sync --python "$(PY_VERSION)" $(SYNC_ARGS); \
	fi

# Run tests
#
# Delegates to the poe tasks rather than repeating the manifest env var and the
# truncation here. CI runs `poe precommit`, so any setup that lives only in this
# file is setup CI does not do — which is how the coverage guard came to run
# nowhere but a developer's machine (#lzguardsnotinci).
test:
	uv run poe conformance_manifest
	uv run poe test

# Compile the reactive core with mypyc (in-place .so files). Idempotent —
# rebuild after editing src/lazily/{slot,cell,signal,effect,batch}.py to run
# tests/benchmarks against the compiled code. If mypyc is unavailable this is a
# no-op and tests/benches fall back to the pure-Python sources.
compile:
	-uv run mypyc --follow-imports=silent --config-file=pyproject.toml \
		src/lazily/slot.py src/lazily/cell.py src/lazily/signal.py \
		src/lazily/effect.py src/lazily/batch.py

# Run tests with coverage
test-cov:
	uv run pytest tests/ --cov=lazily --cov-report=html --cov-report=term-missing

# Format code with ruff
format:
	uv run ruff format src/lazily/ tests/

# Lint code with ruff
lint:
	uv run ruff check src/lazily/ tests/

# Type check
type-check:
	poe ty

# Run all checks
.PHONY: test-interop-peer
test-interop-peer:
	uv run python -m lazily.interop_peer --self-check

check: format lint type-check test conformance-coverage test-interop-peer

# Run the micro-benchmark suite (see BENCHMARKS.md)
bench:
	uv run python -m lazily.benchmarks

# Run the large spreadsheet-shaped scale suite (see BENCHMARKS.md).
# Override size/viewport with LAZILY_SCALE_N / LAZILY_SCALE_VIEWPORT, e.g.:
#   LAZILY_SCALE_N=5000000 make bench-scale   # Google Sheets 10M-cell workbook
bench-scale:
	uv run python -m lazily.scale_bench

# Clean build artifacts
clean:
	rm -rf dist/ build/ *.egg-info/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Build package
build: clean
	python -m build

# Publish to TestPyPI
publish-test: build
	python -m twine upload --repository testpypi dist/*

# Publish to PyPI
publish: build
	@echo "WARNING: This will publish to the real PyPI!"
	@read -p "Are you sure? (y/N) " confirm && [ "$$confirm" = "y" ]
	python -m twine upload dist/*

# Conformance-coverage guard (#portconformancecoverage). Static: fails when the
# canonical corpus grows a fixture no test in this repo even names. Naming is not
# replaying — see the script header for what this does and does not prove.
conformance-coverage:
	uv run poe conformance_coverage
