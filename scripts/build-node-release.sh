#!/usr/bin/env bash
# Cross-compile topcoat-node for the release targets and write SHA256SUMS.
#   scripts/build-node-release.sh <version> <out dir>     (run from node/)
set -euo pipefail
version="$1"
out="$2"
mkdir -p "$out"
ldflags="-s -w -X topcoat/node/internal/version.Version=${version}"
# the hub's public update key(s), so the node can verify what the hub publishes (E2-06); the build
# service passes them in TOPCOAT_UPDATE_KEYS, a build by hand may leave them out
if [ -n "${TOPCOAT_UPDATE_KEYS:-}" ]; then
  ldflags="${ldflags} -X topcoat/node/internal/version.UpdateKeys=${TOPCOAT_UPDATE_KEYS}"
fi
export CGO_ENABLED=0
GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "$ldflags" -o "$out/topcoat-node-linux-amd64" ./cmd/topcoat-node
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "$ldflags" -o "$out/topcoat-node-windows-amd64.exe" ./cmd/topcoat-node
(cd "$out" && sha256sum topcoat-node-linux-amd64 topcoat-node-windows-amd64.exe > SHA256SUMS)
ls -l "$out"
cat "$out/SHA256SUMS"
