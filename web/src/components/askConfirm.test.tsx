import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../i18n";

const client = vi.hoisted(() => ({ on: false }));
vi.mock("../webshell/shell", () => ({ inClient: () => client.on }));

import { askConfirm } from "./askConfirm";

afterEach(() => {
  client.on = false;
  vi.restoreAllMocks();
});

describe("askConfirm", () => {
  it("asks the browser in a plain browser", async () => {
    const native = vi.spyOn(window, "confirm").mockReturnValue(true);
    await expect(askConfirm("Delete?")).resolves.toBe(true);
    expect(native).toHaveBeenCalledWith("Delete?");
  });

  it("asks in the page in the desktop client, never with a native dialog", async () => {
    client.on = true;
    const native = vi.spyOn(window, "confirm");
    let answer: Promise<boolean> | undefined;
    act(() => {
      answer = askConfirm("Delete the model?");
    });
    expect(screen.getByRole("alertdialog", { name: "Delete the model?" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Ja" }));
    await expect(answer).resolves.toBe(true);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    act(() => {
      answer = askConfirm("Again?");
    });
    fireEvent.click(screen.getByRole("button", { name: "Abbrechen" }));
    await expect(answer).resolves.toBe(false);
    expect(native).not.toHaveBeenCalled();
  });
});
