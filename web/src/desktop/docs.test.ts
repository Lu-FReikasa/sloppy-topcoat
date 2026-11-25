import { describe, expect, it } from "vitest";
import { routeToModule } from "../layout/modules/meta";
import { docKey, docOf, docOfLibraryId, docPrograms } from "./docs";
import { EMPTY, openProgram, settle } from "./state";

const ID = "4cb80109-48ec-473c-ba34-e67c0e5eb226";

describe("document windows", () => {
  it("have a key per document that a program key and the desktop client accept", () => {
    const key = docKey("report", ID);
    expect(key).toBe(`r-${ID}`);
    expect(key!.length).toBeLessThanOrEqual(40);
    expect(/^[a-z0-9][a-z0-9-]{0,39}$/.test(key!)).toBe(true);
    expect(docOf(key!)).toEqual({ kind: "report", id: ID });
    expect(docKey("artifact", "../x")).toBeNull();
    expect(docOf("research")).toBeNull();
  });

  it("are programs while they are open, several at once, and stay on the desktop", () => {
    const one = docKey("report", ID)!;
    const two = docKey("artifact", "11111111-2222-4333-8444-555555555555")!;
    let s = openProgram(EMPTY, one, { module: "page.document", props: { kind: "report", id: ID, title: "B12" } });
    s = openProgram(s, two, { module: "page.document", props: { kind: "artifact", id: "x" } });
    const programs = docPrograms([s], [], "Dokument");
    expect(programs.map((p) => [p.key, p.title])).toEqual([
      [one, "B12"],
      [two, "Dokument"],
    ]);
    const infos = programs.map((p) => ({ key: p.key, modules: p.modules, first: p.modules[0]! }));
    expect(settle(s, infos, () => true).open).toEqual([one, two]);
  });

  it("open from an address of the Ablage", () => {
    expect(docOfLibraryId(`report:${ID}`)).toEqual({ kind: "report", id: ID });
    expect(routeToModule({ view: "library", id: `report:${ID}` })).toEqual({
      module: "page.document",
      props: { kind: "report", id: ID },
    });
    expect(routeToModule({ view: "library" })).toEqual({ module: "page.library", props: {} });
  });
});
