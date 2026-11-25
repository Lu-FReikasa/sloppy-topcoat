import { describe, expect, it } from "vitest";
import { announceProjectChanged, onProjectChanged, projectOfEvent } from "./liveProject";

describe("live project changes", () => {
  it("reads the project of a stream event and tells the open pages", () => {
    expect(projectOfEvent('{"project_id":"p1"}')).toBe("p1");
    expect(projectOfEvent("not json")).toBeNull();
    const seen: string[] = [];
    const off = onProjectChanged((id) => seen.push(id));
    announceProjectChanged("p1");
    off();
    announceProjectChanged("p2");
    expect(seen).toEqual(["p1"]);
  });
});
