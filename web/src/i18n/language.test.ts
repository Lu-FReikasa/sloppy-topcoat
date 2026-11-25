import { afterEach, describe, expect, it } from "vitest";
import i18n, { applyLanguage, browserLanguage, cachedLanguage, LANGUAGE_KEY } from ".";

afterEach(() => {
  window.localStorage.removeItem(LANGUAGE_KEY);
});

describe("language selection", () => {
  it("falls back from the browser language to English", () => {
    expect(browserLanguage(["de-AT", "en"])).toBe("de");
    expect(browserLanguage(["fr-FR", "en-US"])).toBe("en");
    expect(browserLanguage(["fr"])).toBe("en");
    expect(browserLanguage([])).toBe("en");
  });

  it("applies and remembers a choice", () => {
    applyLanguage("en");
    expect(i18n.language).toBe("en");
    expect(cachedLanguage()).toBe("en");
    expect(document.documentElement.lang).toBe("en");
    expect(i18n.t("header.addNode")).toBe("Add node");
    applyLanguage("de");
    expect(i18n.t("header.addNode")).toBe("Node hinzufügen");
  });
});
