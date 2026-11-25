import { describe, expect, it } from "vitest";
import {
  isProfileRoot,
  readRootProblem,
  readRootsOf,
  suggestedRoots,
  windowsChecksOn,
  withReadRoots,
  withWindowsChecks,
} from "./hostRead";

describe("readRootProblem", () => {
  it.each([
    ["/var/log/app", null],
    ["/var/log/app/", null],
    ["C:\\ProgramData\\topcoat-node\\logs", null],
    ["d:/Logs", null],
    ["", "empty"],
    ["logs", "notAbsolute"],
    ["~/logs", "notAbsolute"],
    ["/", "filesystemRoot"],
    ["C:\\", "filesystemRoot"],
    ["C:/", "filesystemRoot"],
    ["//server/share", "unc"],
    ["\\\\server\\share", "unc"],
    ["\\\\?\\C:\\x", "unc"],
    ["/var/../etc", "dots"],
    ["C:\\a\\.\\b", "dots"],
    ["C:\\a:stream", "stream"],
    ["/" + "x".repeat(600), "tooLong"],
  ])("%s -> %s", (value, expected) => {
    expect(readRootProblem(value)).toBe(expected);
  });
});

describe("profiles and suggestions", () => {
  it("recognises a whole profile folder, not a folder inside it", () => {
    expect(isProfileRoot("C:\\Users\\someone")).toBe(true);
    expect(isProfileRoot("C:\\Users\\someone\\")).toBe(true);
    expect(isProfileRoot("/home/someone")).toBe(true);
    expect(isProfileRoot("/root")).toBe(true);
    expect(isProfileRoot("C:\\Users\\someone\\Documents\\logs")).toBe(false);
    expect(isProfileRoot("/home/someone/projects")).toBe(false);
  });

  it("suggests only Topcoat's own log folder and never a profile or program data folder", () => {
    expect(suggestedRoots("Windows 11").map((s) => s.key)).toEqual(["logs"]);
    expect(suggestedRoots("linux").map((s) => s.key)).toEqual(["logs"]);
    for (const s of [...suggestedRoots("Windows 11"), ...suggestedRoots("linux")]) {
      expect(isProfileRoot(s.path)).toBe(false);
      expect(readRootProblem(s.path)).toBeNull();
    }
  });
});

describe("config changes", () => {
  const base = { roles: ["tools"], allowed_actions: [], capabilities: ["docker.ps"], tools: {} };

  it("folders switch the file capabilities and capability.invoke on, none switch them off", () => {
    const on = withReadRoots(base, ["/srv/logs"]);
    expect(readRootsOf(on)).toEqual(["/srv/logs"]);
    expect(on.capabilities).toEqual(["docker.ps", "files.list", "files.read"]);
    expect(on.allowed_actions).toEqual(["capability.invoke"]);
    const again = withReadRoots(on, ["/srv/logs", "/srv/more"]);
    expect(again.capabilities).toEqual(["docker.ps", "files.list", "files.read"]);
    const off = withReadRoots(again, []);
    expect(off.capabilities).toEqual(["docker.ps"]);
    expect(readRootsOf(off)).toEqual([]);
  });

  it("does not touch other keys and does not mutate its input", () => {
    const copy = JSON.stringify(base);
    withReadRoots(base, ["/srv/logs"]);
    expect(JSON.stringify(base)).toBe(copy);
    expect(withReadRoots(base, ["/srv/logs"]).roles).toEqual(["tools"]);
  });

  it("switches the Windows checks together", () => {
    expect(windowsChecksOn(base)).toBe(false);
    const on = withWindowsChecks(base, true);
    expect(windowsChecksOn(on)).toBe(true);
    expect(on.capabilities).toContain("docker.ps");
    expect(on.allowed_actions).toEqual(["capability.invoke"]);
    expect(windowsChecksOn(withWindowsChecks(on, false))).toBe(false);
    expect(withWindowsChecks(on, false).capabilities).toEqual(["docker.ps"]);
  });
});
