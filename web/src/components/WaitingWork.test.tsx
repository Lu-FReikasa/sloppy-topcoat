import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ActivityView } from "../types";
import { WaitingWork } from "./MachineTable";

const activity = (poolWaiting: ActivityView["pool_waiting"]): ActivityView =>
  ({
    nodes: [],
    roles: [],
    background_nodes: [],
    digest: { running: 0, waiting: 0 },
    pool_waiting: poolWaiting,
  }) as unknown as ActivityView;

describe("WaitingWork", () => {
  it("lists the requests that wait for a free machine of their pool, in order", () => {
    render(
      <WaitingWork
        activity={activity([
          { place: 1, seconds: 5, ahead: 0, source: "agent", nodes: ["node-a"], kind: "agent", model: "big" },
          { place: 2, seconds: 90, ahead: 1, source: "background", nodes: ["node-a", "node-b"], kind: "memory" },
        ])}
      />,
    );
    const box = within(screen.getByTestId("activity-pool-waiting"));
    expect(box.getByText("Warten auf eine freie Maschine: 2")).toBeTruthy();
    expect(box.getAllByRole("listitem")).toHaveLength(2);
    expect(box.getByText(/0 davor/)).toBeTruthy();
    expect(box.getByText(/1 davor/)).toBeTruthy();
  });

  it("shows nothing while nothing waits", () => {
    const { container } = render(<WaitingWork activity={activity([])} />);
    expect(container.textContent).toBe("");
  });
});
