import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { AddNodeDialog, pairCommand, prepareCommand } from "./AddNodeDialog";

describe("AddNodeDialog", () => {
  it("builds pair and install commands with the release binary names", () => {
    const fp = "sha256:" + "ab".repeat(32);
    const hub = "https://hub.example.lan:8443";
    expect(pairCommand({ hub, code: "ABCD-1234", os: "linux", fingerprint: fp })).toBe(
      `./topcoat-node-linux-amd64 pair --hub ${hub} --code ABCD-1234 --ca-fingerprint ${fp}`,
    );
    expect(
      pairCommand({ hub, code: "ABCD-1234", os: "linux", verb: "install", name: "node-nvidia" }),
    ).toBe(
      `sudo ./topcoat-node-linux-amd64 install --hub ${hub} --code ABCD-1234 --name node-nvidia`,
    );
    // PowerShell runs programs from the current folder only with an explicit .\ path.
    expect(
      pairCommand({
        hub,
        code: "ABCD-1234",
        os: "windows",
        fingerprint: fp,
        verb: "install",
        name: "node-amd",
      }),
    ).toBe(
      `.\\topcoat-node-windows-amd64.exe install --hub ${hub} --code ABCD-1234 --ca-fingerprint ${fp} --name node-amd`,
    );
    // A plain-http hub (tests, local dev) needs the explicit opt-in on the node.
    expect(pairCommand({ hub: "http://localhost:5173", code: "ABCD-1234", os: "windows" })).toBe(
      ".\\topcoat-node-windows-amd64.exe pair --hub http://localhost:5173 --code ABCD-1234 --insecure-http",
    );
    expect(prepareCommand("windows")).toBe("Unblock-File .\\topcoat-node-windows-amd64.exe");
    expect(prepareCommand("linux")).toBe("chmod +x topcoat-node-linux-amd64");
  });

  it("switches the OS and adds a valid node name", async () => {
    const mock = mockFetch({
      "POST /api/pairing-codes": {
        code: "ABCD-1234",
        expires_at: "2026-01-01T12:10:00Z",
        hub_url: "https://hub.example.lan",
      },
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<AddNodeDialog api={api} onClose={() => undefined} />);
    const install = await screen.findByTestId("install-command");
    // jsdom's user agent is not Windows: Linux is preselected.
    expect(install.textContent).toBe(
      "sudo ./topcoat-node-linux-amd64 install --hub https://hub.example.lan --code ABCD-1234",
    );
    expect(screen.getByText("Ohne Name verwendet der Node seinen Rechnernamen.")).toBeTruthy();

    fireEvent.click(screen.getByRole("radio", { name: "Windows" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Name des Nodes (optional)" }), {
      target: { value: "node-amd" },
    });
    expect(install.textContent).toBe(
      ".\\topcoat-node-windows-amd64.exe install --hub https://hub.example.lan --code ABCD-1234 --name node-amd",
    );
    expect(screen.getByTestId("pair-command").textContent).toBe(
      ".\\topcoat-node-windows-amd64.exe pair --hub https://hub.example.lan --code ABCD-1234 --name node-amd",
    );
    expect(screen.getByTestId("prepare-command").textContent).toBe(
      "Unblock-File .\\topcoat-node-windows-amd64.exe",
    );

    // Names the hub would reject disable copying.
    fireEvent.change(screen.getByRole("textbox", { name: "Name des Nodes (optional)" }), {
      target: { value: "Node AMD" },
    });
    expect(screen.getByTestId("name-invalid")).toBeTruthy();
    for (const b of screen.getAllByRole("button", { name: "Kopieren" })) {
      expect((b as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it("uses the hub URL and fingerprint from the hub and offers the CA", async () => {
    const fp = "sha256:" + "cd".repeat(32);
    const mock = mockFetch({
      "POST /api/pairing-codes": () =>
        jsonResponse(
          {
            code: "ABCD-1234",
            expires_at: "2026-01-01T12:10:00Z",
            hub_url: "https://hub.example.lan",
            ca_fingerprint: fp,
          },
          201,
        ),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<AddNodeDialog api={api} onClose={() => undefined} origin="https://10.0.0.5:8443" />);
    expect((await screen.findByTestId("install-command")).textContent).toBe(
      `sudo ./topcoat-node-linux-amd64 install --hub https://hub.example.lan --code ABCD-1234 --ca-fingerprint ${fp}`,
    );
    expect(screen.getByTestId("ca-info").textContent).toContain(fp);
    expect(
      screen.getByRole("link", { name: "Hub-Zertifikat (CA) herunterladen" }).getAttribute("href"),
    ).toBe("/api/tls/ca.pem");
    expect(screen.queryByTestId("insecure-warning")).toBeNull();
  });

  it("requests a pairing code and shows code, expiry and command", async () => {
    const mock = mockFetch({
      "POST /api/pairing-codes": () =>
        jsonResponse({ code: "ABCD-1234", expires_at: "2026-01-01T12:10:00Z" }, 201),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<AddNodeDialog api={api} onClose={() => undefined} />);
    expect((await screen.findByTestId("pair-code")).textContent).toBe("ABCD-1234");
    const insecure = window.location.origin.startsWith("http:") ? " --insecure-http" : "";
    expect(screen.getByTestId("pair-command").textContent).toBe(
      `./topcoat-node-linux-amd64 pair --hub ${window.location.origin} --code ABCD-1234${insecure}`,
    );
    expect(screen.getByText(/^Gültig bis /)).toBeTruthy();
    expect(mock.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("copies the command to the clipboard", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    const mock = mockFetch({
      "POST /api/pairing-codes": {
        code: "WXYZ-0001",
        expires_at: "2026-01-01T12:10:00Z",
      },
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<AddNodeDialog api={api} onClose={() => undefined} origin="http://hub.example.lan" />);
    const buttons = await screen.findAllByRole("button", { name: "Kopieren" });
    fireEvent.click(buttons[0]!);
    await waitFor(() => expect(screen.getByRole("button", { name: "Kopiert" })).toBeTruthy());
    expect(writeText).toHaveBeenCalledWith(
      "sudo ./topcoat-node-linux-amd64 install --hub http://hub.example.lan --code WXYZ-0001 --insecure-http",
    );
    expect(screen.getByTestId("insecure-warning")).toBeTruthy();
  });

  it("shows an error and closes on Escape", async () => {
    const mock = mockFetch({
      "POST /api/pairing-codes": () => jsonResponse({ detail: "boom" }, 500),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    const onClose = vi.fn();
    render(<AddNodeDialog api={api} onClose={onClose} />);
    expect((await screen.findByRole("alert")).textContent).toContain("boom");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});
