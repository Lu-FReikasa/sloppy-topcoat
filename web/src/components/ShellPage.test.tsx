import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import { Desktop } from "../desktop/Desktop";
import { hubUnreachable } from "../errors";
import type { ModuleCtx } from "../layout/modules/render";
import { bootstrap, setNarrow, workspace } from "../test/layoutFixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { WebShell } from "../webshell/WebShell";
import { Login } from "./Login";

function ctx(api: ApiClient): ModuleCtx {
  return {
    api,
    me: { id: "u1", name: "Lu", role: "admin" },
    isAdmin: true,
    nodes: [],
    nodesLoaded: true,
    nodesError: null,
    refreshNodes: () => undefined,
    tick: 0,
    inboxTick: 0,
    dispatch: () => undefined,
  };
}

const BOOT = (() => {
  const b = bootstrap([workspace("archive", ["page.library"], { icon: "#", title: "Archive" })]);
  return {
    ...b,
    prefs: {
      hue: 140,
      look: "terminal",
      desktop: { open: ["archive"], minimised: [], focus: "archive", maximised: null, views: {} },
    },
  };
})();

/** A hub behind a proxy: down (the proxy answers 502) until `up()`. */
function flakyHub() {
  let down = true;
  const mock = mockFetch({
    "GET /api/ui/bootstrap": () =>
      down ? new Response("<html>Bad Gateway</html>", { status: 502, statusText: "Bad Gateway" }) : jsonResponse(BOOT),
    "GET /api/inbox/counts": { unread: 0, urgent: 0, applications: {} },
    "GET /api/artifacts": { artifacts: [] },
  });
  return { api: new ApiClient({ token: "t", fetchImpl: mock.fetch }), up: () => (down = false) };
}

describe("hubUnreachable", () => {
  it("is true for no network and for a proxy that cannot reach the hub, not for the hub's own errors", () => {
    expect(hubUnreachable(new ApiError(0, "network", "Failed to fetch"))).toBe(true);
    expect(hubUnreachable(new ApiError(502, "http_502", "Bad Gateway"))).toBe(true);
    expect(hubUnreachable(new ApiError(504, "http_504", "Gateway Timeout"))).toBe(true);
    expect(hubUnreachable(new ApiError(503, "http_503", "Service Unavailable"))).toBe(true);
    expect(hubUnreachable(new TypeError("Failed to fetch"))).toBe(true);
    // the hub answered: a busy model, a server error, a refusal
    expect(hubUnreachable(new ApiError(503, "model_unavailable", "busy"))).toBe(false);
    expect(hubUnreachable(new ApiError(500, "http_500", "Internal Server Error"))).toBe(false);
    expect(hubUnreachable(new ApiError(403, "forbidden", "no"))).toBe(false);
    expect(hubUnreachable(new Error("boom"))).toBe(false);
  });
});

describe("the trusted shell's pages", () => {
  beforeEach(() => {
    setNarrow(false);
    window.location.hash = "#/w";
  });

  it.each(["windows", "nav"] as const)(
    "says the hub cannot be reached instead of the HTTP status, and comes back on its own (%s)",
    async (shell) => {
      const h = flakyHub();
      const props = {
        api: h.api,
        ctx: ctx(h.api),
        status: "lost" as const,
        onSaveLanguage: () => undefined,
        onLogout: () => undefined,
      };
      const view = (tick: number) =>
        shell === "windows" ? <Desktop {...props} uiTick={tick} /> : <WebShell {...props} routeKey="w" uiTick={tick} />;
      const { rerender, container } = render(view(0));
      const alert = await screen.findByRole("alert");
      expect(screen.getByRole("heading", { name: "Der Hub ist nicht erreichbar" })).toBeTruthy();
      expect(alert.textContent).toContain("Details: Bad Gateway");
      // the status text is a detail, not the message
      expect(screen.getByRole("heading").textContent).not.toContain("Bad Gateway");
      expect(container.querySelector(".shell-page")).not.toBeNull();
      // the stream's return bumps the tick: the page loads by itself
      h.up();
      rerender(view(1));
      await waitFor(() => expect(container.querySelector(".shell-page")).toBeNull());
      expect(screen.queryByRole("heading", { name: "Der Hub ist nicht erreichbar" })).toBeNull();
    },
  );

  it("tries again on the button and shows that it is trying", async () => {
    const h = flakyHub();
    render(
      <Desktop
        api={h.api}
        ctx={ctx(h.api)}
        uiTick={0}
        status="lost"
        onSaveLanguage={() => undefined}
        onLogout={() => undefined}
      />,
    );
    const retry = await screen.findByRole("button", { name: "Noch einmal" });
    h.up();
    fireEvent.click(retry);
    expect(screen.getByRole("button", { name: "Versuche …" })).toHaveProperty("disabled", true);
    await waitFor(() => expect(document.querySelector(".shell-page")).toBeNull());
  });

  it("names a failure of the hub itself as such, with its reason as the detail", async () => {
    const mock = mockFetch({
      "GET /api/ui/bootstrap": () => jsonResponse({ detail: "database is gone" }, 500),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(
      <Desktop api={api} ctx={ctx(api)} uiTick={0} status="live" onSaveLanguage={() => undefined} onLogout={() => undefined} />,
    );
    expect(await screen.findByRole("heading", { name: "Die Arbeitsbereiche konnten nicht geladen werden" })).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("Details: database is gone");
  });

  it("draws the sign-in page in the shell's own dark style", () => {
    const { container } = render(<Login onLogin={() => undefined} />);
    expect(container.querySelector("main.shell-page form.shell-card")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Anmelden" }).className).toContain("primary");
  });
});
