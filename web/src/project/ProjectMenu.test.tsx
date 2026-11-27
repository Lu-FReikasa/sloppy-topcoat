import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import i18n from "../i18n";
import { fakeApi, node, project } from "../test/projectFixtures";
import { menuFocus, ProjectMenu } from "./ProjectMenu";
import { ProjectPage } from "./ProjectPage";

describe("menuFocus", () => {
  it("moves with the arrows, wraps around and jumps to the ends", () => {
    const count = 3;
    const last = count - 1;
    expect(menuFocus(0, "ArrowDown", count)).toBe(1);
    expect(menuFocus(last, "ArrowDown", count)).toBe(0);
    expect(menuFocus(0, "ArrowUp", count)).toBe(last);
    expect(menuFocus(-1, "ArrowUp", count)).toBe(last);
    expect(menuFocus(1, "Home", count)).toBe(0);
    expect(menuFocus(0, "End", count)).toBe(last);
    expect(menuFocus(0, "a", count)).toBeNull();
    expect(menuFocus(0, "ArrowDown", 0)).toBeNull();
  });
});

describe("ProjectMenu", () => {
  const items = () => [
    { key: "a", label: "First", onSelect: vi.fn() },
    { key: "b", label: "Second", onSelect: vi.fn(), checked: true },
    { key: "c", label: "Third", onSelect: vi.fn() },
  ];

  it("opens on the first entry, walks with the keys and runs the picked one", () => {
    const list = items();
    render(<ProjectMenu label="Actions" items={list} />);
    const button = screen.getByRole("button", { name: "Actions" });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    const menu = screen.getByRole("menu", { name: "Actions" });
    expect(document.activeElement).toBe(within(menu).getByText("First"));
    expect(
      within(menu)
        .getByRole("menuitemcheckbox", { name: "Second" })
        .getAttribute("aria-checked"),
    ).toBe("true");
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(within(menu).getByText("Third"));
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    fireEvent.click(document.activeElement!);
    expect(list[0]!.onSelect).toHaveBeenCalledOnce();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("closes on Escape, gives the focus back and keeps Escape from the desktop", () => {
    render(<ProjectMenu label="Actions" items={items()} />);
    const seen: boolean[] = [];
    const desktop = (e: KeyboardEvent) => seen.push(e.defaultPrevented);
    window.addEventListener("keydown", desktop);
    const button = screen.getByRole("button", { name: "Actions" });
    fireEvent.click(button);
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    window.removeEventListener("keydown", desktop);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(button);
    // the desktop minimises a window on an Escape nobody took (Desktop.tsx); this one was taken
    expect(seen.every((prevented) => prevented)).toBe(true);
  });

  it("closes on a click outside", () => {
    render(
      <div>
        <p>elsewhere</p>
        <ProjectMenu label="Actions" items={items()} />
      </div>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Actions" }));
    fireEvent.mouseDown(screen.getByText("elsewhere"));
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("the open node as a side panel (#179)", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the node beside the tree, not inside it, and closes it again", async () => {
    const task = node({ id: "t1", title: "Plan", number: "1" });
    const api = fakeApi({ project: project(), nodes: [task] });
    render(<ProjectPage api={api as unknown as ApiClient} />);
    // the name patterns match a row's accessible name, which holds more than the title
    // ("1 ▸ Plan Task" matches /Plan/, "Topcoat Active" matches /Topcoat/)
    fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Plan/ }));
    const side = await screen.findByRole("complementary", { name: "Details" });
    const tree = screen.getByRole("region", { name: "Tree" });
    expect(within(side).getByRole("heading", { name: /Plan/ })).toBeTruthy();
    expect(tree.contains(side)).toBe(false);
    expect(side.closest(".project")?.classList.contains("has-side")).toBe(true);
    // the back button (narrow windows) and the close button (wide ones) both close it
    fireEvent.click(
      within(side).getByRole("button", { name: "← Back to the tree" }),
    );
    expect(screen.queryByRole("complementary", { name: "Details" })).toBeNull();
    // the focus goes back to the node's row, not to the page
    expect((document.activeElement as HTMLElement).dataset.nodeId).toBe("t1");
    fireEvent.click(screen.getByRole("button", { name: /Plan/ }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Close the details" }),
    );
    expect(screen.queryByRole("complementary", { name: "Details" })).toBeNull();
  });
});
