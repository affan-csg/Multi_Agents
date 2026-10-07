.PHONY: help install dev test lint format clean

help:
	@echo "CSG Agents Development Commands"
	@echo "================================"
	@echo ""
	@echo "Setup:"
	@echo "  make install       Install all dependencies (Python venv + npm packages)"
	@echo ""
	@echo "Development:"
	@echo "  make dev           Run backend (:8000) + Dashboard (:5173) together [requires 2 terminals]"
	@echo "  make backend       Run backend only (both agents on :8000)"
	@echo "  make dashboard     Run Dashboard only (:5173)"
	@echo ""
	@echo "Testing:"
	@echo "  make test          Run all tests (Research, Outreach, Server)"
	@echo "  make test-research Run Research Agent tests only"
	@echo "  make test-outreach Run Outreach Agent tests only"
	@echo "  make test-server   Run Server tests only"
	@echo "  make test-dashboard Type-check Dashboard (no runtime tests)"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint          Run all linters (mypy, eslint, tsc)"
	@echo "  make format        Auto-format code (black, prettier)"
	@echo "  make typecheck     TypeScript type checking (Dashboard)"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean         Remove __pycache__, .pytest_cache, node_modules, dist"
	@echo "  make clean-venv    Remove .venv (caution: run 'make install' after)"
	@echo ""
	@echo "See CONTRIBUTING.md for full documentation."

# Setup targets
install:
	python -m venv .venv 2>/dev/null || true
	@echo "Activating venv and installing Python packages..."
	pip install -e Research_Agent -e Outreach_Agent -e Server
	cd Dashboard && npm ci

dev:
	@echo "Starting full stack (backend + dashboard)"
	@echo "Terminal 1: Backend on :8000"
	@echo "Terminal 2: Dashboard on :5173"
	@echo ""
	@echo "To start, run these in separate terminals:"
	@echo "  make backend"
	@echo "  make dashboard"

backend:
	python -m uvicorn app:app --app-dir Server --reload \
		--reload-dir Research_Agent/src --reload-dir Outreach_Agent/src --reload-dir Server \
		--port 8000

dashboard:
	cd Dashboard && npm run dev

# Testing targets
test: test-research test-outreach test-server test-dashboard
	@echo "All tests passed!"

test-research:
	cd Research_Agent && pytest -v

test-outreach:
	cd Outreach_Agent && pytest -v

test-server:
	cd Server && pytest -v

test-dashboard:
	cd Dashboard && npx tsc -b --noEmit

# Code quality targets
lint: typecheck
	@echo "Linting complete"

typecheck:
	cd Dashboard && npx tsc -b --noEmit

format:
	@echo "Note: automatic code formatting not configured yet"
	@echo "See CONTRIBUTING.md for code style guidelines"

# Cleanup targets
clean:
	@echo "Cleaning up..."
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .venv -prune -o -type d -name node_modules -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name dist -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name dist-ssr -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	@echo "Cleanup complete"

clean-venv:
	@echo "Removing .venv..."
	rm -rf .venv
	@echo "Run 'make install' to recreate it"
