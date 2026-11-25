import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { ChatSettings as Settings } from "../types";
import { ChatSettings } from "./ChatSettings";

/** A hub that remembers what was saved, like the real one. */
function server(initial?: Partial<Settings["user"]>) {
  const state: Settings = {
    user: {
      system_prompt: "Be brief.",
      temperature: 0.5,
      address: "du",
      memory_enabled: true,
      quality: "balanced",
      internet: true,
      ...initial,
    },
    hub: {},
    effective_model: "qwen-7b",
  };
  const mock = mockFetch({
    "GET /api/chat/settings": () => jsonResponse(state),
    "PUT /api/chat/settings": (init?: RequestInit) => {
      state.user = JSON.parse(String(init?.body)) as Settings["user"];
      return jsonResponse(state);
    },
  });
  return { state, mock };
}

function show(mock: ReturnType<typeof server>["mock"]) {
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  return render(<ChatSettings api={api} />);
}

const puts = (
  mock: ReturnType<typeof server>["mock"],
  url = "/api/chat/settings",
) => mock.calls.filter((c) => c.method === "PUT" && c.url === url);

describe("ChatSettings", () => {
  it("has separate sections for chat and memory, each with its scope and Save", async () => {
    const { mock } = server();
    show(mock);
    const chat = await screen.findByTestId("settings-chat");
    const memory = screen.getByTestId("settings-memory");
    expect(within(chat).getByRole("heading", { name: "Chat" })).toBeTruthy();
    expect(within(chat).getByText("Nur für dich")).toBeTruthy();
    expect(within(memory).getByText("Nur für dich")).toBeTruthy();
    for (const section of [chat, memory]) {
      const save = within(section).getByRole("button", {
        name: "Speichern",
      }) as HTMLButtonElement;
      expect(save.disabled).toBe(true); // nothing changed yet
    }
  });

  it("keeps the system prompt and the temperature under Erweitert, closed unless one is set", async () => {
    const { mock } = server({ system_prompt: null, temperature: null });
    const { unmount } = show(mock);
    const chat = await screen.findByTestId("settings-chat");
    const advanced = chat.querySelector("details.settings-advanced") as HTMLDetailsElement;
    expect(advanced.open).toBe(false);
    expect(within(advanced).getByText("Erweitert")).toBeTruthy();
    unmount();
    const { mock: set } = server({ temperature: 0.4 });
    show(set);
    const again = await screen.findByTestId("settings-chat");
    expect((again.querySelector("details.settings-advanced") as HTMLDetailsElement).open).toBe(true);
  });

  it("has no hub section: the hub's models are in Administration", async () => {
    const { mock } = server();
    show(mock);
    await screen.findByTestId("settings-chat");
    expect(screen.queryByTestId("settings-hub")).toBeNull();
  });

  it("shows the memory switch left of its text and keeps a change after Save and reload", async () => {
    const { mock } = server();
    const first = show(mock);
    const memory = await screen.findByTestId("settings-memory");
    const box = within(memory).getByLabelText(
      "Der Assistent führt Notizen über mich",
    ) as HTMLInputElement;
    expect(box.checked).toBe(true);
    expect(box.closest("label")?.className).toContain("check");
    expect(box.nextElementSibling?.textContent).toBe(
      "Der Assistent führt Notizen über mich",
    );

    fireEvent.click(box);
    expect(within(memory).getByText("Ungespeicherte Änderungen")).toBeTruthy();
    fireEvent.click(within(memory).getByRole("button", { name: "Speichern" }));
    expect(await within(memory).findByText("Gespeichert ✓")).toBeTruthy();
    expect(puts(mock)).toHaveLength(1);
    expect(puts(mock)[0]?.body).toMatchObject({ memory_enabled: false });

    // switching the tab away and back: a fresh page load reads what the hub stored
    first.unmount();
    show(mock);
    const again = (await screen.findByLabelText(
      "Der Assistent führt Notizen über mich",
    )) as HTMLInputElement;
    expect(again.checked).toBe(false);
  });

  it("saves a section without persisting the unsaved edits of another one", async () => {
    const { mock, state } = server();
    show(mock);
    const chat = await screen.findByTestId("settings-chat");
    const memory = screen.getByTestId("settings-memory");
    fireEvent.change(within(chat).getByLabelText("Dein System-Prompt"), {
      target: { value: "Something else entirely" },
    });
    fireEvent.click(
      within(memory).getByLabelText("Der Assistent führt Notizen über mich"),
    );
    fireEvent.click(within(memory).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(puts(mock)).toHaveLength(1));
    expect(puts(mock)[0]?.body).toEqual({
      system_prompt: "Be brief.", // the saved value, not the draft of the other section
      temperature: 0.5,
      address: "du",
      memory_enabled: false,
      quality: "balanced",
      internet: true,
    });
    expect(state.user.memory_enabled).toBe(false);
    // the chat draft is untouched and still marked as unsaved
    expect(
      (within(chat).getByLabelText("Dein System-Prompt") as HTMLTextAreaElement)
        .value,
    ).toBe("Something else entirely");
    expect(within(chat).getByText("Ungespeicherte Änderungen")).toBeTruthy();
  });

  it("never sends the read-only persona_id back (the hub refuses it with 422)", async () => {
    const { mock } = server({
      persona_id: "11111111-1111-1111-1111-111111111111",
    });
    show(mock);
    const memory = await screen.findByTestId("settings-memory");
    fireEvent.click(
      within(memory).getByLabelText("Der Assistent führt Notizen über mich"),
    );
    fireEvent.click(within(memory).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(puts(mock)).toHaveLength(1));
    expect(puts(mock)[0]?.body).not.toHaveProperty("persona_id");
  });

  it("saves the chat section with all its fields", async () => {
    const { mock } = server();
    show(mock);
    const chat = await screen.findByTestId("settings-chat");
    fireEvent.change(within(chat).getByLabelText("Anrede"), {
      target: { value: "sie" },
    });
    fireEvent.change(within(chat).getByLabelText(/Temperatur/), {
      target: { value: "0,8" },
    });
    fireEvent.click(within(chat).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(puts(mock)).toHaveLength(1));
    expect(puts(mock)[0]?.body).toEqual({
      system_prompt: "Be brief.",
      temperature: 0.8,
      address: "sie",
      memory_enabled: true,
      quality: "balanced",
      internet: true,
    });
    expect(await within(chat).findByText("Gespeichert ✓")).toBeTruthy();
  });

  it("saves the default quality of the user", async () => {
    const { mock } = server();
    show(mock);
    const chat = await screen.findByTestId("settings-chat");
    fireEvent.change(within(chat).getByLabelText("Qualität neuer Chats"), {
      target: { value: "fast" },
    });
    fireEvent.click(within(chat).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(puts(mock)).toHaveLength(1));
    expect((puts(mock)[0]?.body as { quality: string }).quality).toBe("fast");
  });

  it("goes back to 'saved' state when an edit is undone and shows nothing for a fresh page", async () => {
    const { mock } = server();
    show(mock);
    const memory = await screen.findByTestId("settings-memory");
    expect(within(memory).queryByText("Ungespeicherte Änderungen")).toBeNull();
    expect(within(memory).queryByText("Gespeichert ✓")).toBeNull();
    const box = within(memory).getByLabelText(
      "Der Assistent führt Notizen über mich",
    );
    fireEvent.click(box);
    fireEvent.click(box);
    expect(within(memory).queryByText("Ungespeicherte Änderungen")).toBeNull();
    expect(
      (
        within(memory).getByRole("button", {
          name: "Speichern",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });

  it("rejects a temperature outside 0..2 without calling the hub", async () => {
    const { mock } = server();
    show(mock);
    const chat = await screen.findByTestId("settings-chat");
    fireEvent.change(within(chat).getByLabelText(/Temperatur/), {
      target: { value: "3" },
    });
    expect(within(chat).getByRole("alert").textContent).toContain(
      "zwischen 0 und 2",
    );
    expect(
      (
        within(chat).getByRole("button", {
          name: "Speichern",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(puts(mock)).toHaveLength(0);
  });

  it("shows a save error in its own section", async () => {
    const { mock } = server();
    mock.calls.length = 0;
    const failing = mockFetch({
      "GET /api/chat/settings": {
        user: {
          system_prompt: null,
          temperature: null,
              address: "du",
          memory_enabled: true,
        },
        hub: {},
        effective_model: "qwen-7b",
      },
        "PUT /api/chat/settings": () =>
        jsonResponse(
          { detail: { code: "invalid", message: "not saved" } },
          422,
        ),
    });
    show(failing);
    const memory = await screen.findByTestId("settings-memory");
    fireEvent.click(
      within(memory).getByLabelText("Der Assistent führt Notizen über mich"),
    );
    fireEvent.click(within(memory).getByRole("button", { name: "Speichern" }));
    expect((await within(memory).findByRole("alert")).textContent).toContain(
      "not saved",
    );
    expect(
      within(screen.getByTestId("settings-chat")).queryByRole("alert"),
    ).toBeNull();
    expect(within(memory).getByText("Ungespeicherte Änderungen")).toBeTruthy();
  });

  it("saves the default of the Internet switch with the chat section", async () => {
    const { mock, state } = server();
    show(mock);
    const chat = await screen.findByTestId("settings-chat");
    const box = within(chat).getByLabelText(
      /Internet ist in neuen Chats eingeschaltet/,
    );
    expect((box as HTMLInputElement).checked).toBe(true);
    fireEvent.click(box);
    fireEvent.click(within(chat).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(puts(mock)).toHaveLength(1));
    expect(puts(mock)[0]?.body).toMatchObject({ internet: false });
    expect(state.user.internet).toBe(false);
  });
});
