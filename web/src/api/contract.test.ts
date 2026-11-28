// @vitest-environment node
// The web client against the hub's committed OpenAPI document (issue #207): every path and method
// that `client.ts` calls must exist in `proto/api/openapi.json`. The calls are read from the source
// with the TypeScript parser, so no method needs to be run. After a hub API change regenerate the
// file: `cd hub && uv run python -m topcoat_hub.openapi_contract`.
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const SRC = resolve(__dirname, "..");
const CONTRACT = resolve(SRC, "../../proto/api/openapi.json");
const CLIENT = resolve(SRC, "api/client.ts");
const PARAM = "{}";
const HOLE = "§"; // a `${...}` in a template literal
const MIN_CLIENT_CALLS = 300; // about 360 today: far fewer means the reader lost track of them

interface Contract {
  paths: Record<string, Record<string, unknown>>;
  "x-topcoat-websockets": string[];
}

interface Call {
  method: string; // upper case, or "ANY" when the code does not say
  path: string; // "/api/x/{}/y": `{}` for a value, the query left out
  where: string; // file:line
}

// ---- reading paths out of the source

function isPathText(text: string): boolean {
  return (
    text === "/api" || text.startsWith("/api/") || text.startsWith("/api?")
  );
}

/** `this.baseUrl`: the hub's origin in front of a path, not a value in it. */
const isBaseUrl = (node: ts.Expression): boolean =>
  ts.isPropertyAccessExpression(node) &&
  node.expression.kind === ts.SyntaxKind.ThisKeyword &&
  node.name.text === "baseUrl";

/** The text of a string or template literal with HOLE for each substitution, or null. A leading
 *  `${this.baseUrl}` is left out, so `${this.baseUrl}/api/x` reads as "/api/x". */
function literalText(node: ts.Node): string | null {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
    return node.text;
  if (ts.isTemplateExpression(node)) {
    const [first, ...rest] = node.templateSpans;
    const spans =
      node.head.text === "" && first && isBaseUrl(first.expression)
        ? rest
        : node.templateSpans;
    const head = spans === rest ? first!.literal.text : node.head.text;
    return head + spans.map((s) => HOLE + s.literal.text).join("");
  }
  return null;
}

/** "/api/x/HOLE/yHOLE?q=HOLE" -> "/api/x/{}/y": a hole that is a whole segment is a value,
 *  holes after a segment's text are a query suffix (`?a=b` or nothing). */
export function normalizePath(text: string): string {
  const path = text.split("?")[0]!;
  const segments = path.split("/").map((seg) => {
    if (!seg.includes(HOLE)) return seg;
    if (seg.startsWith(HOLE)) {
      // only holes: matches HOLE and HOLE+HOLE, rejects HOLE+".png"
      if (/^§+$/.test(seg)) return PARAM; // `${id}` or `${id}${query}`
      throw new Error(
        `a value with text in one path segment is not supported: ${seg}`,
      );
    }
    // trailing holes: strips "conversations"+HOLE to "conversations", leaves "a"+HOLE+"b" alone
    const head = seg.replace(/§+$/, "");
    if (head.includes(HOLE))
      throw new Error(`a value inside a path segment: ${seg}`);
    return head; // `conversations${query}`
  });
  const out = segments.join("/");
  // trailing slashes: "/api/x/" becomes "/api/x"; "/" stays
  return out.length > 1 ? out.replace(/\/+$/, "") : out;
}

function lineOf(sf: ts.SourceFile, node: ts.Node): string {
  const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
  return `${relative(SRC, sf.fileName).replace(/\\/g, "/")}:${line + 1}`;
}

function parse(file: string): ts.SourceFile {
  return ts.createSourceFile(
    file,
    readFileSync(file, "utf8"),
    ts.ScriptTarget.Latest,
    true,
  );
}

/** Private helpers of the client that only build a path (`residentPath`): name -> its template. */
function pathHelpers(sf: ts.SourceFile): Map<string, ts.Expression> {
  const helpers = new Map<string, ts.Expression>();
  const visit = (node: ts.Node): void => {
    if (
      ts.isMethodDeclaration(node) &&
      node.body &&
      ts.isIdentifier(node.name)
    ) {
      const [only] = node.body.statements;
      if (
        node.body.statements.length === 1 &&
        only &&
        ts.isReturnStatement(only) &&
        only.expression
      ) {
        const text = literalText(only.expression);
        if (text !== null && isPathText(text))
          helpers.set(node.name.text, only.expression);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return helpers;
}

/** How the client's transport helpers take their path and method. */
const TRANSPORT: Record<
  string,
  { path: number; method: (args: ts.NodeArray<ts.Expression>) => string }
> = {
  request: {
    path: 1,
    method: (args) => {
      const m = args[0];
      if (!m || !ts.isStringLiteral(m))
        throw new Error("request() needs a literal method");
      return m.text.toUpperCase();
    },
  },
  // GET without a body, POST with one (chatStream in client.ts)
  chatStream: {
    path: 0,
    method: (args) =>
      args[1]?.kind === ts.SyntaxKind.UndefinedKeyword ||
      (args[1] && ts.isIdentifier(args[1]) && args[1].text === "undefined")
        ? "GET"
        : "POST",
  },
  imageBlob: { path: 0, method: () => "GET" },
};

/** The method of a `fetch(path, init)` call: `init.method` when it is a literal, else GET. */
function fetchMethod(args: ts.NodeArray<ts.Expression>): string {
  const init = args[1];
  if (!init) return "GET";
  if (!ts.isObjectLiteralExpression(init))
    throw new Error("fetch() needs an object literal init");
  for (const prop of init.properties) {
    if (
      ts.isPropertyAssignment(prop) &&
      ts.isIdentifier(prop.name) &&
      prop.name.text === "method"
    ) {
      if (!ts.isStringLiteral(prop.initializer))
        throw new Error("fetch() needs a literal method");
      return prop.initializer.text.toUpperCase();
    }
  }
  return "GET";
}

/** Plain calls that take a full `${this.baseUrl}/api/...` URL as their first argument:
 *  `doFetch(url, init)` (a download) and `new URL(url, base)` (a WebSocket, opened with a GET). */
function rawCall(
  node: ts.Node,
): { arg: ts.Expression; method: () => string } | null {
  const [arg] =
    (ts.isCallExpression(node) || ts.isNewExpression(node)
      ? node.arguments
      : undefined) ?? [];
  if (
    !arg ||
    !ts.isIdentifier((node as ts.CallExpression | ts.NewExpression).expression)
  )
    return null;
  const name = (
    (node as ts.CallExpression | ts.NewExpression).expression as ts.Identifier
  ).text;
  if (ts.isNewExpression(node) && name === "URL")
    return { arg, method: () => "GET" };
  if (ts.isCallExpression(node) && (name === "doFetch" || name === "fetch"))
    return { arg, method: () => fetchMethod(node.arguments) };
  return null;
}

/** Every call the client makes, and every `/api` literal that no known call uses (an error). */
function clientCalls(): { calls: Call[]; unread: string[] } {
  const sf = parse(CLIENT);
  const helpers = pathHelpers(sf);
  const used = new Set<ts.Node>(helpers.values());
  const calls: Call[] = [];
  const visit = (node: ts.Node): void => {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.expression.kind === ts.SyntaxKind.ThisKeyword
    ) {
      const transport = TRANSPORT[node.expression.name.text];
      let arg = transport ? node.arguments[transport.path] : undefined;
      if (transport && arg) {
        if (
          ts.isCallExpression(arg) &&
          ts.isPropertyAccessExpression(arg.expression) &&
          helpers.has(arg.expression.name.text)
        )
          arg = helpers.get(arg.expression.name.text)!;
        const text = literalText(arg);
        if (text === null)
          throw new Error(`${lineOf(sf, node)}: the path is not a literal`);
        if (isPathText(text)) {
          used.add(arg);
          calls.push({
            method: transport.method(node.arguments),
            path: normalizePath(text),
            where: lineOf(sf, node),
          });
        }
      }
    }
    const raw = rawCall(node);
    const rawText = raw ? literalText(raw.arg) : null;
    if (raw && rawText !== null && isPathText(rawText)) {
      used.add(raw.arg);
      calls.push({
        method: raw.method(),
        path: normalizePath(rawText),
        where: lineOf(sf, node),
      });
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  const unread: string[] = [];
  const find = (node: ts.Node): void => {
    const text = literalText(node);
    if (text !== null && isPathText(text) && !used.has(node))
      unread.push(lineOf(sf, node));
    ts.forEachChild(node, find);
  };
  find(sf);
  return { calls, unread };
}

/** `/api` literals elsewhere in the app (links, the event stream): the path must exist. A literal
 *  that ends in "/" is a prefix (`url.startsWith("/api/reports/images/")`). */
function otherPaths(): Call[] {
  const out: Call[] = [];
  const walk = (dir: string): void => {
    readdirSync(dir, { withFileTypes: true }).forEach((entry) => {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      // sources: "a.ts", "b.tsx"; not "a.test.ts" or "a.json"
      else if (
        /\.tsx?$/.test(entry.name) &&
        !/\.test\.tsx?$/.test(entry.name) &&
        full !== CLIENT
      ) {
        const sf = parse(full);
        const visit = (node: ts.Node): void => {
          const text = literalText(node);
          if (text !== null && isPathText(text)) {
            const path = text.endsWith("/")
              ? normalizePath(text) + "/"
              : normalizePath(text);
            out.push({ method: "ANY", path, where: lineOf(sf, node) });
          }
          ts.forEachChild(node, visit);
        };
        visit(sf);
      }
    });
  };
  walk(SRC);
  return out;
}

// ---- matching against the contract

function contract(): Contract {
  return JSON.parse(readFileSync(CONTRACT, "utf8")) as Contract;
}

const segmentsOf = (path: string): string[] => path.split("/");
// a whole-segment path parameter: "{id}" matches, "{id}.png" and "id" do not
const isParam = (seg: string): boolean => /^\{[^}]+\}$/.test(seg);

/** A client path ("{}" for values) fits a hub path ("{name}"): same length, the same literal
 *  segments, a value where the hub takes one. */
export function fits(client: string, hub: string): boolean {
  if (client.endsWith("/"))
    return hub.startsWith(client) || fits(client.slice(0, -1), hub);
  const a = segmentsOf(client);
  const b = segmentsOf(hub);
  return (
    a.length === b.length &&
    a.every((seg, i) => (seg === PARAM ? isParam(b[i]!) : seg === b[i]))
  );
}

function missing(calls: Call[], doc: Contract): string[] {
  const http = Object.entries(doc.paths);
  return calls
    .filter(({ method, path }) => {
      // a WebSocket is opened with a GET
      if (doc["x-topcoat-websockets"].some((ws) => fits(path, ws)))
        return method !== "GET" && method !== "ANY";
      return !http.some(
        ([hub, ops]) =>
          fits(path, hub) && (method === "ANY" || method.toLowerCase() in ops),
      );
    })
    .map((c) => `${c.where}: ${c.method} ${c.path}`);
}

describe("the web client against proto/api/openapi.json", () => {
  const doc = contract();
  const { calls, unread } = clientCalls();

  it("reads the client's calls", () => {
    expect(calls.length).toBeGreaterThan(MIN_CLIENT_CALLS);
    expect(calls).toContainEqual(
      expect.objectContaining({ method: "GET", path: "/api/me" }),
    );
    expect(calls).toContainEqual(
      expect.objectContaining({
        method: "PUT",
        path: "/api/nodes/{}/residents/{}",
      }),
    );
    // a full URL with the hub's origin in front: a download and the voice WebSocket
    expect(calls).toContainEqual(
      expect.objectContaining({
        method: "GET",
        path: "/api/project/projects/{}/export",
      }),
    );
    expect(calls).toContainEqual(
      expect.objectContaining({ method: "GET", path: "/api/voice/ws" }),
    );
  });

  it("knows how every /api path in client.ts is called", () => {
    // a new transport helper: add it to TRANSPORT above
    expect(unread).toEqual([]);
  });

  it("calls only paths and methods the hub has", () => {
    expect(missing(calls, doc)).toEqual([]);
  });

  it("links only to paths the hub has elsewhere in the app", () => {
    expect(missing(otherPaths(), doc)).toEqual([]);
  });

  it("fails a call the hub does not have", () => {
    const fake = (method: string, path: string): Call => ({
      method,
      path,
      where: "x",
    });
    expect(missing([fake("GET", "/api/me")], doc)).toEqual([]);
    expect(missing([fake("DELETE", "/api/me")], doc)).toHaveLength(1); // wrong method
    expect(missing([fake("GET", "/api/no-such-route")], doc)).toHaveLength(1);
    expect(missing([fake("GET", "/api/nodes/{}/residents")], doc)).toHaveLength(
      1,
    ); // too short
  });
});

describe("normalizePath", () => {
  it("turns values into {} and drops the query", () => {
    expect(normalizePath(`/api/memory/${HOLE}/undo`)).toBe(
      "/api/memory/{}/undo",
    );
    expect(normalizePath(`/api/conversations${HOLE}`)).toBe(
      "/api/conversations",
    );
    expect(normalizePath(`/api/reports/${HOLE}${HOLE}`)).toBe(
      "/api/reports/{}",
    );
    expect(
      normalizePath(`/api/reports/${HOLE}/diff?from=${HOLE}&to=${HOLE}`),
    ).toBe("/api/reports/{}/diff");
  });
});
