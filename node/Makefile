# topcoat-node developer commands. Run from node/.
GOLANGCI_LINT ?= golangci-lint
VERSION       ?= $(shell ../scripts/node-version.sh)
# UPDATE_KEYS: the public key(s) (base64, comma-separated) that sign updates (E2-06); empty = a build that
# cannot be updated through the hub. Create a key with `topcoat-sign keygen`.
UPDATE_KEYS   ?=
LDFLAGS       := -s -w -X topcoat/node/internal/version.Version=$(VERSION) \
                 -X topcoat/node/internal/version.UpdateKeys=$(UPDATE_KEYS)
BUILD         := CGO_ENABLED=0 go build -trimpath -ldflags "$(LDFLAGS)"

.PHONY: all vet test test-race lint build build-sign build-linux build-windows clean

all: vet lint test build

vet:
	go vet ./...

test:
	go test ./...

# -race needs CGO and a C compiler; the shipped binaries never use CGO.
test-race:
	CGO_ENABLED=1 go test -race ./...

lint:
	$(GOLANGCI_LINT) run ./...
	GOOS=windows $(GOLANGCI_LINT) run ./...

build: build-linux build-windows

# The release tool (runs on the maintainer's PC, not on nodes).
build-sign:
	CGO_ENABLED=0 go build -trimpath -o bin/topcoat-sign ./cmd/topcoat-sign

build-linux:
	GOOS=linux GOARCH=amd64 $(BUILD) -o bin/topcoat-node-linux-amd64 ./cmd/topcoat-node

build-windows:
	GOOS=windows GOARCH=amd64 $(BUILD) -o bin/topcoat-node-windows-amd64.exe ./cmd/topcoat-node

clean:
	rm -rf bin
