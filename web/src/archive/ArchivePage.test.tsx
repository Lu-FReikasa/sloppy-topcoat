import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import "../i18n";
import { ArchivePage } from "./ArchivePage";

afterEach(() => {
  vi.restoreAllMocks();
});

const chat = (id: string, title: string, updated: string) => ({
  id,
  title,
  model: null,
  node: null,
  created_at: updated,
  updated_at: updated,
});

describe("ArchivePage", () => {
  it("lists every conversation with a link, and searches by word", async () => {
    const mock = mockFetch({
      "GET /api/conversations": [
        chat("c1", "Proxmox backup", "2020-01-02T10:00:00Z"),
        chat("c2", "", "2020-01-01T10:00:00Z"),
      ],
      "GET /api/conversations?q=backup": [
        chat("c1", "Proxmox backup", "2020-01-02T10:00:00Z"),
      ],
    });
    render(
      <ArchivePage
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
      />,
    );
    const link = await screen.findByRole("link", { name: "Proxmox backup" });
    expect(link.getAttribute("href")).toBe("#/chat/c1");
    expect(screen.getAllByRole("link")).toHaveLength(2);
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "backup" },
    });
    await waitFor(() => expect(screen.getAllByRole("link")).toHaveLength(1));
    expect(
      mock.calls.some((c) => c.url === "/api/conversations?q=backup"),
    ).toBe(true);
  });

  it("shows the conversations of every application and filters by one", async () => {
    const mock = mockFetch({
      "GET /api/conversations": [
        { ...chat("c1", "Alien", "2020-01-02T10:00:00Z"), application: "research" },
        { ...chat("c2", "Plex", "2020-01-01T10:00:00Z"), application: "admin" },
        { ...chat("c3", "Hallo", "2020-01-01T09:00:00Z"), application: "chat" },
      ],
    });
    render(<ArchivePage api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} />);
    await screen.findByRole("link", { name: "Alien" });
    expect(screen.getAllByRole("link")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: /Recherche/ }));
    await waitFor(() => expect(screen.getAllByRole("link")).toHaveLength(1));
    expect(screen.getByRole("link", { name: "Alien" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Alle/ }));
    await waitFor(() => expect(screen.getAllByRole("link")).toHaveLength(3));
  });

  it("opens on the conversations of its application, and on all of them in Chat", async () => {
    const rows = [
      { ...chat("c1", "Alien", "2020-01-02T10:00:00Z"), application: "research" },
      { ...chat("c2", "Plex", "2020-01-01T10:00:00Z"), application: "admin" },
    ];
    const api = new ApiClient({ token: "t", fetchImpl: mockFetch({ "GET /api/conversations": rows }).fetch });
    const { unmount } = render(<ArchivePage api={api} application="research" />);
    await screen.findByRole("link", { name: "Alien" });
    expect(screen.getAllByRole("link")).toHaveLength(1);
    unmount();
    render(<ArchivePage api={api} application="chat" />);
    await screen.findByRole("link", { name: "Plex" });
    expect(screen.getAllByRole("link")).toHaveLength(2);
  });

  it("says when there is nothing", async () => {
    const mock = mockFetch({ "GET /api/conversations": [] });
    render(
      <ArchivePage
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
      />,
    );
    expect(await screen.findByText(/Noch keine Chats/)).toBeTruthy();
  });
});
