import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import i18n from "../i18n";
import type { ActivityItem } from "../types";
import { CheckControls, isPaused } from "./CheckControls";

afterEach(cleanup);

function steer(event: "pause" | "resume", id: number): ActivityItem {
  return {
    id,
    kind: "steer",
    status: "info",
    title: "",
    detail: null,
    data: { event },
    at: "2026-10-21T10:00:00.000Z",
    ms: 0,
  };
}

function setup(items: ActivityItem[] = [], api: Partial<ApiClient> = {}) {
  const fake = {
    steerCheck: vi.fn().mockResolvedValue({ queued: true, text: "x" }),
    pauseCheck: vi.fn().mockResolvedValue({ paused: true }),
    ...api,
  };
  render(
    <I18nextProvider i18n={i18n}>
      <CheckControls api={fake as unknown as ApiClient} messageId={7} items={items} />
    </I18nextProvider>,
  );
  return fake;
}

describe("isPaused", () => {
  it("is decided by the last pause or resume in the chain", () => {
    expect(isPaused([])).toBe(false);
    expect(isPaused([steer("pause", 1)])).toBe(true);
    expect(isPaused([steer("pause", 1), steer("resume", 2)])).toBe(false);
    expect(isPaused([steer("pause", 1), steer("resume", 2), steer("pause", 3)])).toBe(true);
  });
});

describe("CheckControls", () => {
  it("pauses the check of the answer and offers to resume it once it is paused", async () => {
    const api = setup();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(api.pauseCheck).toHaveBeenCalledWith(7, true));
    cleanup();
    const resumed = setup([steer("pause", 1)]);
    expect(screen.getByTestId("check-paused")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Fortsetzen" }));
    await waitFor(() => expect(resumed.pauseCheck).toHaveBeenCalledWith(7, false));
  });

  it("sends a trimmed note and empties the field", async () => {
    const api = setup();
    const send = screen.getByRole("button", { name: "Hinweis senden" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true); // nothing to send yet
    const field = screen.getByLabelText("Hinweis für die laufende Prüfung") as HTMLInputElement;
    fireEvent.change(field, { target: { value: "  schau zuerst in die Logs  " } });
    fireEvent.click(send);
    await waitFor(() => expect(api.steerCheck).toHaveBeenCalledWith(7, "schau zuerst in die Logs"));
    await waitFor(() => expect(field.value).toBe(""));
  });

  it("says so when the check cannot be steered any more", async () => {
    setup([], { steerCheck: vi.fn().mockRejectedValue(new Error("409")) });
    const field = screen.getByLabelText("Hinweis für die laufende Prüfung");
    fireEvent.change(field, { target: { value: "zu spät" } });
    fireEvent.click(screen.getByRole("button", { name: "Hinweis senden" }));
    expect((await screen.findByRole("alert")).textContent).toContain("nicht mehr steuern");
  });
});
