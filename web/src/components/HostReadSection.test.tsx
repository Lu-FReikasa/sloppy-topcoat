import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { WINDOWS_CAPABILITIES } from "../hostRead";
import { makeDetail } from "../test/fixtures";
import { mockFetch } from "../test/mockFetch";
import { HostReadSection } from "./HostReadSection";

const CONFIG = {
  roles: ["gpu"],
  runner: { type: "llamaserver" },
  allowed_actions: ["models.load"],
  capabilities: [],
  tools: { docker_host: "tcp://127.0.0.1:2375" },
};

function setup(os: string, config: Record<string, unknown> = CONFIG) {
  const mock = mockFetch({ "PUT /api/nodes/n1/config": { config, config_hash: "h" } });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onSaved = vi.fn();
  render(<HostReadSection api={api} node={makeDetail({ id: "n1", os, config })} onSaved={onSaved} />);
  return { mock, onSaved };
}

interface Body {
  tools: Record<string, unknown>;
  capabilities: string[];
  allowed_actions: string[];
  runner: unknown;
}

const input = () => screen.getByRole("textbox", { name: /Ordner freigeben/ });

describe("HostReadSection", () => {
  it("releases nothing by default and says so", () => {
    setup("linux");
    expect(screen.getByText(/Kein Ordner ist freigegeben/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Ordner speichern" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("a suggestion only fills the input; nothing is saved until the person saves", () => {
    const { mock } = setup("Windows 11");
    fireEvent.click(screen.getByRole("button", { name: /Logordner von Topcoat selbst/ }));
    expect((input() as HTMLInputElement).value).toBe("C:\\ProgramData\\topcoat-node\\logs");
    expect(screen.queryByRole("button", { name: /Programmdaten/ })).toBeNull();
    expect(mock.calls).toHaveLength(0);
  });

  it("saving folders switches the file capabilities on and keeps the rest of the config", async () => {
    const { mock, onSaved } = setup("linux");
    fireEvent.change(input(), { target: { value: "/var/log/app" } });
    fireEvent.click(screen.getByRole("button", { name: "Hinzufügen" }));
    expect(mock.calls).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Ordner speichern" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const body = mock.calls[0]?.body as Body;
    expect(body.tools).toEqual({ docker_host: "tcp://127.0.0.1:2375", read_roots: ["/var/log/app"] });
    expect(body.capabilities).toEqual(["files.list", "files.read"]);
    expect(body.allowed_actions).toEqual(["models.load", "capability.invoke"]);
    expect(body.runner).toEqual(CONFIG.runner);
  });

  it("removing the last folder switches the file capabilities off again", async () => {
    const config = {
      ...CONFIG,
      capabilities: ["docker.ps", "files.list", "files.read"],
      tools: { read_roots: ["/var/log/app"] },
    };
    const { mock, onSaved } = setup("linux", config);
    fireEvent.click(screen.getByRole("button", { name: "/var/log/app entfernen" }));
    fireEvent.click(screen.getByRole("button", { name: "Ordner speichern" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const body = mock.calls[0]?.body as Body;
    expect(body.capabilities).toEqual(["docker.ps"]);
    expect(body.tools.read_roots).toBeUndefined();
  });

  it.each([
    ["relative/dir", /absoluten Pfad/],
    ["/", /Wurzel des Dateisystems/],
    ["C:\\", /Wurzel des Dateisystems/],
    ["\\\\server\\share", /UNC/],
    ["/var/../etc", /\.\./],
  ])("refuses %s before it reaches the hub", (value, message) => {
    const { mock } = setup("linux");
    fireEvent.change(input(), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "Hinzufügen" }));
    expect(screen.getByRole("alert").textContent).toMatch(message);
    expect(mock.calls).toHaveLength(0);
  });

  it("warns about a whole user profile", () => {
    setup("Windows 11");
    fireEvent.change(input(), { target: { value: "C:\\Users\\someone" } });
    expect(screen.getByText(/ganzes Benutzerprofil/)).toBeTruthy();
  });

  it("offers the Windows checks only on Windows and switches the eleven scripts together", async () => {
    const { mock, onSaved } = setup("Windows 11");
    fireEvent.click(screen.getByRole("checkbox", { name: /Windows-Lesechecks/ }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const body = mock.calls[0]?.body as Body;
    expect(body.capabilities).toEqual([...WINDOWS_CAPABILITIES]);
    expect(body.allowed_actions).toContain("capability.invoke");
  });

  it("shows no Windows switch on Linux", () => {
    setup("linux");
    expect(screen.queryByRole("checkbox", { name: /Windows-Lesechecks/ })).toBeNull();
  });
});
