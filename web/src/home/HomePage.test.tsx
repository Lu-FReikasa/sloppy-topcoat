import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { INTENT_EVENT, takeIntent } from "../layout/intent";
import { bootstrap, workspace } from "../test/layoutFixtures";
import { HomePage } from "./HomePage";

describe("HomePage", () => {
  it("lists every application but Start itself, each one click away, with its views", () => {
    const boot = bootstrap([
      workspace("chat", ["page.home", "page.inbox"], { title: "Start" }),
      workspace("research", ["page.research", "page.library"], { title: "Research", icon: "~" }),
    ]);
    render(<HomePage workspaces={boot.workspaces} modules={boot.modules} self="chat" />);
    expect(screen.queryByRole("button", { name: /Start/ })).toBeNull();
    let asked = false;
    window.addEventListener(INTENT_EVENT, () => (asked = true), { once: true });
    fireEvent.click(screen.getByRole("button", { name: /Research/ }));
    expect(asked).toBe(true);
    expect(takeIntent()).toEqual({ module: "page.research", props: {}, program: "research" });
    expect(screen.getByRole("button", { name: "Ablage" })).toBeTruthy();
  });
});
