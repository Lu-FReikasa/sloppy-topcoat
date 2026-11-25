import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "../../i18n";
import { Tabbed } from "./Tabbed";

afterEach(cleanup);

describe("Tabbed", () => {
  it("names a tab after its module, or after its own label when it shows part of one", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <Tabbed
          tabs={[
            { key: "models", module: "page.models", render: () => <p>catalogue</p> },
            {
              key: "measurements",
              module: "page.scheduling",
              labelKey: "layout.tab.measurements",
              render: () => <p>bench</p>,
            },
          ]}
        />
      </I18nextProvider>,
    );
    const tabs = screen.getAllByRole("tab").map((x) => x.textContent);
    expect(tabs).toEqual(["Modelle", "Messungen"]);
    expect(screen.getByText("catalogue")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Messungen" }));
    expect(screen.getByText("bench")).toBeTruthy();
  });
});
