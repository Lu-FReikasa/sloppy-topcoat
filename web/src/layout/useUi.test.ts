import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { UiBootstrap, UiPrefs } from "./types";
import { LAZY_MS, useUi } from "./useUi";

function boot(prefs: Partial<UiPrefs>): UiBootstrap {
  return { workspaces: [], modules: [], prefs } as unknown as UiBootstrap;
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("useUi", () => {
  it("drops a load that started before a change of the page (the click stays)", async () => {
    const first = boot({ hue: 10 });
    const late = deferred<UiBootstrap>();
    const uiBootstrap = vi.fn().mockResolvedValueOnce(first).mockReturnValueOnce(late.promise);
    const putUiPrefs = vi.fn(async (p: Partial<UiPrefs>) => ({ hue: 10, ...p }) as UiPrefs);
    const api = { uiBootstrap, putUiPrefs } as unknown as ApiClient;

    const { result, rerender } = renderHook(({ tick }) => useUi(api, tick), { initialProps: { tick: 0 } });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    // the stream says something changed: a load goes out ...
    rerender({ tick: 1 });
    // ... and the person changes something before it comes back
    await act(() => result.current.setPrefs({ hue: 200 }));
    expect(result.current.prefs?.hue).toBe(200);

    // the old load arrives last: it must not put the old choice back
    await act(async () => late.resolve(boot({ hue: 10 })));
    expect(result.current.prefs?.hue).toBe(200);
  });

  it("sends lazy changes once, together, after a moment", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const uiBootstrap = vi.fn().mockResolvedValue(boot({ hue: 10 }));
    const putUiPrefs = vi.fn(async (p: Partial<UiPrefs>) => ({ hue: 10, ...p }) as UiPrefs);
    const api = { uiBootstrap, putUiPrefs } as unknown as ApiClient;

    const { result } = renderHook(() => useUi(api, 0));
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      void result.current.setPrefs({ hue: 50 }, { lazy: true });
      void result.current.setPrefs({ hue: 60 }, { lazy: true });
      void result.current.setPrefs({ hue: 70 }, { lazy: true });
    });
    expect(result.current.prefs?.hue).toBe(70); // shown at once
    expect(putUiPrefs).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(LAZY_MS + 10);
    });
    expect(putUiPrefs).toHaveBeenCalledTimes(1);
    expect(putUiPrefs).toHaveBeenCalledWith({ hue: 70 });
  });
});
