import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import { BUTLER, library, persona, QUESTIONNAIRE } from "./fixtures";
import { PersonaPrompt } from "./PersonaPrompt";
import { Wizard } from "./Wizard";

afterEach(() => {
  vi.restoreAllMocks();
});

const PREVIEW = {
  role: "butler",
  components: { humor: 1 },
  text: "Du bist mein Haus-Assistent.",
  tokens: 12,
  samples: ["Das Backup ist durch.", "Das weiß ich noch nicht."],
};

function routes(extra: Record<string, unknown> = {}) {
  return {
    "GET /api/personas/questionnaire": QUESTIONNAIRE,
    "POST /api/personas/questionnaire/preview": PREVIEW,
    "POST /api/personas/questionnaire/confirm": persona({ id: "new", name: "Meine Persona" }),
    "PUT /api/personas/wizard": { state: "skipped" },
    ...extra,
  };
}

function open(extra: Record<string, unknown> = {}) {
  const mock = mockFetch(routes(extra));
  const onClose = vi.fn();
  const onDone = vi.fn();
  render(
    <Wizard
      api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
      onClose={onClose}
      onDone={onDone}
    />,
  );
  return { mock, onClose, onDone };
}

describe("Wizard", () => {
  it("walks through three scenes, six short questions and a preview, then confirms", async () => {
    const { mock, onDone, onClose } = open();
    await screen.findByText(/Szene 1/);
    const next = () => screen.getByRole("button", { name: "Weiter" });
    expect((next() as HTMLButtonElement).disabled).toBe(true); // a scene needs an answer
    fireEvent.click(screen.getByLabelText("Antwort B1"));
    fireEvent.click(next());
    fireEvent.click(await screen.findByLabelText("Keine passt")); // "none fits" is an answer
    fireEvent.click(next());
    fireEvent.click(await screen.findByLabelText("Antwort C3"));
    fireEvent.click(next());
    // the short questions have their defaults; change one
    expect((screen.getByLabelText("Humor?") as HTMLSelectElement).value).toBe("1");
    fireEvent.change(screen.getByLabelText("Humor?"), { target: { value: "0" } });
    fireEvent.click(next());
    const preview = await screen.findByTestId("wizard-preview");
    await waitFor(() => expect(within(preview).getByText("Das Backup ist durch.")).toBeTruthy());
    expect(within(preview).getByText("Das weiß ich noch nicht.")).toBeTruthy();
    const answers = {
      scenes: { plex: "b", network: "none", everything: "c" },
      address: "du",
      humor: "0",
    };
    expect(mock.calls.filter((c) => c.url.endsWith("/preview")).at(-1)?.body).toEqual({ answers });
    fireEvent.click(screen.getByRole("button", { name: "Das bin ich, übernehmen" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(mock.calls.find((c) => c.url.endsWith("/confirm"))?.body).toEqual({ answers });
    expect(onClose).toHaveBeenCalled();
  });

  it("can be put away at any step: later and skip", async () => {
    const first = open();
    await screen.findByText(/Szene 1/);
    fireEvent.click(screen.getByRole("button", { name: "Überspringen" }));
    await waitFor(() => expect(first.onClose).toHaveBeenCalled());
    expect(first.mock.calls.find((c) => c.method === "PUT")?.body).toEqual({ state: "skipped" });
    expect(first.mock.calls.some((c) => c.url.endsWith("/confirm"))).toBe(false);
  });

  it("later is recorded as later and nothing is created", async () => {
    const { mock, onClose } = open({ "PUT /api/personas/wizard": { state: "later" } });
    await screen.findByText(/Szene 1/);
    fireEvent.click(screen.getByRole("button", { name: "Später" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(mock.calls.find((c) => c.method === "PUT")?.body).toEqual({ state: "later" });
  });

  it("goes back without losing the answers", async () => {
    open();
    await screen.findByText(/Szene 1/);
    fireEvent.click(screen.getByLabelText("Antwort A1"));
    fireEvent.click(screen.getByRole("button", { name: "Weiter" }));
    await screen.findByText(/Szene 2/);
    fireEvent.click(screen.getByRole("button", { name: "Zurück" }));
    expect((screen.getByLabelText("Antwort A1") as HTMLInputElement).checked).toBe(true);
  });
});

describe("PersonaPrompt", () => {
  function mount(lib = library(), extra: Record<string, unknown> = {}) {
    const mock = mockFetch({ "GET /api/personas": lib, ...routes(extra) });
    render(<PersonaPrompt api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} />);
    return mock;
  }

  it("opens the wizard for a new user", async () => {
    mount(library({ wizard: { state: null, show: true, banner: false } }));
    expect(await screen.findByTestId("wizard")).toBeTruthy();
    expect(screen.queryByTestId("persona-banner")).toBeNull();
  });

  it("shows only a banner to an existing user, never the wizard", async () => {
    mount(library({ wizard: { state: null, show: false, banner: true } }));
    expect(await screen.findByTestId("persona-banner")).toBeTruthy();
    expect(screen.queryByTestId("wizard")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Einrichten" }));
    expect(await screen.findByTestId("wizard")).toBeTruthy();
  });

  it("the banner can be closed for now or skipped for good", async () => {
    const mock = mount(library({ wizard: { state: null, show: false, banner: true } }));
    const banner = await screen.findByTestId("persona-banner");
    fireEvent.click(within(banner).getByRole("button", { name: "Später" }));
    expect(screen.queryByTestId("persona-banner")).toBeNull();
    expect(mock.calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("skip tells the hub", async () => {
    const mock = mount(library({ wizard: { state: "later", show: false, banner: true } }));
    const banner = await screen.findByTestId("persona-banner");
    fireEvent.click(within(banner).getByRole("button", { name: "Überspringen" }));
    await waitFor(() =>
      expect(mock.calls.find((c) => c.method === "PUT")?.body).toEqual({ state: "skipped" }),
    );
  });

  it("shows nothing to a user who has a persona", async () => {
    const mock = mount(library({ default_id: BUTLER.id }));
    await waitFor(() => expect(mock.calls.some((c) => c.url === "/api/personas")).toBe(true));
    expect(screen.queryByTestId("persona-banner")).toBeNull();
    expect(screen.queryByTestId("wizard")).toBeNull();
  });
});
