/**
 * The registry of data plug-ins (D105, issue #10): the list and one plug-in's detail, kept live by
 * the stream. Every change in the registry (install, update, rollback, disable, remove), from this
 * page or another admin's, is published as `ui_plugins` and reloads both. See `useResource.ts`.
 */
import type { ApiClient } from "../api/client";
import type { PluginDetail, PluginRow } from "../plugins/types";
import type { HubEvents } from "./hubEvents";
import { useResource, type Resource } from "./useResource";

/** The stream event the hub sends for every change in the registry. */
export const PLUGINS_EVENT = "ui_plugins";

const live = { events: [PLUGINS_EVENT] } as const;

export function usePlugins(api: ApiClient, events?: HubEvents): Resource<PluginRow[]> {
  return useResource("plugins", () => api.plugins().then((r) => r.plugins), { live, events });
}

/** One plug-in with its versions and changes; `key` null loads nothing. */
export function usePlugin(
  api: ApiClient,
  key: string | null,
  events?: HubEvents,
): Resource<PluginDetail | null> {
  return useResource(
    `plugin:${key ?? ""}`,
    () => (key ? api.plugin(key) : Promise.resolve(null)),
    { live, events },
  );
}
