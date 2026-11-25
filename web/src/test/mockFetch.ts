import { vi } from "vitest";

export type Handler = (init: RequestInit | undefined, url: string) => Response | Promise<Response>;

export interface RecordedCall {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Route-based fetch mock. Keys are "METHOD /path". Unknown routes return 404.
 * Returns the mock and the list of recorded calls.
 */
export function mockFetch(routes: Record<string, Handler | Response | unknown>) {
  const calls: RecordedCall[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = Object.fromEntries(new Headers(init?.headers).entries());
    let body: unknown = undefined;
    if (typeof init?.body === "string") body = JSON.parse(init.body);
    calls.push({ method, url, headers, body });
    const route = routes[`${method} ${url}`];
    if (route === undefined) return jsonResponse({ detail: "Not Found" }, 404);
    if (typeof route === "function") return (route as Handler)(init, url);
    if (route instanceof Response) return route.clone();
    return jsonResponse(route);
  });
  return { fetch: fn as unknown as typeof fetch, calls };
}
