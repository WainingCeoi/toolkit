# Universal entry point for the monorepo (backend + frontend).
# Run `make help` to see the targets. Recipe lines MUST be TAB-indented.
SHELL := /bin/bash

# Base API port. Override when :8000 is busy, e.g.:  make dev PORT=8010
PORT ?= 8000

# Optional backend extras. `docmd` pulls MinerU (Doc→Markdown), whose torch
# dependency ships NO macOS x86_64 wheel — so it is skipped automatically on
# Intel Macs, where installing it can only fail. Force either way:
#   make install EXTRAS=docmd     (force on)
#   make install EXTRAS=          (force off)
EXTRAS ?= $(shell if [ "$$(uname -s)" = "Darwin" ] && [ "$$(uname -m)" = "x86_64" ]; \
                  then echo ""; else echo "docmd"; fi)
EXTRA_FLAGS := $(if $(strip $(EXTRAS)),--extra $(strip $(EXTRAS)),)

.PHONY: help install dev backend frontend build start host test lint clean

help:
	@echo "make install   install backend (uv) + frontend (npm) dependencies"
	@echo "make dev       run backend (:$(PORT)) + frontend (:5173) together, hot-reload"
	@echo "make start     build the frontend, then serve API + UI from ONE server (127.0.0.1:$(PORT))"
	@echo "make host      build + serve API + UI to the whole LAN (http://<this-machine>.local:$(PORT))"
	@echo "make build     build the frontend for production (frontend/dist)"
	@echo "make test      backend tests + lint + a frontend build check"
	@echo "make clean     remove build artifacts and caches"

install:
	@if [ -z "$(strip $(EXTRAS))" ]; then \
	  echo "note: skipping the MinerU extra on this machine (no macOS x86_64 wheel)."; \
	  echo "      Doc to Markdown will be unavailable; every other tool works."; \
	  echo "      Hide it from the UI with TOOLKIT_DISABLED_TOOLS=doc-to-markdown in backend/.env"; \
	fi
	cd backend && uv sync $(EXTRA_FLAGS)
	cd frontend && npm install

# Development: both servers, one command, one Ctrl-C. Vite proxies /api -> :$(PORT).
# Loopback only — the hot-reload dev server is never exposed on the LAN.
dev:
	@echo "backend  -> http://127.0.0.1:$(PORT)"
	@echo "frontend -> http://localhost:5173"
	@trap 'kill 0' EXIT INT TERM; \
	( cd backend && uv run --frozen uvicorn toolkit_api.main:app --reload --port $(PORT) ) & \
	( cd frontend && API_PORT=$(PORT) npm run dev ) & \
	wait

# Production-style: one process serves the built UI and the API on :$(PORT) (loopback).
start: build
	@echo "serving API + UI -> http://127.0.0.1:$(PORT)"
	cd backend && uv run --frozen uvicorn toolkit_api.main:app --port $(PORT)

# LAN host: build the UI, then serve API + UI from ONE process bound to 0.0.0.0 so every
# device on the same Wi-Fi can reach it at http://<this-machine>.local:$(PORT). The launcher
# prints the real URLs, auto-advances past a busy port, and warns about LAN exposure.
# Overrides: HOST=127.0.0.1 (local-only), PORT=<n> (base port). --frozen: never rewrite the lock.
host: build
	cd backend && PORT=$(PORT) uv run --frozen python -m toolkit_api.host

build:
	cd frontend && npm run build

backend:
	cd backend && uv run --frozen uvicorn toolkit_api.main:app --reload --port $(PORT)

frontend:
	cd frontend && API_PORT=$(PORT) npm run dev

# Tests first: `&&` short-circuits, so ordering decides what you learn when it fails.
# Explicit `src tests` paths, not a bare `ruff check`: an explicit path overrides `exclude`,
# so a stray ignore can't quietly shrink the gate, and a bad `cd` fails loudly with E902.
# `ruff format --check` is enforced too, so drift fails the gate instead of piling up.
#
# The frontend gate used to be `npm run build` alone, which proves almost
# nothing: Vite strips TypeScript types with esbuild WITHOUT checking them, so a
# type-broken app builds clean. `typecheck` (TypeScript 7) is the real gate, and
# lint/test were already written but never run in CI. Ordered cheapest-first.
test:
	cd backend && uv run --frozen pytest -q && uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests
	cd frontend && npm run typecheck && npm run lint && npm run test && npm run build

lint:
	cd backend && uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests
	cd frontend && npm run typecheck && npm run lint

clean:
	rm -rf frontend/dist
	find backend -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
