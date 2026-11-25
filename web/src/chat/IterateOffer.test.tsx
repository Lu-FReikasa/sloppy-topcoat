import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { IterateOffer } from "./IterateOffer";

const OFFER = {
  task_id: "t1",
  task_number: "1.8.2",
  task_title: "Dev neues Theme schreiben",
  iteration: 5,
  based_on: "r4",
  based_on_number: "1.8.2.4",
  feedback: "Bitte mach eine v5, Hintergrund schwarz",
  requirements: ["dunkle Grundfarbe"],
};

describe("the next iteration offered in the Projekte chat", () => {
  it("starts nothing until the click, then the trimmed feedback on the named version", async () => {
    const iterate = vi.fn().mockResolvedValue({ task: "t1", run: "x" });
    render(<IterateOffer api={{ iterateProjectNode: iterate } as unknown as ApiClient} offer={OFFER} />);
    expect(screen.getByText(/Iteration 5 von 1.8.2/)).toBeTruthy();
    expect(iterate).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Hintergrund schwarz " } });
    fireEvent.click(screen.getByRole("button", { name: "Iteration starten" }));
    await waitFor(() =>
      expect(iterate).toHaveBeenCalledWith("t1", { based_on: "r4", feedback: "Hintergrund schwarz", keep: true }),
    );
    expect(await screen.findByRole("status")).toBeTruthy();
  });
});
