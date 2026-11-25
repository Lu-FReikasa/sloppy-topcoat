// Signing a release in the browser (NS-01, ARCHITECTURE D80). The key file made by `topcoat-sign
// keygen` is read here, on this page, and never sent anywhere; only the signature lines leave. The
// bytes signed are the ones the node and the hub verify: "topcoat-update-v1\n" + the manifest.

export const KEY_HEADER = "topcoat-update-key-v1";
export const SIG_SCHEME = "topcoat-sig-v1";
export const SIGNED_PREFIX = "topcoat-update-v1\n";

// PKCS#8 of an Ed25519 private key is this fixed header followed by the 32-byte seed.
const PKCS8_PREFIX = Uint8Array.from([
  0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04,
  0x22, 0x04, 0x20,
]);

export class KeyFileError extends Error {}

function fromBase64(text: string): Uint8Array {
  const raw = atob(text);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function toBase64(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function fromBase64Url(text: string): Uint8Array {
  return fromBase64(
    text.replace(/-/g, "+").replace(/_/g, "/") +
      "=".repeat((4 - (text.length % 4)) % 4),
  );
}

/** The seed in a key file (`topcoat-update-key-v1 <base64 seed>`); anything else is refused. */
export function parseKeyFile(text: string): Uint8Array {
  const [head, seed, ...rest] = text.trim().split(/\s+/);
  if (head !== KEY_HEADER || !seed || rest.length > 0) {
    throw new KeyFileError("not a topcoat-sign key file");
  }
  let bytes: Uint8Array;
  try {
    bytes = fromBase64(seed);
  } catch {
    throw new KeyFileError("the key is damaged");
  }
  if (bytes.length !== 32) throw new KeyFileError("the key is damaged");
  return bytes;
}

/** The id of a public key: the first 16 hex digits of its SHA-256 (the same as the node's). */
export async function keyIdOf(pub: Uint8Array): Promise<string> {
  const sum = new Uint8Array(
    await crypto.subtle.digest("SHA-256", pub as BufferSource),
  );
  return Array.from(sum.slice(0, 8), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
}

export interface SigningKey {
  key: CryptoKey;
  keyId: string;
  publicKey: Uint8Array;
}

/** Whether this browser can sign with Ed25519 (Web Crypto). If not, `topcoat-sign` on the PC does it. */
export async function ed25519Supported(): Promise<boolean> {
  try {
    await crypto.subtle.generateKey({ name: "Ed25519" }, false, ["sign"]);
    return true;
  } catch {
    return false;
  }
}

export async function importSigningKey(seed: Uint8Array): Promise<SigningKey> {
  const pkcs8 = new Uint8Array(PKCS8_PREFIX.length + seed.length);
  pkcs8.set(PKCS8_PREFIX);
  pkcs8.set(seed, PKCS8_PREFIX.length);
  const key = await crypto.subtle.importKey(
    "pkcs8",
    pkcs8,
    { name: "Ed25519" },
    true,
    ["sign"],
  );
  pkcs8.fill(0);
  const jwk = await crypto.subtle.exportKey("jwk", key);
  if (!jwk.x) throw new KeyFileError("the public half could not be derived");
  const publicKey = fromBase64Url(jwk.x);
  return { key, publicKey, keyId: await keyIdOf(publicKey) };
}

/** The raw signature of some bytes. */
export async function signBytes(
  key: CryptoKey,
  bytes: Uint8Array,
): Promise<Uint8Array> {
  return new Uint8Array(
    await crypto.subtle.sign({ name: "Ed25519" }, key, bytes as BufferSource),
  );
}

/** The signature line of one manifest, exactly as the update channel reads it. */
export async function signManifest(
  signing: SigningKey,
  manifest: string,
): Promise<string> {
  const message = new TextEncoder().encode(SIGNED_PREFIX + manifest);
  const sig = await signBytes(signing.key, message);
  return `${SIG_SCHEME} ${signing.keyId} ${toBase64(sig)}`;
}
