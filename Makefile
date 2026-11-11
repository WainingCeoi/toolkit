.DEFAULT_GOAL := help

PORT ?= 8000
FRONTEND_PORT ?= 5173
# Intel Macs lack the ONNX wheel required by MinerU.
EXTRAS ?= $(shell if [ "$$(uname -sm)" != 'Darwin x86_64' ]; then echo docmd; fi)
TEST_SUITE ?= all
TEST_WORKERS ?= auto

.PHONY: help dev init test

help:
	@printf '%s\n' \
	  'Toolkit' \
	  '  make help  Show commands and options (the default).' \
	  '  make dev   Prepare dependencies and start the API and frontend with hot reload.' \
	  '             PORT=8000 and FRONTEND_PORT=5173 select the ports.' \
	  '             EXTRAS= omits MinerU.' \
	  '  make init  Destructive local reset of Git-ignored configuration, data and files.' \
	  '             Previews changes, requires confirmation, and rebuilds without starting the app.' \
	  '             EXTRAS= omits MinerU.' \
	  '  make test  Run tests, lint, type checks and build with prepared dependencies.' \
	  '             TEST_SUITE=all|backend|frontend selects suites.' \
	  '             TEST_WORKERS=auto|N|0 controls Vitest workers.' \
	  '  See README.md for setup, reset scope and native LAN hosting.'

dev init: export EXTRAS := $(EXTRAS)
dev: export PORT := $(PORT)
dev: export FRONTEND_PORT := $(FRONTEND_PORT)

dev init:
	@python3 -B scripts/dev.py $@

test: export TEST_SUITE := $(TEST_SUITE)
test: export TEST_WORKERS := $(TEST_WORKERS)
test:
	@python3 -B scripts/run_tests.py
