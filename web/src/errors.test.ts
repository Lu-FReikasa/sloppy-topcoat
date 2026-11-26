import { describe, expect, it } from "vitest";
import { ApiError } from "./api/client";
import { describeError, toActionError } from "./errors";
import i18n from "./i18n";

const t = i18n.getFixedT("en");
const TOO_MANY_REQUESTS = 429;
const HUB_BUSY = "All places for your programs are taken";

describe("describeError", () => {
  it("shows a node's own busy reason", () => {
    const text = describeError(
      {
        code: "busy",
        message: "node is busy gaming; models cannot be loaded now",
      },
      t,
      i18n,
    );
    expect(text).toBe(
      "Error: node is busy gaming; models cannot be loaded now",
    );
  });

  it("keeps the catalogue text for the hub's own 429 busy", () => {
    const text = describeError(
      {
        code: "busy",
        message: "too many runs are going",
        status: TOO_MANY_REQUESTS,
      },
      t,
      i18n,
    );
    expect(text).toContain(HUB_BUSY);
    expect(text).not.toContain("too many runs");
  });

  it("keeps the catalogue text for a node busy without a message", () => {
    expect(describeError({ code: "busy", message: "" }, t, i18n)).toContain(
      HUB_BUSY,
    );
  });

  it("keeps the catalogue text for a hub 429 busy that was thrown", () => {
    const thrown = new ApiError(
      TOO_MANY_REQUESTS,
      "busy",
      "at most 2 researches run at once",
    );
    const text = describeError(toActionError(thrown), t, i18n);
    expect(text).toContain(HUB_BUSY);
    expect(text).not.toContain("researches");
  });

  it("uses the catalogue for other known codes and the message for unknown ones", () => {
    expect(describeError({ code: "node_offline", message: "x" }, t, i18n)).toBe(
      t("errors.node_offline"),
    );
    expect(
      describeError({ code: "no_such_code", message: "boom" }, t, i18n),
    ).toBe("Error: boom");
  });
});

describe("toActionError", () => {
  it("keeps the hub's HTTP status", () => {
    expect(
      toActionError(new ApiError(TOO_MANY_REQUESTS, "busy", "full")),
    ).toEqual({
      code: "busy",
      message: "full",
      status: TOO_MANY_REQUESTS,
    });
  });

  it("adds no status to errors that carry none", () => {
    expect(toActionError({ code: "busy", message: "m" })).toEqual({
      code: "busy",
      message: "m",
    });
    expect(toActionError(new Error("boom"))).toEqual({
      code: "unknown",
      message: "boom",
    });
  });
});
