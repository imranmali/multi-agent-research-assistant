# Developer shortcuts. Run `make help` for the list.
# Assumes an activated virtualenv (python -m venv .venv && . .venv/bin/activate).

PYTHON ?= python
IMAGE  ?= ghcr.io/imranmali/multi-agent-research-assistant:local
Q      ?= What is the state of solid-state batteries?
PROVIDER ?= mock

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test cov check run serve worker \
        docker-build compose-up compose-down openspec-validate clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install the package in editable mode with dev dependencies
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

lint: ## Ruff lint + format check
	ruff check src tests
	ruff format --check src tests

format: ## Auto-format and apply safe lint fixes
	ruff format src tests
	ruff check --fix src tests

typecheck: ## Strict mypy
	mypy

test: ## Run the test suite
	pytest

cov: ## Tests with coverage gate (>= 85%) and coverage.xml
	pytest --cov=research_assistant --cov-report=xml --cov-report=term --cov-fail-under=85

check: lint typecheck cov openspec-validate ## Everything CI runs (except the Docker smoke test)

run: ## Research a question from the CLI (Q="...", PROVIDER=mock|anthropic)
	research-assistant run "$(Q)" --provider $(PROVIDER)

serve: ## Start the API on :8000
	research-assistant serve

worker: ## Start a worker (needs Redis; REDIS_URL defaults to localhost)
	JOB_BACKEND=redis research-assistant worker

docker-build: ## Build the container image
	docker build -t $(IMAGE) .

compose-up: ## Start redis + api + 2 workers in the background
	docker compose up --build -d

compose-down: ## Stop the compose stack
	docker compose down

openspec-validate: ## Validate all OpenSpec specs and changes
	openspec validate --all --strict

clean: ## Remove caches and build artefacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
