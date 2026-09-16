SHELL := /bin/bash

# Base port: dev, start, backend and host advance to the first free port at or above it.
PORT ?= 8000

# Backend extras; skipped on Intel Macs (no torch wheel). Override: make install EXTRAS=docmd
EXTRAS ?= $(shell if [ "$$(uname -s)" = "Darwin" ] && [ "$$(uname -m)" = "x86_64" ]; \
                  then echo ""; else echo "docmd watermark"; fi)
EXTRA_FLAGS := $(foreach extra,$(strip $(EXTRAS)),--extra $(extra))

.PHONY: help install dev backend frontend build start host test lint clean

help:
	@echo "make install   install backend (uv) + frontend (npm) dependencies"
	@echo "make dev       run backend (:$(PORT)+) + frontend (:5173) together, hot-reload"
	@echo "make start     build the frontend, then serve API + UI from ONE server (127.0.0.1:$(PORT)+)"
	@echo "make host      build + serve API + UI to the whole LAN (http://<this-machine>.local:$(PORT))"
	@echo "make build     build the frontend for production (frontend/dist)"
	@echo "make test      backend tests + lint + a frontend build check"
	@echo "make clean     remove build artifacts and caches"

install:
	@if [ -z "$(strip $(EXTRAS))" ]; then \
	  echo "note: skipping the MinerU and torch extras on this machine (no macOS x86_64 wheels)."; \
	  echo "      Doc to Markdown will be unavailable and Watermark Remover falls back to its"; \
	  echo "      cv2 inpainter; every other tool works. Hide Doc to Markdown from the UI with"; \
	  echo "      TOOLKIT_DISABLED_TOOLS=doc-to-markdown in backend/.env"; \
	fi
	cd backend && uv sync $(EXTRA_FLAGS)
	cd frontend && npm install

# Loopback only; the port is resolved once, up front, since Vite fixes its /api proxy at boot.
dev:
	@cd backend; \
	FREE=$$(PORT=$(PORT) uv run --frozen python -m toolkit_api.host --free-port) || exit $$?; \
	cd ..; \
	if [ "$$FREE" != "$(PORT)" ]; then echo "⚠  port $(PORT) was busy → backend on $$FREE"; fi; \
	echo "backend  -> http://127.0.0.1:$$FREE"; \
	echo "frontend -> http://localhost:5173"; \
	trap 'kill 0' EXIT INT TERM; \
	( cd backend && uv run --frozen uvicorn toolkit_api.main:app --reload --port $$FREE ) & \
	( cd frontend && API_PORT=$$FREE npm run dev ) & \
	wait

# One process serves UI + API on loopback; a busy port auto-advances.
start: build
	cd backend && HOST=127.0.0.1 PORT=$(PORT) uv run --frozen python -m toolkit_api.host

# Same as start but bound to 0.0.0.0 for the whole LAN; HOST=127.0.0.1 keeps it local.
host: build
	cd backend && PORT=$(PORT) uv run --frozen python -m toolkit_api.host

build:
	cd frontend && npm run build

backend:
	@cd backend; \
	FREE=$$(PORT=$(PORT) uv run --frozen python -m toolkit_api.host --free-port) || exit $$?; \
	if [ "$$FREE" != "$(PORT)" ]; then echo "⚠  port $(PORT) was busy → backend on $$FREE"; fi; \
	echo "backend -> http://127.0.0.1:$$FREE"; \
	uv run --frozen uvicorn toolkit_api.main:app --reload --port $$FREE

frontend:
	cd frontend && API_PORT=$(PORT) npm run dev

# npm run build alone does not typecheck (Vite strips types); explicit ruff paths override exclude.
test:
	cd backend && uv run --frozen pytest -q && uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests
	cd frontend && npm run typecheck && npm run lint && npm run test && npm run build

lint:
	cd backend && uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests
	cd frontend && npm run typecheck && npm run lint

clean:
	rm -rf frontend/dist
	find backend -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
