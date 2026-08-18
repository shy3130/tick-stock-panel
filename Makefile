SHELL := /usr/bin/env bash

BACKEND_PORT ?= 3018
FRONTEND_PORT ?= 3011
DATA_PROVIDER ?= fquant_local
TDX_DATA_DIR ?= /Volumes/vol3/tdx
AGENT_RUNTIME ?= python
PI_WORKER_STAMP := pi-agent-worker/node_modules/.package-lock.json

.PHONY: help deps pi-deps start start-pi start-local backend frontend stop restart status test typecheck

help:
	@printf '%s\n' \
		'Targets:' \
		'  make deps          install backend/frontend dependencies' \
		'  make pi-deps       install optional Pi worker dependencies' \
		'  make start         start backend + frontend in foreground' \
		'  make start-pi      install Pi deps if needed, then start with Pi runtime' \
		'  make start-local   start with DATA_PROVIDER=fquant_local' \
		'  make backend       start backend only' \
		'  make frontend      start frontend only' \
		'  make stop          stop listeners on BACKEND_PORT/FRONTEND_PORT' \
		'  make restart       stop then start' \
		'  make status        show listeners on service ports' \
		'  make test          run backend tests' \
		'  make typecheck     run frontend typecheck'

deps:
	cd backend && uv sync
	cd frontend && pnpm install

$(PI_WORKER_STAMP): pi-agent-worker/package.json pi-agent-worker/package-lock.json
	cd pi-agent-worker && npm ci --ignore-scripts

pi-deps: $(PI_WORKER_STAMP)

start:
	BACKEND_PORT="$(BACKEND_PORT)" FRONTEND_PORT="$(FRONTEND_PORT)" DATA_PROVIDER="$(DATA_PROVIDER)" AGENT_RUNTIME="$(AGENT_RUNTIME)" ./dev.sh

start-pi: pi-deps
	$(MAKE) start AGENT_RUNTIME=pi

start-local:
	DATA_PROVIDER=fquant_local TDX_DATA_DIR="$(TDX_DATA_DIR)" BACKEND_PORT="$(BACKEND_PORT)" FRONTEND_PORT="$(FRONTEND_PORT)" AGENT_RUNTIME="$(AGENT_RUNTIME)" ./dev.sh

backend:
	cd backend && DATA_PROVIDER="$(DATA_PROVIDER)" TDX_DATA_DIR="$(TDX_DATA_DIR)" AGENT_RUNTIME="$(AGENT_RUNTIME)" uv run uvicorn app.main:app --reload --reload-dir app --host 0.0.0.0 --port "$(BACKEND_PORT)"

frontend:
	cd frontend && pnpm dev --host 0.0.0.0 --port "$(FRONTEND_PORT)"

stop:
	@for port in "$(BACKEND_PORT)" "$(FRONTEND_PORT)"; do \
		pids="$$(lsof -nP -tiTCP:$$port -sTCP:LISTEN 2>/dev/null || true)"; \
		if [ -n "$$pids" ]; then \
			echo "stopping port $$port: $$pids"; \
			kill $$pids 2>/dev/null || true; \
		else \
			echo "port $$port: no listener"; \
		fi; \
	done

restart: stop
	$(MAKE) start

status:
	@for port in "$(BACKEND_PORT)" "$(FRONTEND_PORT)"; do \
		echo "port $$port"; \
		lsof -nP -iTCP:$$port -sTCP:LISTEN 2>/dev/null || echo "  no listener"; \
	done

test:
	cd backend && uv run --extra dev pytest -q

typecheck:
	cd frontend && pnpm tsc --noEmit
