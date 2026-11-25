import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { ApiAccess } from "./ApiAccess";

afterEach(() => {
  vi.restoreAllMocks();
});

const KEY = {
  id: "k1",
  name: "editor",
  prefix: "tc_abcdefg",
  created_at: "2026-01-01T12:00:00Z",
  last_used_at: null,
};

describe("ApiAccess", () => {
  it("shows the base URL and existing keys", async () => {
    const mock = mockFetch({ "GET /api/keys": [KEY] });
    render(
      <ApiAccess
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
        origin="https://hub.example.lan:8443"
      />,
    );
    expect(screen.getByTestId("base-url").textContent).toBe("https://hub.example.lan:8443/v1");
    expect(await screen.findByText("editor")).toBeTruthy();
    expect(screen.getByText("tc_abcdefg…")).toBeTruthy();
    expect(screen.getByText("nie")).toBeTruthy();
  });

  it("creates a key and shows it once", async () => {
    const mock = mockFetch({
      "GET /api/keys": [],
      "POST /api/keys": () => jsonResponse({ ...KEY, key: "tc_secret123" }, 201),
    });
    render(<ApiAccess api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} />);
    await screen.findByText("Noch keine API-Schlüssel.");
    fireEvent.change(screen.getByLabelText(/Name des neuen Schlüssels/), {
      target: { value: "editor" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Schlüssel anlegen" }));
    expect((await screen.findByTestId("new-key")).textContent).toBe("tc_secret123");
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({ name: "editor" });
  });

  it("revokes a key after confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const mock = mockFetch({
      "GET /api/keys": [KEY],
      "DELETE /api/keys/k1": () => new Response(null, { status: 204 }),
    });
    render(<ApiAccess api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} />);
    fireEvent.click(await screen.findByRole("button", { name: "Schlüssel editor widerrufen" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "DELETE")).toBe(true));
  });
});
