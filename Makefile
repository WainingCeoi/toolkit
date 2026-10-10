# Everyday use: make start. Stop with Ctrl-C.
SHELL := /bin/bash
.DEFAULT_GOAL := help
PORT ?= 8000

.PHONY: help start

help:
	@echo "Toolkit"
	@echo "  make start             prepare dependencies, build, and start locally"
	@echo "  Ctrl-C                 stop Toolkit"
	@echo "  make help-maintenance  show development and maintenance commands"

# Complete setup before the build, including under parallel make.
start: install
	@$(MAKE) --no-print-directory build
	cd backend && HOST=127.0.0.1 PORT=$(PORT) uv run --no-sync python -m toolkit_api.host

include make/maintenance.mk
