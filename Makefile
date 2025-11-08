.PHONY: install-dev build test lint format type-check clean publish-test publish

# Install development dependencies and package in editable mode
install-dev:
	pip install build twine pytest pytest-cov ruff mypy
	pip install -e .

# Run tests
test:
	pytest tests/ -v

# Run tests with coverage
test-cov:
	pytest tests/ --cov=become --cov-report=html --cov-report=term-missing

# Format code with ruff
format:
	ruff format become/ tests/

# Lint code with ruff
lint:
	ruff check become/ tests/

# Type check
type-check:
	mypy become/

# Run all checks
check: format lint type-check test

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