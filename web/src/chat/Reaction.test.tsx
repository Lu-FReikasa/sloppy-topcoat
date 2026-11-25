import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../types";
import { MessageView } from "./MessageView";

describe("a reaction", () => {
  it("is a small emoji, not a text bubble", () => {
    const message = {
      id: 9,
      role: "assistant",
      content: "👍",
      kind: "reaction",
      status: "complete",
    } as unknown as ChatMessage;
    render(<MessageView message={message} busy={false} />);
    const view = screen.getByTestId("message-reaction");
    expect(view.textContent).toBe("👍");
    expect(screen.queryByTestId("message-assistant")).toBeNull();
  });
});
