import { describe, expect, it } from "vitest";
import { SentenceCutter, readable } from "./speech";

describe("SentenceCutter", () => {
  it("hands out each complete sentence once while the answer grows", () => {
    const cut = new SentenceCutter();
    expect(cut.take("Der Container läu")).toEqual([]);
    expect(cut.take("Der Container läuft seit gestern. Er hat")).toEqual([
      "Der Container läuft seit gestern.",
    ]);
    expect(cut.take("Der Container läuft seit gestern. Er hat 3.5 GB")).toEqual([]);
    expect(cut.take("Der Container läuft seit gestern. Er hat 3.5 GB belegt", true)).toEqual([
      "Er hat 3.5 GB belegt",
    ]);
  });

  it("keeps short pieces together and skips what has no words", () => {
    const cut = new SentenceCutter();
    expect(cut.take("Ok. Das ist erledigt.\n---\n", true)).toEqual(["Ok. Das ist erledigt."]);
  });

  it("does not read out code, also while a block is still being written", () => {
    const cut = new SentenceCutter();
    expect(cut.take("Mach das hier zuerst:\n```\ndocker ps. ")).toEqual(["Mach das hier zuerst:"]);
    expect(cut.take("Mach das hier zuerst:\n```\ndocker ps.\n```\nDann ist es gut.", true)).toEqual([
      "Dann ist es gut.",
    ]);
  });

  it("says nothing twice when the text is replaced by a shorter one", () => {
    const cut = new SentenceCutter();
    cut.take("Eine lange Antwort mit einem Satz. ");
    expect(cut.take("Kurz", true)).toEqual([]);
  });

  it("splits lines on CRLF too", () => {
    expect(new SentenceCutter().take("Erste Zeile hier\r\nZweite Zeile hier", true)).toEqual([
      "Erste Zeile hier",
      "Zweite Zeile hier",
    ]);
    expect(readable("a```b```c")).toBe("a\nc");
  });
});
