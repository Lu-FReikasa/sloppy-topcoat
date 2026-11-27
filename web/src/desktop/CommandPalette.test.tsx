import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import en from "../i18n/en.json";
import { SETTINGS } from "../settings/registry";
import { CommandPalette, rank, type PaletteEntry } from "./CommandPalette";

const item = en.settingsApp.item as Record<string, string>;

// the options of Settings as the desktop lists them, with their English names
const settings: PaletteEntry[] = SETTINGS.map((e) => ({
  id: `s-${e.id}`,
  label: item[e.id] ?? e.id,
  hint: "Settings",
  words: e.words,
  quiet: true,
  run: () => undefined,
}));

describe("the palette's ranking", () => {
  it("puts a match in the name above one only in the further words or the hint", () => {
    const entry = { id: "x", label: "Memory on or off", hint: "Settings › Tools", words: "frame", run: () => undefined };
    expect(rank(entry, "memo")).toBe(0);
    expect(rank(entry, "frame")).toBe(1);
    expect(rank(entry, "tools")).toBe(2);
    expect(rank(entry, "memo tools")).toBe(2);
  });

  it('shows "Memory on or off" first for "memo", not "Frame and tools of the applications"', () => {
    render(<CommandPalette entries={settings} onClose={() => undefined} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "memo" } });
    const names = screen.getAllByRole("option").map((o) => o.textContent ?? "");
    expect(names[0]).toContain("Memory on or off");
    // the one that only lists memory among its words still comes, after the names
    const apps = names.findIndex((n) => n.includes("Frame and tools of the applications"));
    expect(apps).toBeGreaterThan(0);
    expect(names[0]).toBe(screen.getByRole("option", { selected: true }).textContent);
  });
});
