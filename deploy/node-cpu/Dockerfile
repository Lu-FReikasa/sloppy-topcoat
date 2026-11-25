# A model node without a GPU in a container (CPU only), e.g. for the embedding model on an
# always-on server. Build context is the repository root:
#   docker build -f deploy/node-cpu/Dockerfile -t sloppy-topcoat-node-cpu:local .
# The node connects OUTBOUND to the hub and opens no port; llama-server inside binds to the
# loopback only. The node downloads the pinned official llama.cpp CPU build into its data volume
# on first start (it needs internet access once, like every model node).
ARG GO_VERSION=1.27
FROM golang:${GO_VERSION} AS build
WORKDIR /src/node
COPY node/go.mod node/go.sum ./
RUN go mod download
COPY node/ ./
ARG VERSION=0.1.0-dev
RUN CGO_ENABLED=0 go build -trimpath \
    -ldflags "-s -w -X topcoat/node/internal/version.Version=${VERSION}" \
    -o /out/topcoat-node ./cmd/topcoat-node

# The llama.cpp release is built on Ubuntu: glibc based image, not Alpine.
FROM ubuntu:24.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 libstdc++6 tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/topcoat-node /usr/local/bin/topcoat-node
COPY deploy/node-cpu/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh
# The data dir holds the node key, the pinned hub CA, the llama.cpp runtime and the models.
ENV TOPCOAT_NODE_DATA_DIR=/data
VOLUME /data
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
