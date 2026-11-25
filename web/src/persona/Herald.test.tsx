import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../i18n";
import { Herald, SHOW_MS } from "./Herald";
import { noticeOf, signalTask } from "./heraldNews";

type Global = { topcoatClient?: object };

afterEach(() => {
  delete (window as unknown as Global).topcoatClient;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const ended = (over: Record<string, unknown> = {}) => ({
  event: "agent_task" as const,
  data: { id: "t1", status: "done", title: "Recherche zu Alien", origin: "tasks", parent_id: null, seconds: 90, ...over },
});

describe("which ends the herald tells", () => {
  it("tells the end of a task the person started, not a step, not a quick chat check", () => {
    expect(noticeOf(ended())).toMatchObject({ id: "t1", failed: false, title: "Recherche zu Alien" });
    expect(noticeOf(ended({ status: "failed" }))?.failed).toBe(true);
    expect(noticeOf(ended({ status: "running" }))).toBeNull();
    expect(noticeOf(ended({ parent_id: "p" }))).toBeNull();
    expect(noticeOf(ended({ origin: "chat", seconds: 12 }))).toBeNull();
    expect(noticeOf(ended({ origin: "chat", seconds: 120 }))).not.toBeNull();
  });
});

describe("Herald", () => {
  it("asks the client to show it, says the news, opens the task on a click and goes again", () => {
    vi.useFakeTimers();
    (window as unknown as Global).topcoatClient = { config: { key: "ctrl+alt+Space" } };
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign } as Location);
    render(<Herald />);
    expect(screen.queryByTestId("herald")).toBeNull();
    // a page that starts sends a herald the page before it left open away
    expect(assign).toHaveBeenCalledWith("?client=herald-hide");

    act(() => signalTask("agent_task", ended().data));
    const herald = screen.getByTestId("herald");
    expect(herald.textContent).toContain("„Recherche zu Alien“ ist fertig");
    expect(assign).toHaveBeenCalledWith("?client=herald-show");

    fireEvent.click(herald);
    expect(assign).toHaveBeenCalledWith("?client=open-task&id=t1");

    act(() => vi.advanceTimersByTime(SHOW_MS + 1000));
    expect(screen.queryByTestId("herald")).toBeNull();
    expect(assign).toHaveBeenLastCalledWith("?client=herald-hide");
  });
});
