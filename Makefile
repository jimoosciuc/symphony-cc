.PHONY: help setup lint test ci all failure-drills live-github live-graphql live-claude live-remote live-remote-claude live-e2e live-concurrency-e2e live-integration live-validation

PYTHON ?= python

help:
	@echo "Targets: setup, lint, test, ci, all, failure-drills, live-github, live-graphql, live-claude, live-remote, live-remote-claude, live-e2e, live-concurrency-e2e, live-integration, live-validation"

setup:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e '.[dev]'

lint:
	ruff check src/ tests/

test:
	PYTHONPATH=src pytest -q

ci: lint test

all: ci

failure-drills:
	PYTHONPATH=src pytest -q tests/test_recovery.py tests/test_github_tracker.py tests/test_evidence.py tests/test_routing.py tests/test_timeouts.py tests/test_remote_ssh.py tests/test_remote_runner.py tests/test_orchestrator_remote.py tests/test_cleanup_executor.py tests/test_workflow_reload.py

live-github:
	SYMPHONY_RUN_GITHUB_INTEGRATION=1 PYTHONPATH=src pytest tests/test_github_tracker_live.py -q

live-graphql:
	SYMPHONY_RUN_GRAPHQL_TOOL_INTEGRATION=1 PYTHONPATH=src pytest tests/test_github_graphql_tool_live.py -q

live-claude:
	SYMPHONY_RUN_CLAUDE_INTEGRATION=1 PYTHONPATH=src pytest tests/test_claude_provider_live.py -q

live-remote:
	SYMPHONY_RUN_REMOTE_INTEGRATION=1 PYTHONPATH=src pytest tests/test_remote_integration_live.py -q

live-remote-claude:
	SYMPHONY_RUN_REMOTE_CLAUDE_E2E=1 PYTHONPATH=src pytest tests/test_remote_claude_e2e_live.py -v -s

live-e2e:
	SYMPHONY_RUN_FULL_E2E=1 PYTHONPATH=src pytest tests/test_live_e2e_full.py -v -s

live-concurrency-e2e:
	SYMPHONY_RUN_CONCURRENCY_E2E=1 PYTHONPATH=src pytest tests/test_live_e2e_concurrency.py -v -s

live-integration: live-github live-graphql live-claude live-remote

live-validation: live-github live-graphql live-claude live-remote live-e2e live-remote-claude live-concurrency-e2e
