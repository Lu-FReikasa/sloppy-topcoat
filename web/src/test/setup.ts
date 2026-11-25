import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import i18n from "../i18n";

// Most component tests assert the German texts; English tests switch explicitly.
void i18n.changeLanguage("de");

// The tests of the desktop expect the window desktop; the web navigation's tests choose it (the
// browser's default since phase 4, webshell/shell.ts).
beforeEach(() => {
  try {
    window.localStorage.setItem("topcoat.shell", "windows");
  } catch {
    // no storage: the test sees the navigation
  }
});

afterEach(() => {
  cleanup();
  void i18n.changeLanguage("de");
});
