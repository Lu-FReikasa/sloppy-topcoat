#!/bin/sh
# Pairs the node on the first start (a one-time code from the hub GUI), then runs it.
# A paired node ignores the pairing variables, so they can stay in the compose file.
set -eu

DATA="${TOPCOAT_NODE_DATA_DIR:-/data}"

if [ ! -f "$DATA/node.json" ]; then
  : "${TOPCOAT_HUB_URL:?set TOPCOAT_HUB_URL for the first start}"
  : "${TOPCOAT_PAIR_CODE:?set TOPCOAT_PAIR_CODE (one-time code from the hub GUI) for the first start}"
  : "${TOPCOAT_CA_FINGERPRINT:?set TOPCOAT_CA_FINGERPRINT (sha256:... of the hub CA) for the first start}"
  topcoat-node pair \
    --hub "$TOPCOAT_HUB_URL" \
    --code "$TOPCOAT_PAIR_CODE" \
    --ca-fingerprint "$TOPCOAT_CA_FINGERPRINT" \
    --name "${TOPCOAT_NODE_NAME:-node-cpu}" \
    --data-dir "$DATA"
fi

exec topcoat-node run --data-dir "$DATA"
