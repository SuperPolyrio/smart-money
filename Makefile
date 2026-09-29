PYTHON ?= python3
SOURCE := src/smart_money
TESTS := tests

.PHONY: test lint format format-check typecheck build

test:
	$(PYTHON) -m pytest $(TESTS)

lint:
	$(PYTHON) -m ruff check $(SOURCE) $(TESTS)

format:
	$(PYTHON) -m ruff format $(SOURCE) $(TESTS)

format-check:
	$(PYTHON) -m ruff format --check $(SOURCE) $(TESTS)

typecheck:
	$(PYTHON) -m mypy --config-file pyproject.toml $(SOURCE)

build:
	$(PYTHON) -m build
