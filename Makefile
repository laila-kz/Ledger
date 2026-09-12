.PHONY: help check test lint fmt clean sync

PYTHON ?= python
VENV_RUN ?= $(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts/,$(if $(wildcard .venv/bin/python),.venv/bin/,))

help:
	@echo "Available commands:"
	@echo "  make check  - Run full quality gate (lint, format check, mypy strict, pytest)"
	@echo "  make test   - Run test suite with coverage"
	@echo "  make lint   - Run ruff check and mypy strict"
	@echo "  make fmt    - Auto-fix formatting and imports via ruff"
	@echo "  make clean  - Remove caches, temporary files, and test artifacts"
	@echo "  make sync   - Install all project and development dependencies"

check: lint test

test:
	$(VENV_RUN)pytest --cov=ledger --cov-report=term-missing

lint:
	$(VENV_RUN)ruff check .
	$(VENV_RUN)ruff format --check .
	$(VENV_RUN)mypy .

fmt:
	$(VENV_RUN)ruff check --fix .
	$(VENV_RUN)ruff format .

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build *.egg-info
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

sync:
	$(VENV_RUN)pip install -e ".[dev,backtest]"
