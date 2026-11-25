import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AppBadge } from "./AppBadge";

afterEach(cleanup);

describe("AppBadge", () => {
  it("shows the application and whether the task only reads", () => {
    render(<AppBadge application="research" mode="lesen" />);
    const badge = screen.getByTestId("app-badge");
    expect(badge.getAttribute("data-app")).toBe("research");
    expect(badge.getAttribute("data-mode")).toBe("lesen");
    expect(badge.className).not.toContain("is-write");
    expect(badge.textContent).toContain("Recherche");
    expect(badge.textContent).toContain("lesend");
  });

  it("marks an administration task that may write as running with admin rights", () => {
    render(<AppBadge application="admin" mode="schreiben" />);
    const badge = screen.getByTestId("app-badge");
    expect(badge.className).toContain("is-write");
    expect(badge.textContent).toContain("Verwaltung");
    expect(badge.textContent).toContain("schreibend");
    expect(badge.textContent).toContain("mit Admin-Rechten");
  });

  it("shows nothing for a node that is no task", () => {
    render(<AppBadge application={null} />);
    expect(screen.queryByTestId("app-badge")).toBeNull();
  });
});
