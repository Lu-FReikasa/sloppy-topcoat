import { beforeEach, describe, expect, it } from "vitest";
import {
  applyChatList,
  CHAT_LISTS,
  DEFAULT_CHAT_LIST,
  dayGroup,
  isChatList,
  loadChatList,
} from "./chatListModel";

describe("chatlist", () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete document.documentElement.dataset.chatlist;
  });

  it("defaults to the detailed list", () => {
    expect(DEFAULT_CHAT_LIST).toBe("liste");
    expect(loadChatList()).toBe("liste");
  });

  it("remembers a variant and sets it on the document", () => {
    applyChatList("dicht");
    expect(document.documentElement.dataset.chatlist).toBe("dicht");
    expect(loadChatList()).toBe("dicht");
  });

  it("ignores a stored value that is not a variant", () => {
    window.localStorage.setItem("topcoat.chatlist", "wild");
    expect(loadChatList()).toBe("liste");
    expect(isChatList("wild")).toBe(false);
    expect(CHAT_LISTS.every(isChatList)).toBe(true);
  });

  it("groups by calendar day", () => {
    const now = new Date(2026, 9, 3, 12, 0, 0);
    const at = (d: number, h = 9) => new Date(2026, 9, d, h, 0, 0).toISOString();
    expect(dayGroup(at(3), now)).toBe("today");
    expect(dayGroup(at(3, 0), now)).toBe("today");
    expect(dayGroup(at(2, 23), now)).toBe("yesterday");
    expect(dayGroup(at(1), now)).toBe("week");
    expect(dayGroup(at(1), new Date(2026, 9, 7))).toBe("week");
    expect(dayGroup(at(1), new Date(2026, 9, 8))).toBe("older");
    expect(dayGroup(null, now)).toBe("older");
    expect(dayGroup("not a date", now)).toBe("older");
  });
});
