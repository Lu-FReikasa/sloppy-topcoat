import { describe, expect, it } from "vitest";
import { parseHubMessage } from "./hubMessage";

describe("parseHubMessage", () => {
  it("reads the chat and the message the hub started", () => {
    expect(parseHubMessage('{"conversation_id":"c1","message_id":7}')).toEqual({
      conversationId: "c1",
      messageId: 7,
    });
  });
  it("ignores anything else", () => {
    expect(parseHubMessage("not json")).toBeNull();
    expect(parseHubMessage('{"conversation_id":"c1"}')).toBeNull();
    expect(parseHubMessage('{"conversation_id":1,"message_id":"7"}')).toBeNull();
  });
});
