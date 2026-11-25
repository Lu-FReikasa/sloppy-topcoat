import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { NEAR_PX, nearBottom, useStickToBottom } from "./useStickToBottom";

afterEach(cleanup);

describe("nearBottom", () => {
  it("counts the last NEAR_PX as the end", () => {
    expect(nearBottom({ scrollHeight: 1000, scrollTop: 600, clientHeight: 400 })).toBe(true);
    expect(nearBottom({ scrollHeight: 1000, scrollTop: 600 - NEAR_PX, clientHeight: 400 })).toBe(true);
    expect(nearBottom({ scrollHeight: 1000, scrollTop: 600 - NEAR_PX - 1, clientHeight: 400 })).toBe(
      false,
    );
  });
});

function Harness({ n }: { n: number }) {
  const { ref, onScroll, atBottom, unseen, jump, stick } = useStickToBottom<HTMLDivElement>([n]);
  return (
    <div>
      <div data-testid="box" ref={ref} onScroll={onScroll} />
      <output data-testid="state">{`${atBottom}/${unseen}`}</output>
      <button onClick={jump}>jump</button>
      <button onClick={stick}>stick</button>
    </div>
  );
}

/** jsdom has no layout: give the box the numbers a browser would. */
function layout(box: HTMLElement, o: { scrollHeight: number; clientHeight: number; top: number }) {
  Object.defineProperty(box, "scrollHeight", { value: o.scrollHeight, configurable: true });
  Object.defineProperty(box, "clientHeight", { value: o.clientHeight, configurable: true });
  box.scrollTop = o.top;
}

describe("useStickToBottom", () => {
  it("follows new content while the user is at the end", () => {
    const { rerender } = render(<Harness n={1} />);
    const box = screen.getByTestId("box");
    layout(box, { scrollHeight: 1000, clientHeight: 400, top: 600 });
    rerender(<Harness n={2} />);
    expect(box.scrollTop).toBe(1000); // moved to the end
    expect(screen.getByTestId("state").textContent).toBe("true/false");
  });

  it("stays where the user reads and says something new came in", () => {
    const { rerender } = render(<Harness n={1} />);
    const box = screen.getByTestId("box");
    layout(box, { scrollHeight: 1000, clientHeight: 400, top: 100 });
    fireEvent.scroll(box);
    expect(screen.getByTestId("state").textContent).toBe("false/false");
    rerender(<Harness n={2} />);
    expect(box.scrollTop).toBe(100); // not pulled away
    expect(screen.getByTestId("state").textContent).toBe("false/true");
  });

  it("jump goes to the end and clears the note; scrolling back down clears it too", () => {
    const { rerender } = render(<Harness n={1} />);
    const box = screen.getByTestId("box");
    layout(box, { scrollHeight: 1000, clientHeight: 400, top: 100 });
    fireEvent.scroll(box);
    rerender(<Harness n={2} />);
    fireEvent.click(screen.getByText("jump"));
    expect(box.scrollTop).toBe(1000);
    expect(screen.getByTestId("state").textContent).toBe("true/false");

    layout(box, { scrollHeight: 1000, clientHeight: 400, top: 100 });
    fireEvent.scroll(box);
    rerender(<Harness n={3} />);
    expect(screen.getByTestId("state").textContent).toBe("false/true");
    layout(box, { scrollHeight: 1000, clientHeight: 400, top: 590 });
    act(() => void fireEvent.scroll(box));
    expect(screen.getByTestId("state").textContent).toBe("true/false");
  });
});
