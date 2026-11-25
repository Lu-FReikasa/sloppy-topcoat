import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { createPublicKey, verify } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import i18n from "../i18n";
import { BROWSER_SIGNING, ReleasesPage, type Signing } from "./ReleasesPage";
import type { ReleaseBuild, ReleasePreview, ReleasesOverview } from "./types";

afterEach(cleanup);
beforeEach(async () => {
  await i18n.changeLanguage("en");
});

const SHA_L = "a".repeat(64);
const SHA_W = "b".repeat(64);

function build(overrides: Partial<ReleaseBuild> = {}): ReleaseBuild {
  return {
    id: "b1",
    kind: "node_release",
    source: "release_repo",
    ref: "main",
    commit: "c".repeat(40),
    version: "0.3.0",
    state: "built",
    node: "build-vm",
    changes: "a1b2c3 Add paging\nd4e5f6 Fix the footer",
    artifacts: [
      {
        kind: "binary",
        name: "topcoat-node-linux-amd64",
        os: "linux",
        arch: "amd64",
        size: 3 << 20,
        sha256: SHA_L,
      },
      {
        kind: "binary",
        name: "topcoat-node-windows-amd64.exe",
        os: "windows",
        arch: "amd64",
        size: 4 << 20,
        sha256: SHA_W,
      },
    ],
    smoke: null,
    rollout: [],
    log: "compiled [masked]",
    error: null,
    channel: null,
    key_id: null,
    created_at: null,
    started_at: null,
    finished_at: null,
    published_at: null,
    ...overrides,
  };
}

function overview(overrides: Partial<ReleasesOverview> = {}): ReleasesOverview {
  return {
    enabled: true,
    channel_enabled: true,
    key_ids: ["0123456789abcdef"],
    rollout_node: "prod-host",
    build_nodes: [{ id: "n1", name: "build-vm", state: "available" }],
    builds: [build()],
    published: [],
    ...overrides,
  };
}

const PREVIEW: ReleasePreview = {
  channel: "stable",
  created: "2026-10-29T12:00:00Z",
  previous_version: "0.2.0",
  key_ids: ["0123456789abcdef"],
  build: build(),
  manifests: [
    {
      platform: "linux-amd64",
      name: "topcoat-node-linux-amd64",
      size: 3 << 20,
      sha256: SHA_L,
      manifest: '{"os":"linux"}',
    },
    {
      platform: "windows-amd64",
      name: "topcoat-node-windows-amd64.exe",
      size: 4 << 20,
      sha256: SHA_W,
      manifest: '{"os":"windows"}',
    },
  ],
};

function fakeApi(data: ReleasesOverview = overview()) {
  return {
    releases: vi.fn(async () => data),
    startReleaseBuild: vi.fn(async () =>
      build({ id: "b2", state: "requested" }),
    ),
    releaseBuild: vi.fn(async () => build()),
    releaseManifests: vi.fn(async () => PREVIEW),
    publishRelease: vi.fn(async () => build({ state: "published" })),
    rolloutRelease: vi.fn(async () => build()),
  };
}

function fakeSigning(
  keyId = "0123456789abcdef",
): Signing & { signed: string[] } {
  const signed: string[] = [];
  return {
    signed,
    supported: async () => true,
    load: async (text) => {
      if (!text.startsWith("topcoat-update-key-v1")) throw new Error("bad");
      return { key: {} as CryptoKey, keyId, publicKey: new Uint8Array(32) };
    },
    sign: async (_key, manifest) => {
      signed.push(manifest);
      return `topcoat-sig-v1 ${keyId} c2ln`;
    },
  };
}

function open(api = fakeApi(), signing: Signing = fakeSigning()) {
  render(<ReleasesPage api={api as unknown as ApiClient} signing={signing} />);
  return api;
}

const keyFile = (text = "topcoat-update-key-v1 AAAA") =>
  new File([text], "release.key");

async function prepare() {
  fireEvent.click(
    await screen.findByRole("button", { name: /Node release 0.3.0/ }),
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Show what will be signed" }),
  );
  await screen.findByLabelText("Key file");
}

describe("ReleasesPage", () => {
  it("says so when releases are switched off", async () => {
    open(fakeApi(overview({ enabled: false })));
    expect(await screen.findByText(/switched off on this hub/)).toBeTruthy();
  });

  it("warns about a missing build node and a missing update key and does not start a build", async () => {
    open(fakeApi(overview({ build_nodes: [], key_ids: [], builds: [] })));
    expect(await screen.findByText(/No build node is connected/)).toBeTruthy();
    expect(screen.getByText(/no update key/)).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Start build" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("starts a node release only with a version and sends exactly what was typed", async () => {
    const api = open(fakeApi(overview({ builds: [] })));
    const start = (await screen.findByRole("button", {
      name: "Start build",
    })) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Version"), {
      target: { value: "latest" },
    });
    expect(start.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Version"), {
      target: { value: "0.3.0" },
    });
    fireEvent.change(screen.getByLabelText("Commit, branch or tag"), {
      target: { value: "release/0.3" },
    });
    fireEvent.change(screen.getByLabelText("Code from"), {
      target: { value: "dev_mirror" },
    });
    fireEvent.click(start);
    await waitFor(() =>
      expect(api.startReleaseBuild).toHaveBeenCalledWith({
        kind: "node_release",
        ref: "release/0.3",
        version: "0.3.0",
        source: "dev_mirror",
      }),
    );
  });

  it("starts a hub image without a version", async () => {
    const api = open(fakeApi(overview({ builds: [] })));
    fireEvent.change(await screen.findByLabelText("What"), {
      target: { value: "hub_image" },
    });
    expect(screen.queryByLabelText("Version")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Start build" }));
    await waitFor(() =>
      expect(api.startReleaseBuild).toHaveBeenCalledWith({
        kind: "hub_image",
        ref: "main",
        version: null,
        source: "release_repo",
      }),
    );
  });

  it("allows one build at a time and says where a build stands", async () => {
    open(fakeApi(overview({ builds: [build({ state: "building" })] })));
    expect(
      (
        (await screen.findByRole("button", {
          name: "Start build",
        })) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(screen.getByText("building")).toBeTruthy();
  });

  it("shows the commit, the changes and the checksums of a build", async () => {
    open();
    fireEvent.click(
      await screen.findByRole("button", { name: /Node release 0.3.0/ }),
    );
    const detail = await screen.findByLabelText("Build");
    expect(within(detail).getByText("c".repeat(40))).toBeTruthy();
    expect(within(detail).getByText(/Add paging/)).toBeTruthy();
    expect(within(detail).getByText(SHA_L)).toBeTruthy();
    expect(within(detail).getByText(SHA_W)).toBeTruthy();
    expect(within(detail).getByText("linux/amd64")).toBeTruthy();
  });

  it("shows why a build failed", async () => {
    open(
      fakeApi(
        overview({
          builds: [
            build({
              state: "failed",
              error: "capability_failed: no such ref",
              artifacts: [],
            }),
          ],
        }),
      ),
    );
    fireEvent.click(await screen.findByRole("button", { name: /failed/ }));
    expect((await screen.findByRole("alert")).textContent).toContain(
      "no such ref",
    );
    expect(
      screen.queryByRole("button", { name: "Show what will be signed" }),
    ).toBeNull();
  });

  it("offers no signing for a hub image: it is rolled out, not published", async () => {
    open(
      fakeApi(
        overview({
          builds: [
            build({
              kind: "hub_image",
              version: null,
              artifacts: [
                {
                  kind: "image",
                  name: "topcoat-hub:local-aaaaaaa",
                  size: 0,
                  digest: "sha256:" + "d".repeat(64),
                },
              ],
              smoke: { ok: true, summary: "health ok" },
            }),
          ],
        }),
      ),
    );
    fireEvent.click(await screen.findByRole("button", { name: /Hub image/ }));
    expect(await screen.findByLabelText("Roll out the hub")).toBeTruthy();
    expect(screen.getByText(/Smoke test passed: health ok/)).toBeTruthy();
    expect(screen.queryByText("Sign and publish")).toBeNull();
  });
});

describe("signing", () => {
  it("shows what will be signed before any key is asked for", async () => {
    const api = open();
    await prepare();
    expect(api.releaseManifests).toHaveBeenCalledWith("b1", "stable");
    expect(
      screen.getByText(
        /Version 0.3.0 goes to the channel stable \(until now: 0.2.0\)/,
      ),
    ).toBeTruthy();
    expect(screen.getAllByText(SHA_L).length).toBeGreaterThan(0);
    expect(
      (
        screen.getByRole("button", {
          name: "Sign and publish",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });

  it("refuses a key the hub does not know and a file that is no key", async () => {
    const api = open(fakeApi(), fakeSigning("ffffffffffffffff"));
    await prepare();
    fireEvent.change(screen.getByLabelText("Key file"), {
      target: { files: [keyFile()] },
    });
    expect((await screen.findByRole("alert")).textContent).toContain(
      "ffffffffffffffff",
    );
    fireEvent.click(screen.getByLabelText(/I checked/));
    expect(
      (
        screen.getByRole("button", {
          name: "Sign and publish",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    fireEvent.change(screen.getByLabelText("Key file"), {
      target: { files: [keyFile("hello")] },
    });
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        "could not be read",
      ),
    );
    expect(api.publishRelease).not.toHaveBeenCalled();
  });

  it("needs the key and the person's confirmation, signs every manifest here and sends only signatures", async () => {
    const signing = fakeSigning();
    const api = open(fakeApi(), signing);
    await prepare();
    fireEvent.change(screen.getByLabelText("Key file"), {
      target: { files: [keyFile("topcoat-update-key-v1 SECRETSEED")] },
    });
    expect((await screen.findByRole("status")).textContent).toContain(
      "0123456789abcdef",
    );
    const go = screen.getByRole("button", {
      name: "Sign and publish",
    }) as HTMLButtonElement;
    expect(go.disabled).toBe(true); // not confirmed yet
    fireEvent.click(screen.getByLabelText(/I checked/));
    expect(go.disabled).toBe(false);
    fireEvent.click(go);
    await waitFor(() => expect(api.publishRelease).toHaveBeenCalledTimes(1));
    expect(signing.signed).toEqual(['{"os":"linux"}', '{"os":"windows"}']);
    const [id, body] = api.publishRelease.mock.calls[0]! as unknown as [
      string,
      Record<string, unknown>,
    ];
    expect(id).toBe("b1");
    expect(body).toEqual({
      channel: "stable",
      created: "2026-10-29T12:00:00Z",
      signatures: {
        "linux-amd64": "topcoat-sig-v1 0123456789abcdef c2ln",
        "windows-amd64": "topcoat-sig-v1 0123456789abcdef c2ln",
      },
    });
    expect(JSON.stringify(body)).not.toContain("SECRETSEED");
    // the key is let go of: signing again needs the file again
    await waitFor(() => expect(screen.queryByLabelText("Key file")).toBeNull());
  });

  it("shows the hub's refusal and keeps the build as it was", async () => {
    const api = fakeApi();
    api.publishRelease.mockRejectedValueOnce(
      new Error("bad_signature: key is not one of the update keys"),
    );
    open(api);
    await prepare();
    fireEvent.change(screen.getByLabelText("Key file"), {
      target: { files: [keyFile()] },
    });
    await screen.findByRole("status");
    fireEvent.click(screen.getByLabelText(/I checked/));
    fireEvent.click(screen.getByRole("button", { name: "Sign and publish" }));
    expect(
      (await screen.findByText(/not one of the update keys/)).textContent,
    ).toContain("bad_signature");
    expect(screen.getByLabelText("Key file")).toBeTruthy();
  });

  it("tells a browser without Ed25519 to use topcoat-sign", async () => {
    open(fakeApi(), { ...fakeSigning(), supported: async () => false });
    fireEvent.click(
      await screen.findByRole("button", { name: /Node release 0.3.0/ }),
    );
    expect(await screen.findByText(/cannot sign with Ed25519/)).toBeTruthy();
  });

  it("signs for real with Web Crypto and the hub could verify it", async () => {
    const SEED =
      "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
    const PUB =
      "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";
    const digest = new Uint8Array(
      await crypto.subtle.digest(
        "SHA-256",
        Uint8Array.from(Buffer.from(PUB, "hex")),
      ),
    );
    const sha = Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join(
      "",
    );
    const keyId = sha.slice(0, 16);
    const api = fakeApi(overview({ key_ids: [keyId] }));
    open(api, BROWSER_SIGNING);
    await prepare();
    const file = keyFile(
      `topcoat-update-key-v1 ${Buffer.from(SEED, "hex").toString("base64")}`,
    );
    fireEvent.change(screen.getByLabelText("Key file"), {
      target: { files: [file] },
    });
    expect((await screen.findByRole("status")).textContent).toContain(keyId);
    fireEvent.click(screen.getByLabelText(/I checked/));
    fireEvent.click(screen.getByRole("button", { name: "Sign and publish" }));
    await waitFor(() => expect(api.publishRelease).toHaveBeenCalled());
    const body = (
      api.publishRelease.mock.calls[0] as unknown as [
        string,
        { signatures: Record<string, string> },
      ]
    )[1];
    const [scheme, id, sig] = body.signatures["linux-amd64"]!.split(" ");
    expect([scheme, id]).toEqual(["topcoat-sig-v1", keyId]);
    const spki = Buffer.concat([
      Buffer.from("302a300506032b6570032100", "hex"),
      Buffer.from(PUB, "hex"),
    ]);
    const pub = createPublicKey({ key: spki, format: "der", type: "spki" });
    expect(
      verify(
        null,
        Buffer.from('topcoat-update-v1\n{"os":"linux"}'),
        pub,
        Buffer.from(sig!, "base64"),
      ),
    ).toBe(true);
  });
});

describe("roll-out of a hub image", () => {
  const image = (rollout: ReleaseBuild["rollout"] = []) =>
    build({
      kind: "hub_image",
      version: null,
      artifacts: [
        {
          kind: "image",
          name: "topcoat-hub:local-aaaaaaa",
          size: 0,
          digest: "sha256:" + "d".repeat(64),
        },
      ],
      smoke: { ok: true, summary: "health ok" },
      rollout,
    });
  const attempt = (dry: boolean, ok = true) => ({
    at: dry ? "2026-10-29T10:00:00+00:00" : "2026-10-29T10:05:00+00:00",
    node: "prod-host",
    dry_run: dry,
    ok,
    output: "== checks done",
  });

  async function openImage(
    b: ReleaseBuild,
    api = fakeApi(overview({ builds: [b] })),
  ) {
    open(api);
    fireEvent.click(await screen.findByRole("button", { name: /Hub image/ }));
    await screen.findByLabelText("Roll out the hub");
    return api;
  }

  it("starts with the check and offers no real roll-out before it passed", async () => {
    const api = await openImage(image());
    const go = screen.getByRole("button", {
      name: "Roll out",
    }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    expect(screen.getByText(/needs a passed check/)).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Check only (dry run)" }),
    );
    await waitFor(() =>
      expect(api.rolloutRelease).toHaveBeenCalledWith("b1", true),
    );
  });

  it("rolls out only after a passed check and the person's own confirmation", async () => {
    const api = await openImage(image([attempt(true)]));
    const go = screen.getByRole("button", {
      name: "Roll out",
    }) as HTMLButtonElement;
    expect(go.disabled).toBe(true); // checked, not confirmed
    fireEvent.click(screen.getByLabelText(/production hub to run this image/));
    expect(go.disabled).toBe(false);
    fireEvent.click(go);
    await waitFor(() =>
      expect(api.rolloutRelease).toHaveBeenCalledWith("b1", false),
    );
  });

  it("does not count a failed check", async () => {
    await openImage(image([attempt(true, false)]));
    fireEvent.click(screen.getByLabelText(/production hub to run this image/));
    expect(
      (screen.getByRole("button", { name: "Roll out" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(screen.getByText(/failed/)).toBeTruthy();
  });

  it("shows the hub's refusal", async () => {
    const api = fakeApi(overview({ builds: [image([attempt(true)])] }));
    api.rolloutRelease.mockRejectedValueOnce(
      new Error("dry_run_first: run the checks first"),
    );
    await openImage(image([attempt(true)]), api);
    fireEvent.click(screen.getByLabelText(/production hub to run this image/));
    fireEvent.click(screen.getByRole("button", { name: "Roll out" }));
    expect((await screen.findByRole("alert")).textContent).toContain(
      "dry_run_first",
    );
  });

  it("offers nothing when no node is named for the roll-out", async () => {
    open(fakeApi(overview({ rollout_node: null, builds: [image()] })));
    fireEvent.click(await screen.findByRole("button", { name: /Hub image/ }));
    expect(
      await screen.findByText(/rolled out on the production host/),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Roll out" })).toBeNull();
  });
});
