import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import i18n from "../i18n";
import type { Workspace } from "../layout/types";
import { mockFetch } from "../test/mockFetch";
import { SettingsApp } from "./SettingsApp";
import { searchSettings } from "./registry";

const api = () => new ApiClient({ token: "t", fetchImpl: mockFetch({}).fetch });
const ws = (key: string, title: string): Workspace =>
  ({ key, title, title_key: null, icon: "", position: 0, modules: [], builtin: true }) as unknown as Workspace;

describe("searchSettings", () => {
  const t = i18n.t.bind(i18n) as (k: string) => string;
  it("finds an option by its name, a synonym in either language, or its section", () => {
    expect(searchSettings("temperatur", t).map((e) => e.id)).toEqual(["assistant-advanced"]);
    expect(searchSettings("temperature", t).map((e) => e.id)).toEqual(["assistant-advanced"]);
    expect(searchSettings("anrede", t).map((e) => e.id)).toContain("assistant-address");
    expect(searchSettings("", t)).toEqual([]);
    // a hit in the name comes before a hit among the synonyms ("tempo" is a word of quality)
    expect(searchSettings("temp", t).map((e) => e.id)[0]).toBe("assistant-advanced");
  });
});

describe("SettingsApp", () => {
  it("jumps from the search to the option and marks it", async () => {
    render(<SettingsApp api={api()} workspaces={[]} section="household" />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "kompakt" } });
    const found = within(screen.getByRole("list", { name: /Gefundene|found/ }));
    fireEvent.click(found.getByRole("button", { name: /Liste der Unterhaltungen|List of conversations/ }));
    await screen.findByRole("heading", { level: 1, name: /Allgemein|General/ });
    await waitFor(() =>
      expect(document.querySelector('[data-setting="chatlist"]')?.classList.contains("setting-flash")).toBe(true),
    );
  });

  it("lists the applications that have an assistant and opens the one it was asked for", () => {
    render(
      <SettingsApp
        api={api()}
        workspaces={[ws("chat", "Chat"), ws("wiki", "Wiki"), ws("research", "Recherche")]}
        section="apps"
        app="research"
      />,
    );
    const tabs = within(screen.getByRole("tablist")).getAllByRole("tab");
    expect(tabs.map((b) => b.textContent)).toEqual(["Chat", "Recherche"]);
    expect(tabs[1]!.getAttribute("aria-selected")).toBe("true");
  });
});
