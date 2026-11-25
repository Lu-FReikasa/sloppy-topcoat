# Dev loop for sloppy-topcoat. See docs/DEVELOPMENT.md.
COMPOSE    := docker compose -f deploy/docker-compose.dev.yml --env-file deploy/dev.env
NODE_BIN   := node/bin/topcoat-node
HUB_URL    ?= https://localhost:8443
NODE_NAME  ?= fake-1
VERSION    ?= $(shell scripts/node-version.sh)
UPDATE_KEYS ?=
LDFLAGS    := -s -w -X topcoat/node/internal/version.Version=$(VERSION) \
              -X topcoat/node/internal/version.UpdateKeys=$(UPDATE_KEYS)

.PHONY: help dev dev-down db hub-run web-run node-build node-cross fake-node pair \
        check check-fast test test-hub test-node test-web e2e lint lint-hub lint-node lint-web

check: ## everything CI runs except Windows tests and the docker image (scripts/dev/local-ci.sh)
	scripts/dev/local-ci.sh

check-fast: ## quick pre-push check: lint, types, unit tests, no -race/pytest/e2e
	scripts/dev/local-ci.sh --fast

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | sort

deploy/dev.env:
	@echo "copy deploy/dev.env.example to deploy/dev.env and set the values first" && false

dev: deploy/dev.env ## start Postgres + hub + web dev server in docker
	$(COMPOSE) up --build

dev-down: ## stop the docker dev stack
	$(COMPOSE) down

db: deploy/dev.env ## start only Postgres (+pgvector) in docker
	$(COMPOSE) up -d db

hub-run: deploy/dev.env ## run the hub natively on https://localhost:8443 (needs `make db`)
	cd hub && set -a && . ../deploy/dev.env && set +a && uv run python -m topcoat_hub.serve

web-run: ## run the web dev server natively (proxies /api to HUB_URL)
	cd web && npm install && HUB_URL=$(HUB_URL) npm run dev

node-build: ## build the node for this machine
	cd node && CGO_ENABLED=0 go build -trimpath -ldflags "$(LDFLAGS)" -o bin/topcoat-node ./cmd/topcoat-node

node-cross: ## cross-compile the node for linux/amd64 and windows/amd64
	cd node && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "$(LDFLAGS)" \
	  -o bin/topcoat-node-linux-amd64 ./cmd/topcoat-node
	cd node && CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "$(LDFLAGS)" \
	  -o bin/topcoat-node-windows-amd64.exe ./cmd/topcoat-node

pair: node-build ## pair a fake node: make pair CODE=ABCD-1234 FP=sha256:... [NODE_NAME=fake-1]
	$(NODE_BIN) pair --hub $(HUB_URL) --code $(CODE) --ca-fingerprint $(FP) --name $(NODE_NAME) \
	  --data-dir .dev/$(NODE_NAME)

fake-node: node-build ## run a paired fake node: make fake-node [NODE_NAME=fake-1]
	$(NODE_BIN) run --fake --data-dir .dev/$(NODE_NAME)

test: test-hub test-node test-web ## run all unit tests

test-hub:
	cd hub && uv run pytest

test-node:
	cd node && go test ./...

test-web:
	cd web && npm test

e2e: node-build ## end-to-end test: real hub + two fake nodes (needs Postgres)
	cd hub && TOPCOAT_NODE_BIN=$(CURDIR)/$(NODE_BIN) uv run pytest -m integration -v

lint: lint-hub lint-node lint-web ## run all linters

lint-hub:
	cd hub && uv run ruff check . && uv run ruff format --check .

lint-node:
	cd node && go vet ./... && golangci-lint run ./...

lint-web:
	cd web && npm run lint && npm run typecheck
