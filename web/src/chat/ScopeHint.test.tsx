import { cleanup, render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { afterEach, describe, expect, it } from "vitest";
import i18n from "../i18n";
import type { ChatToolsOverview } from "../types";
import { ScopeHint, scopeNodes } from "./ScopeHint";

afterEach(cleanup);

const overview = {
  groups: [],
  nodes: [{ name: "node-amd", groups: ["host"] }, { name: "tools-host", groups: ["docker"] }],
  internet: { node: null },
} as ChatToolsOverview;

describe("scopeNodes", () => {
  it("is every machine with no choice, and only the chosen ones otherwise", () => {
    expect(scopeNodes(overview, { nodes: null, groups: null, internet: null })).toEqual([
      "node-amd",
      "tools-host",
    ]);
    expect(scopeNodes(overview, { nodes: ["node-amd", "gone"], groups: null, internet: null })).toEqual([
      "node-amd",
    ]);
  });
});

describe("ScopeHint", () => {
  it("names the machines a check of this chat can reach", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <ScopeHint overview={overview} scope={{ nodes: ["tools-host"], groups: null, internet: null }} />
      </I18nextProvider>,
    );
    expect(screen.getByTestId("scope-hint").textContent).toContain("tools-host");
    expect(screen.getByTestId("scope-hint").textContent).not.toContain("node-amd");
  });
});
