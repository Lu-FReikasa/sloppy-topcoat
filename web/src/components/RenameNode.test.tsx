import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { I18nextProvider } from "react-i18next";
import { ApiClient } from "../api/client";
import i18n from "../i18n";
import { mockFetch } from "../test/mockFetch";
import { RenameNode } from "./RenameNode";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function setup(routes: Record<string, unknown>) {
  const mock = mockFetch(routes);
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onRenamed = vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <RenameNode api={api} nodeId="n1" name="node-a" onRenamed={onRenamed} />
    </I18nextProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Umbenennen" }));
  return { mock, onRenamed };
}

describe("RenameNode", () => {
  it("renames after a confirmation and says what it did", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { mock, onRenamed } = setup({
      "POST /api/nodes/n1/rename": { id: "n1", from: "node-a", to: "office-pc", rewritten: {} },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Neuer Name" }), {
      target: { value: "office-pc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Umbenennen" }));
    await waitFor(() => expect(onRenamed).toHaveBeenCalled());
    expect(mock.calls[0]?.body).toEqual({ name: "office-pc" });
    expect(screen.getByRole("status").textContent).toContain("node-a → office-pc");
  });

  it("does not send a name the hub would refuse", () => {
    const { mock } = setup({});
    fireEvent.change(screen.getByRole("textbox", { name: "Neuer Name" }), {
      target: { value: "Office PC" },
    });
    expect(screen.getByText(/Nur Kleinbuchstaben/)).toBeTruthy();
    const submit = screen.getByRole("button", { name: "Umbenennen" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(mock.calls).toHaveLength(0);
  });
});
