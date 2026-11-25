import { afterEach, describe, expect, it } from "vitest";
import { placeOf } from "./ClipperDock";

describe("placeOf", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("names the project and task the window's page shows, not another window's", () => {
    // live 2026-11-20: the project window was opened without props, so Clipper's conversation knew no
    // project and the agent called its tool with the id "p1"
    document.body.innerHTML = `
      <section data-program="project"><div data-place-project="p-uuid" data-place-node="n-uuid"></div></section>
      <section data-program="other"><div data-place-project="x"></div></section>`;
    expect(placeOf("project")).toEqual({ project: "p-uuid", node: "n-uuid" });
    expect(placeOf("missing")).toEqual({});
  });

  it("leaves out what the page does not say", () => {
    document.body.innerHTML = `<section data-program="project"><div data-place-project="p-uuid"></div></section>`;
    expect(placeOf("project")).toEqual({ project: "p-uuid" });
  });
});
