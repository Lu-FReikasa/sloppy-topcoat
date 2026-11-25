// @vitest-environment node
import { createPublicKey, verify } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  KeyFileError,
  SIGNED_PREFIX,
  ed25519Supported,
  importSigningKey,
  keyIdOf,
  parseKeyFile,
  signBytes,
  signManifest,
} from "./sign";

const hex = (s: string) =>
  Uint8Array.from(s.match(/../g)!.map((h) => parseInt(h, 16)));
const toHex = (b: Uint8Array) =>
  Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
const b64 = (b: Uint8Array) => Buffer.from(b).toString("base64");

// RFC 8032, section 7.1, test 1 (public test data, not a secret)
const SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
const PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";
const SIG =
  "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b";

describe("signing in the browser", () => {
  it("can sign with Ed25519 here", async () => {
    expect(await ed25519Supported()).toBe(true);
  });

  it("derives the public key from the seed of a key file and signs like the RFC says", async () => {
    const seed = parseKeyFile(`topcoat-update-key-v1 ${b64(hex(SEED))}\n`);
    const signing = await importSigningKey(seed);
    expect(toHex(signing.publicKey)).toBe(PUB);
    expect(toHex(await signBytes(signing.key, new Uint8Array()))).toBe(SIG);
  });

  it("gives a key the id the node and the hub give it", async () => {
    const id = await keyIdOf(hex(PUB));
    expect(id).toMatch(/^[0-9a-f]{16}$/);
    const { createHash } = await import("node:crypto");
    expect(id).toBe(
      createHash("sha256").update(hex(PUB)).digest("hex").slice(0, 16),
    );
  });

  it("writes a signature line over the prefixed manifest that verifies", async () => {
    const signing = await importSigningKey(hex(SEED));
    const manifest = '{"format":1,"product":"topcoat-node","version":"0.3.0"}';
    const line = await signManifest(signing, manifest);
    const [scheme, id, sig] = line.split(" ");
    expect(scheme).toBe("topcoat-sig-v1");
    expect(id).toBe(signing.keyId);
    const spki = Buffer.concat([
      Buffer.from("302a300506032b6570032100", "hex"),
      Buffer.from(signing.publicKey),
    ]);
    const publicKey = createPublicKey({
      key: spki,
      format: "der",
      type: "spki",
    });
    expect(
      verify(
        null,
        Buffer.from(SIGNED_PREFIX + manifest),
        publicKey,
        Buffer.from(sig!, "base64"),
      ),
    ).toBe(true);
    // not the manifest alone, and not another manifest
    expect(
      verify(
        null,
        Buffer.from(manifest),
        publicKey,
        Buffer.from(sig!, "base64"),
      ),
    ).toBe(false);
    expect(
      verify(
        null,
        Buffer.from(SIGNED_PREFIX + manifest + " "),
        publicKey,
        Buffer.from(sig!, "base64"),
      ),
    ).toBe(false);
  });

  it("refuses what is not a key file", () => {
    for (const bad of [
      "",
      "hello",
      "topcoat-update-key-v1",
      "topcoat-update-key-v1 !!!",
      `topcoat-update-key-v1 ${b64(hex(SEED).slice(0, 16))}`,
      `other-key-v1 ${b64(hex(SEED))}`,
      `topcoat-update-key-v1 ${b64(hex(SEED))} extra`,
    ]) {
      expect(() => parseKeyFile(bad), bad).toThrow(KeyFileError);
    }
  });
});
