# Workshop browser image

The container image behind the `browser.*` capabilities of a dev node
(brief E6-02-workshop-browser). It is built by hand, once, and named in the node's
configuration:

```json
{"tools": {"workshop": {"runner": {"type": "docker", "images": {"browser": "topcoat-workshop-browser:1"}}}}}
```

* The node starts it per session with **no network**, a read-only root file system, all capabilities
  dropped and two mounts: a copy of the worktree (read-only) and the mailbox (`/box`).
* `helper.mjs` serves the built GUI of the worktree copy with a faked hub on the container's own
  loopback, drives Chromium through Playwright, and answers one command at a time from the mailbox.
* The audit, snapshot and fake-hub modules are shared with the layout checks in `web/scripts/`
  (`web/scripts/lib/`); their pure parts have unit tests (`npm test`).

The helper has run against a real Chromium (`node deploy/workshop/browser/selftest.mjs` repeats that run:
fixture page, snapshot, act, audit, animations, refusals). The image build and the node's Docker path have not
been run. `mode: dev` starts the worktree's own vite from a wrapper config in `/tmp` (the worktree is
read-only) and needs `web/node_modules` in the copy. See the test plan and section 6b in
brief E6-02-workshop-browser.
