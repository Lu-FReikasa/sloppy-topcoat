import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import "../i18n";
import en from "../i18n/en.json";
import de from "../i18n/de.json";
import { Clipper, LINES_PER_MOOD, lineOf, moodOf, ticketNumber, type Mood } from "./Clipper";

const MOODS: Mood[] = ["idle", "thinking", "working", "done", "error", "waiting"];

describe("Clipper's mood comes from the chat's state", () => {
  it("work first, then a failure, then a waiting click", () => {
    expect(moodOf({ running: true, checking: false })).toBe("thinking");
    expect(moodOf({ running: true, checking: true, last: "error" })).toBe("working");
    expect(moodOf({ running: false, checking: false, last: "error", waiting: true })).toBe("error");
    expect(moodOf({ running: false, checking: false, last: "complete", waiting: true })).toBe("waiting");
    expect(moodOf({ running: false, checking: false, last: "complete" })).toBe("done");
    expect(moodOf({ running: false, checking: false })).toBe("idle");
  });

  it("the same seed says the same line, and every line exists in both languages", () => {
    expect(lineOf("idle", 5)).toBe(lineOf("idle", 5 + LINES_PER_MOOD));
    for (const mood of MOODS) {
      for (let i = 0; i < LINES_PER_MOOD; i += 1) {
        const [, m, n] = lineOf(mood, i).split(".");
        const pick = (cat: unknown) => (cat as Record<string, Record<string, string[]>>).clipper?.[m ?? ""]?.[Number(n)];
        expect(pick(en), `${mood}.${i} en`).toBeTruthy();
        expect(pick(de), `${mood}.${i} de`).toBeTruthy();
      }
    }
  });

  it("draws the figure with its mood and says its line", () => {
    render(<Clipper mood="error" seed={0} />);
    const fig = screen.getByRole("figure");
    expect(fig.getAttribute("data-mood")).toBe("error");
    expect(fig.className).toContain("clip-error");
    expect(screen.getByRole("status").textContent).not.toBe("");
  });
});

describe("Clipper at work (the desktop plan, 6.3/6.3a)", () => {
  it("holds a ticket while he waits, his application's prop, and a cigarette that burns with the work", () => {
    const { container, rerender } = render(<Clipper mood="working" seed={0} app="research" queue={{ number: 47, ahead: 2 }} />);
    expect(screen.getByTestId("clipper-ticket").textContent).toContain("Nr. 47");
    expect(screen.getByTestId("clipper-ticket").textContent).toContain("noch 2 vor dir");
    expect(screen.getByRole("status").textContent).toBe("Nr. 47. Natürlich.");
    const fig = container.querySelector("figure") as HTMLElement;
    expect(fig.className).toContain("clip-app-research");
    expect(fig.textContent).toContain("🔍");
    rerender(<Clipper mood="working" seed={0} app="research" burn={0.5} />);
    expect(fig.style.getPropertyValue("--burn")).toBe("0.5");
    expect(screen.queryByTestId("clipper-ticket")).toBeNull();
  });

  it("gives a task the same two-digit ticket each time", () => {
    const n = ticketNumber("7df190c2-0748-4384-9044-5918a00a525b");
    expect(n).toBe(ticketNumber("7df190c2-0748-4384-9044-5918a00a525b"));
    expect(n).toBeGreaterThanOrEqual(10);
    expect(n).toBeLessThan(100);
  });
});
