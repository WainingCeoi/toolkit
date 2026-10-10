# Maintenance recipes. The everyday entry point is `make start`.

# Backend extras; skipped on Intel Macs (no torch wheel). Override: make install EXTRAS=docmd
EXTRAS ?= $(shell if [ "$$(uname -s)" = "Darwin" ] && [ "$$(uname -m)" = "x86_64" ]; \
                  then echo ""; else echo "docmd"; fi)
EXTRA_FLAGS := $(foreach extra,$(strip $(EXTRAS)),--extra $(extra))

.PHONY: help-maintenance install dev backend frontend build host test lint clean

help-maintenance:
	@echo "Maintenance commands (also used by CI and coding agents):"
	@echo "  make test       run all tests, lint, type checks, and the frontend build"
	@echo "  make dev        run the API and UI with hot reload"
	@echo "  make host       build and serve Toolkit to the LAN"
	@echo "  make install    prepare locked dependencies without starting Toolkit"
	@echo "  make build      build the frontend"
	@echo "  make lint       run lint, formatting, and type checks"
	@echo "  make backend    run the API with hot reload"
	@echo "  make frontend   run Vite against the API's resolved port"
	@echo "  make clean      remove project build outputs and caches"

install:
	@if [ -z "$(strip $(EXTRAS))" ]; then \
	  echo "note: the MinerU extra is disabled for this install."; \
	  echo "      Doc to Markdown will be unavailable; every other tool works."; \
	  echo "      Hide Doc to Markdown from the UI with"; \
	  echo "      TOOLKIT_DISABLED_TOOLS=doc-to-markdown in backend/.env"; \
	fi
	cd backend && uv sync --locked $(EXTRA_FLAGS)
	cd frontend && npm ci

# Loopback only; the port is resolved once, up front, since Vite fixes its /api proxy at boot.
dev: install
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

# Prepare dependencies before building, including under parallel make.
# HOST=127.0.0.1 keeps this optional LAN command local.
host: install
	@$(MAKE) --no-print-directory build
	cd backend && PORT=$(PORT) uv run --no-sync python -m toolkit_api.host

build:
	cd frontend && npm run build

# The resolved port is left in backend/.port so `make frontend` can aim its proxy at it.
backend:
	@cd backend; \
	FREE=$$(PORT=$(PORT) uv run --frozen python -m toolkit_api.host --free-port) || exit $$?; \
	if [ "$$FREE" != "$(PORT)" ]; then echo "⚠  port $(PORT) was busy → backend on $$FREE"; fi; \
	echo $$FREE > .port; \
	echo "backend -> http://127.0.0.1:$$FREE"; \
	uv run --frozen uvicorn toolkit_api.main:app --reload --port $$FREE

frontend:
	@PROXY=$$(cat backend/.port 2>/dev/null || echo $(PORT)); \
	echo "frontend -> http://localhost:5173 (/api proxied to 127.0.0.1:$$PROXY)"; \
	cd frontend && API_PORT=$$PROXY npm run dev

# npm run build alone does not typecheck (Vite strips types); explicit ruff paths override exclude.
test:
	cd backend && uv run --frozen pytest -q && uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests
	cd frontend && npm run typecheck && npm run lint && npm run test && npm run build

lint:
	cd backend && uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests
	cd frontend && npm run typecheck && npm run lint

clean:
	rm -rf frontend/dist frontend/dist-ssr frontend/coverage backend/.port
	rm -rf .pytest_cache .ruff_cache backend/.pytest_cache backend/.ruff_cache backend/.mypy_cache backend/htmlcov backend/.coverage
	find backend/src backend/tests -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
