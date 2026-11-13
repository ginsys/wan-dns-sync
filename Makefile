.PHONY: help venv venv-reset install dev test lint typecheck check format clean build run test-run

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

help:
	@echo "Available targets:"
	@echo "  venv       - Create Python virtual environment and install dev dependencies"
	@echo "  venv-reset - Remove and recreate virtual environment"
	@echo "  install    - Install package in production mode"
	@echo "  dev        - Install package with dev dependencies"
	@echo "  test       - Run pytest"
	@echo "  lint       - Run ruff linter"
	@echo "  typecheck  - Run mypy type checker"
	@echo "  check      - Run all checks (lint, typecheck, test)"
	@echo "  format     - Format code with ruff"
	@echo "  clean      - Remove build artifacts"
	@echo "  build      - Build Docker image"
	@echo "  run        - Run with example config (dry-run)"
	@echo "  test-run   - Run container with .env config (dry-run, single iteration)"

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

venv: $(VENV)/bin/activate
	$(PIP) install -e ".[dev]"
	@echo ""
	@echo "Virtual environment ready. Activate with:"
	@echo "  source $(VENV)/bin/activate"

venv-reset: clean
	rm -rf $(VENV)
	$(MAKE) venv

install: $(VENV)/bin/activate
	$(PIP) install .

dev: $(VENV)/bin/activate
	$(PIP) install -e ".[dev]"

test: $(VENV)/bin/activate
	$(PYTHON) -m pytest

lint: $(VENV)/bin/activate
	$(VENV)/bin/ruff check .

typecheck: $(VENV)/bin/activate
	$(VENV)/bin/mypy src/

check: lint typecheck test

format: $(VENV)/bin/activate
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

IMAGE := ghcr.io/ginsys/wan-dns-sync

build:
	docker build -t $(IMAGE) .

run: $(VENV)/bin/activate
	$(VENV)/bin/wan-dns-sync --config config.example.yaml --once --dry-run --log-level debug

test-run: build
	@test -f .env || (echo "Error: .env file not found. Copy .env.example to .env and fill in values." && exit 1)
	docker run --rm -it \
		--env-file .env \
		-v $(PWD)/config.test.yaml:/etc/wan-dns-sync/config.yaml:ro \
		-p 8080:8080 \
		$(IMAGE) \
		--once --log-level debug
