# The image of the workshop browser (brief E6-02-workshop-browser): a headless Chromium
# with the helper that answers the node's browser.* capabilities. Build it from the repository root:
#
#   docker build -f deploy/workshop/browser/Dockerfile -t topcoat-workshop-browser:1 .
#
# and name it in the dev node's config: tools.workshop.runner.images.browser.
#
# The browser version is pinned here (fonts included), so measurements do not drift between hosts.
# The tag was seen on the registry on 2026-10-29; the image itself has not been built yet. After the build:
#   docker run --rm --network none topcoat-workshop-browser:1 node /opt/helper/selftest.mjs
ARG PLAYWRIGHT_VERSION=1.56.0

# the GUI as of this build: a worktree without web/dist (the agent did not build it) is shown with
# this one, so a theme can still be looked at (the theme comes from the mailbox, not from the build)
FROM node:22-alpine AS gui
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble
ARG PLAYWRIGHT_VERSION

# the helper and the shared audit modules; the page logic is the same code the unit tests run
COPY deploy/workshop/browser/helper.mjs /opt/helper/helper.mjs
COPY deploy/workshop/browser/selftest.mjs /opt/helper/selftest.mjs
COPY web/scripts/lib/ /opt/helper/lib/
COPY --from=gui /web/dist /opt/helper/gui
# the Playwright package at exactly the version the browsers in this image belong to
RUN npm install --prefix /opt/helper --no-audit --no-fund playwright@${PLAYWRIGHT_VERSION}
# the node starts the container with its own, fixed command; nothing here listens on the network
USER pwuser
CMD ["node", "/opt/helper/helper.mjs"]
