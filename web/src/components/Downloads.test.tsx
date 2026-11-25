import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import type { NodeEvent } from "../types";
import { DownloadDialog, DownloadList, downloadArgs } from "./Downloads";

function setup(actionRoute: unknown = { ok: true, result: { started: true } }) {
  const mock = mockFetch({ "POST /api/nodes/n1/actions": actionRoute });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onClose = vi.fn();
  render(<DownloadDialog api={api} nodeId="n1" onClose={onClose} />);
  return { mock, onClose };
}

function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("downloadArgs", () => {
  it("omits empty optional fields and trims", () => {
    expect(downloadArgs(" o/r ", "f.gguf", " ", "")).toEqual({
      source: { type: "huggingface", repo: "o/r", file: "f.gguf" },
    });
    expect(downloadArgs("o/r", "f.gguf", "main", "mein-modell")).toEqual({
      source: { type: "huggingface", repo: "o/r", file: "f.gguf", revision: "main" },
      target_name: "mein-modell",
    });
  });
});

describe("DownloadDialog", () => {
  it("posts models.download and closes", async () => {
    const { mock, onClose } = setup();
    const submit = screen.getByRole("button", { name: "Herunterladen" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fill(/Hugging Face Repo/, "Qwen/Qwen2.5-1.5B-Instruct-GGUF");
    fill(/^Datei/, "qwen2.5-1.5b-instruct-q4_k_m.gguf");
    fill(/Revision/, "main");
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(mock.calls[0]?.body).toEqual({
      name: "models.download",
      args: {
        source: {
          type: "huggingface",
          repo: "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
          file: "qwen2.5-1.5b-instruct-q4_k_m.gguf",
          revision: "main",
        },
      },
    });
  });

  it("validates repo and file", () => {
    setup();
    fill(/Hugging Face Repo/, "nur-ein-name");
    fill(/^Datei/, "model.bin");
    expect(screen.getByText("Format: Besitzer/Repository")).toBeTruthy();
    expect(screen.getByText("Die Datei muss auf „.gguf“ enden.")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Herunterladen" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("shows the node's error and stays open", async () => {
    const { onClose } = setup({ ok: false, error: { code: "invalid_args", message: "bad file" } });
    fill(/Hugging Face Repo/, "o/r");
    fill(/^Datei/, "f.gguf");
    fireEvent.click(screen.getByRole("button", { name: "Herunterladen" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Fehler: bad file");
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("DownloadList", () => {
  const ev = (id: number, data: Record<string, unknown>): NodeEvent => ({
    id,
    kind: "download_progress",
    data,
    ts: "2026-01-01T12:00:00Z",
    received_at: "",
  });

  it("renders running, done and failed downloads", () => {
    render(
      <DownloadList
        events={[
          ev(3, { model: "a", status: "running", downloaded_bytes: 500_000_000, total_bytes: 2_000_000_000 }),
          ev(2, { model: "b", status: "done", downloaded_bytes: 1_117_320_736, total_bytes: 1_117_320_736 }),
          ev(1, { model: "c", status: "failed", downloaded_bytes: 0, total_bytes: 0, error: "sha256 mismatch" }),
        ]}
      />,
    );
    expect(screen.getByText("500 MB / 2,0 GB (25 %)")).toBeTruthy();
    expect(screen.getByRole("progressbar", { name: "a" }).getAttribute("aria-valuenow")).toBe("25");
    expect(screen.getByText("fertig")).toBeTruthy();
    expect(screen.getByText("fehlgeschlagen")).toBeTruthy();
    expect(screen.getByText("sha256 mismatch")).toBeTruthy();
  });

  it("renders nothing without downloads", () => {
    const { container } = render(<DownloadList events={[]} />);
    expect(container.textContent).toBe("");
  });
});
