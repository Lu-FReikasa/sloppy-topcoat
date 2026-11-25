# Test image of the dev workshop for the "pytest" template (D52): build context is the repository
# root, so the hub's lock file is the one the tests need.
#
#   docker build -f deploy/workshop/pytest/Dockerfile -t topcoat-pytest:2 .
#
# The build context needs hub/pyproject.toml, hub/uv.lock, hub/README.md, web/package.json,
# web/package-lock.json and deploy/workshop/.
#
# The workshop runs `pytest -q` in /work as an unprivileged user, without network, with a read-only
# root file system and /tmp as the only writable place (besides /work). `pytest` in this image is a
# small script (run-tests.sh): for the hub repository it starts a throw-away PostgreSQL in /tmp and
# runs the hub tests against it; for any other repository it is plain pytest. The dependencies are
# fixed when the image is built: rebuild it when hub/uv.lock changes. The same goes for the web
# checks (typecheck, lint, tests of web/): Node and the packages of web/package-lock.json are baked
# in, because the container has no network; rebuild when that file changes.
ARG BASE=ubuntu:24.04
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive
# no ready-made database cluster: the test script makes its own in /tmp
RUN mkdir -p /etc/postgresql-common \
    && printf 'create_main_cluster = false\n' > /etc/postgresql-common/createcluster.conf
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3.12 python3.12-venv \
       postgresql postgresql-16-pgvector libnss-wrapper \
    && rm -rf /var/lib/apt/lists/*

RUN python3.12 -m venv /opt/uvtool && /opt/uvtool/bin/pip install --no-cache-dir uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON=/usr/bin/python3.12 UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY hub/pyproject.toml hub/uv.lock hub/README.md ./
RUN /opt/uvtool/bin/uv sync --frozen --no-install-project --group dev \
    && rm -rf /build /root/.cache

COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
WORKDIR /opt/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund && npm cache clean --force

COPY deploy/workshop/pytest/run-tests.sh /usr/local/bin/pytest
RUN chmod 0755 /usr/local/bin/pytest
WORKDIR /work
