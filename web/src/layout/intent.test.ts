import { afterEach, describe, expect, it } from "vitest";
import { requestOpen, takeIntent } from "./intent";

afterEach(() => {
  takeIntent();
});

describe("requestOpen", () => {
  it("keeps the program a window asked for, next to the module", () => {
    requestOpen({ module: "page.chat", props: {}, program: "dev" });
    expect(takeIntent()).toEqual({
      module: "page.chat",
      props: {},
      program: "dev",
    });
    expect(takeIntent()).toBeNull();
  });

  it("still works without a program", () => {
    requestOpen({ module: "page.node", props: { id: "n1" } });
    expect(takeIntent()).toEqual({ module: "page.node", props: { id: "n1" } });
  });
});
