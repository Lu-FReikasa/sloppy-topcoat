import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { IssueCard } from "./IssueCard";

describe("an issue in a chat", () => {
  it("filed by its prefix: undo puts it in the trash", async () => {
    const trash = vi.fn().mockResolvedValue({});
    render(
      <IssueCard
        api={{ trashProjectNode: trash } as unknown as ApiClient}
        issue={{ filed: true, task_id: "t9", project_id: "p1", number: "2.4", title: "Fenster springt", similar: [] }}
      />,
    );
    expect(screen.getByText("Issue 2.4")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Rückgängig" }));
    await waitFor(() => expect(trash).toHaveBeenCalledWith("t9"));
    expect(await screen.findByRole("status")).toBeTruthy();
  });

  it("offered for a complaint: filed only on the click, with the trimmed words", async () => {
    const file = vi.fn().mockResolvedValue({ task: { id: "t1", number: "2.5", project_id: "p1" }, similar: [] });
    render(
      <IssueCard
        api={{ fileIssue: file } as unknown as ApiClient}
        issue={{ filed: false, text: "Der Export geht nicht mehr", application: "project", conversation: "c1" }}
      />,
    );
    expect(file).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Export der Akte geht nicht " } });
    fireEvent.click(screen.getByRole("button", { name: "Als Issue anlegen" }));
    await waitFor(() =>
      expect(file).toHaveBeenCalledWith({ text: "Export der Akte geht nicht", application: "project", conversation: "c1" }),
    );
    expect(await screen.findByText("Issue 2.5")).toBeTruthy();
  });
});
