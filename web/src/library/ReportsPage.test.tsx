import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { Report, ReportDetail } from "../types";
import { DocumentPage } from "./DocumentPage";
import { ReportView, reportHref } from "./ReportsPage";

afterEach(cleanup);

const BODY = `---
title: "Systemd"
---

# Systemd

## Inhalt

- [Vorwort](#vorwort)

## Vorwort

Der Bericht behandelt systemd [1].

## 1. Geschichte

Text.
`;

const REPORT: Report = {
  id: "r1",
  application: "research",
  title: "Systemd und seine Rolle",
  question: "Was ist systemd?",
  language: "de",
  depth: "thorough",
  status: "draft",
  current_version: 2,
  released_version: null,
  task_id: "t1",
  conversation_id: "c1",
  unread: true,
  created_at: "2026-11-04T05:00:00Z",
  updated_at: "2026-11-04T05:10:00Z",
  released_at: null,
};

function detail(over: Partial<ReportDetail> = {}, version = 2, complete = true): ReportDetail {
  return {
    ...REPORT,
    shown: {
      version,
      parent_version: version > 1 ? version - 1 : null,
      kind: version > 1 ? "revision" : "agent",
      note: "",
      content_hash: "h",
      meta: { complete },
      created_at: "2026-11-04T05:10:00Z",
      body: BODY,
    },
    comments: [],
    ...over,
  };
}

function api(over: Partial<Record<keyof ApiClient, unknown>> = {}) {
  return {
    reports: vi.fn().mockResolvedValue({ reports: [REPORT] }),
    report: vi.fn().mockImplementation((_id: string, v?: number) => Promise.resolve(detail({}, v ?? 2))),
    reportVersions: vi.fn().mockResolvedValue({
      versions: [
        { version: 1, parent_version: null, kind: "agent", note: "", content_hash: "a", meta: {}, created_at: "2026-11-04T05:00:00Z" },
        { version: 2, parent_version: 1, kind: "revision", note: "kürzer", content_hash: "b", meta: {}, created_at: "2026-11-04T05:10:00Z" },
      ],
    }),
    reportDiff: vi.fn().mockResolvedValue({ from: 1, to: 2, added: 1, removed: 1, diff: "--- v1\n+++ v2\n-alt\n+neu" }),
    addReportComment: vi.fn().mockResolvedValue({}),
    resolveReportComment: vi.fn().mockResolvedValue({}),
    setReportStatus: vi.fn().mockResolvedValue({ ...REPORT, status: "reviewed" }),
    markReportRead: vi.fn().mockResolvedValue(undefined),
    reviseReport: vi.fn().mockResolvedValue({ task_id: "t9", from_version: 2, open_comments: 3 }),
    deleteReport: vi.fn().mockResolvedValue(undefined),
    ...over,
  } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
}

async function opened(a: ApiClient) {
  render(<ReportView api={a} id="r1" onChanged={() => undefined} />);
  await screen.findByTestId("report-paper");
}

describe("the address", () => {
  it("of a report is one of the Ablage", () => {
    expect(reportHref("r1")).toBe("#/library/report%3Ar1");
  });
});

describe("the open report", () => {
  it("is a page with the contents and is marked read", async () => {
    const a = api();
    await opened(a);
    expect(screen.getByRole("heading", { name: "Systemd und seine Rolle", level: 2 })).toBeTruthy();
    expect(within(screen.getByTestId("report-paper")).getByRole("heading", { name: "Vorwort" })).toBeTruthy();
    expect(screen.getByTestId("report-status").textContent).toBe("Entwurf");
    expect(screen.queryByTestId("report-incomplete")).toBeNull();
    await waitFor(() => expect(a.markReportRead).toHaveBeenCalledWith("r1"));
  });

  it("says that a research was cut short", async () => {
    const a = api({ report: vi.fn().mockResolvedValue(detail({}, 2, false)) });
    await opened(a);
    expect(screen.getByTestId("report-incomplete").textContent).toBe("unvollständig");
  });

  it("takes the person's clicks: review, release, back, withdraw", async () => {
    const a = api();
    await opened(a);
    fireEvent.click(screen.getByRole("button", { name: "Als geprüft markieren" }));
    await waitFor(() => expect(a.setReportStatus).toHaveBeenCalledWith("r1", "reviewed"));
    cleanup();
    const b = api({ report: vi.fn().mockResolvedValue(detail({ status: "reviewed" })) });
    await opened(b);
    fireEvent.click(screen.getByRole("button", { name: "Freigeben" }));
    await waitFor(() => expect(b.setReportStatus).toHaveBeenCalledWith("r1", "released"));
    fireEvent.click(screen.getByRole("button", { name: "Zurück zum Entwurf" }));
    await waitFor(() => expect(b.setReportStatus).toHaveBeenCalledWith("r1", "draft"));
    cleanup();
    const c = api({ report: vi.fn().mockResolvedValue(detail({ status: "released", released_version: 2 })) });
    await opened(c);
    expect(screen.getByText(/freigegeben: Version 2/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Freigabe zurücknehmen" }));
    await waitFor(() => expect(c.setReportStatus).toHaveBeenCalledWith("r1", "draft"));
  });

  it("shows the hub's refusal of a click", async () => {
    const a = api({ setReportStatus: vi.fn().mockRejectedValue(new Error("a draft cannot become released")) });
    await opened(a);
    fireEvent.click(screen.getByRole("button", { name: "Als geprüft markieren" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("cannot become");
  });

  it("deletes only after a confirmation", async () => {
    const a = api();
    await opened(a);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(screen.getByRole("button", { name: "Löschen" }));
    expect(a.deleteReport).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Löschen" }));
    await waitFor(() => expect(a.deleteReport).toHaveBeenCalledWith("r1"));
    confirm.mockRestore();
  });

  it("has the research revise it, with the open comments, and links the task", async () => {
    const a = api();
    await opened(a);
    fireEvent.click(screen.getByRole("button", { name: "Überarbeiten lassen" }));
    await waitFor(() => expect(a.reviseReport).toHaveBeenCalledWith("r1"));
    const note = await screen.findByTestId("report-revising");
    expect(note.textContent).toContain("3 offenen Kommentaren");
    expect(within(note).getByRole("link").getAttribute("href")).toBe("#/tasks/t9");
  });

  it("offers to take back a release while a newer draft is worked on", async () => {
    const a = api({ report: vi.fn().mockResolvedValue(detail({ status: "draft", released_version: 1 })) });
    await opened(a);
    fireEvent.click(screen.getByRole("button", { name: "Freigabe zurücknehmen" }));
    await waitFor(() => expect(a.setReportStatus).toHaveBeenCalledWith("r1", "draft"));
  });

  it("exports the document as HTML and as Markdown", async () => {
    const a = api();
    await opened(a);
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((b: Blob) => (blobs.push(b), "blob:x"));
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    fireEvent.click(screen.getByRole("button", { name: "HTML exportieren" }));
    fireEvent.click(screen.getByRole("button", { name: "Markdown exportieren" }));
    expect(click).toHaveBeenCalledTimes(2);
    expect(blobs[0]!.type).toContain("text/html");
    expect(await blobs[0]!.text()).toContain("<h2 id=\"vorwort\">Vorwort</h2>");
    expect(await blobs[1]!.text()).toBe(BODY);
    click.mockRestore();
  });
});

describe("versions", () => {
  it("lists them, shows an older one without the status buttons, and compares two", async () => {
    const a = api();
    await opened(a);
    fireEvent.click(screen.getByRole("tab", { name: /Versionen/ }));
    const panel = await screen.findByTestId("report-versions");
    expect(within(panel).getByText(/Version 2 · Überarbeitung/).textContent).toContain("kürzer");
    fireEvent.click(within(panel).getByRole("button", { name: "Version 1 anzeigen" }));
    await waitFor(() => expect(a.report).toHaveBeenCalledWith("r1", 1));
    await screen.findByText(/Du liest Version 1; die aktuelle ist 2/);
    expect(screen.queryByRole("button", { name: "Als geprüft markieren" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Aktuelle Version zeigen" }));
    await waitFor(() => expect(screen.queryByText(/Du liest Version 1/)).toBeNull());

    fireEvent.click(screen.getByRole("tab", { name: /Versionen/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Vergleichen" }));
    const diff = await screen.findByTestId("report-diff");
    expect(a.reportDiff).toHaveBeenCalledWith("r1", 1, 2);
    expect(within(diff).getByText("+neu").className).toBe("diff-add");
    expect(within(diff).getByText("-alt").className).toBe("diff-del");
    expect(diff.textContent).toContain("1 Zeilen hinzugefügt, 1 entfernt");
  });
});

describe("comments", () => {
  it("are written at a chapter of the shown version and can be marked as done", async () => {
    const withComment = detail({
      comments: [
        { id: "k1", version: 2, anchor: "vorwort", text: "Quelle fehlt", author: "person", resolved: false, created_at: "2026-11-04T06:00:00Z" },
      ],
    });
    const a = api({ report: vi.fn().mockResolvedValue(withComment) });
    await opened(a);
    fireEvent.click(screen.getByRole("tab", { name: /Kommentare \(1\)/ }));
    const panel = await screen.findByTestId("report-comments");
    expect(within(panel).getByText(/Quelle fehlt/)).toBeTruthy();
    expect(within(panel).getByText("Vorwort", { selector: "strong" })).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Als erledigt markieren" }));
    await waitFor(() => expect(a.resolveReportComment).toHaveBeenCalledWith("r1", "k1", true));

    fireEvent.change(within(panel).getByLabelText("Kapitel"), { target: { value: "1-geschichte" } });
    const add = within(panel).getByRole("button", { name: "Kommentar hinzufügen" });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(panel).getByLabelText("Kommentar"), { target: { value: "Zu kurz" } });
    fireEvent.click(add);
    await waitFor(() =>
      expect(a.addReportComment).toHaveBeenCalledWith("r1", { text: "Zu kurz", anchor: "1-geschichte", version: 2 }),
    );
  });
});

describe("the document window", () => {
  it("shows the report and, once it is deleted, says so instead of leaving", async () => {
    const a = api();
    window.location.hash = "#/w/research";
    render(<DocumentPage api={a} kind="report" id="r1" />);
    await screen.findByTestId("report-paper");
    expect(a.report).toHaveBeenCalledWith("r1", undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Löschen" }));
    await screen.findByText(/Das Dokument wurde gelöscht/);
    expect(window.location.hash).toBe("#/w/research");
    confirm.mockRestore();
  });
});

describe("the document window's wiki button", () => {
  it("shows where the document would go, and takes it over only on the card's click", async () => {
    const a = api({
      wikiAblagePreview: vi.fn().mockResolvedValue({
        kind: "report", id: "r1", section: "Recherche", title: "Systemd", path: "Recherche/Systemd",
        page_id: null, new_page: true, version: 1, unchanged: false, chars: 120,
      }),
      wikiAblageTakeOver: vi.fn().mockResolvedValue({ id: "w1", version: 1 }),
    });
    render(<DocumentPage api={a} kind="report" id="r1" />);
    await screen.findByTestId("report-paper");
    fireEvent.click(screen.getByRole("button", { name: "Ins Wiki übernehmen" }));
    await screen.findByTestId("wiki-take-over-card");
    expect(a.wikiAblagePreview).toHaveBeenCalledWith("report", "r1", "de");
    expect(screen.getByTestId("wiki-take-over-card").textContent).toContain("Recherche/Systemd");
    expect(a.wikiAblageTakeOver).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Übernehmen" }));
    await waitFor(() => expect(a.wikiAblageTakeOver).toHaveBeenCalledWith("report", "r1", "de"));
  });
});
