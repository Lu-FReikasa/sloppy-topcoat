import { render } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it } from "vitest";
import { useScrollportHeight } from "./scrollport";

function Probe() {
  const ref = useRef<HTMLDivElement>(null);
  useScrollportHeight(ref, "--h", true);
  return <div ref={ref} data-testid="probe" />;
}

describe("useScrollportHeight", () => {
  it("leaves out the port's padding, where a sticky panel cannot go", () => {
    const port = document.createElement("div");
    port.style.overflowY = "auto";
    port.style.padding = "12px 0 8px";
    Object.defineProperty(port, "clientHeight", { value: 700 });
    document.body.appendChild(port);
    const { getByTestId } = render(<Probe />, { container: port });
    expect(getByTestId("probe").style.getPropertyValue("--h")).toBe("680px");
    port.remove();
  });
});
