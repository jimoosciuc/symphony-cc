.PHONY: help setup lint test ci all live-github live-graphql live-claude live-remote live-integration

PYTHON ?= python

help:
	@echo "Targets: setup, lint, test, ci, all, live-github, live-graphql, live-claude, live-remote, live-integration"

setup:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e '.[dev]'

lint:
	ruff check src/ tests/

test:
	PYTHONPATH=src pytest -q

ci: lint test

all: ci

live-github:
	SYMPHONY_RUN_GITHUB_INTEGRATION=1 PYTHONPATH=src pytest tests/test_github_tracker_live.py -q

live-graphql:
	SYMPHONY_RUN_GRAPHQL_TOOL_INTEGRATION=1 PYTHONPATH=src pytest tests/test_github_graphql_tool_live.py -q

live-claude:
	SYMPHONY_RUN_CLAUDE_INTEGRATION=1 PYTHONPATH=src pytest tests/test_claude_provider_live.py -q

live-remote:
	SYMPHONY_RUN_REMOTE_INTEGRATION=1 PYTHONPATH=src pytest tests/test_remote_integration_live.py -q

live-integration: live-github live-graphql live-claude live-remote
