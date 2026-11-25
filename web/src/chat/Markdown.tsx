import {
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useId,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import { useTranslation } from "react-i18next";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import powershell from "highlight.js/lib/languages/powershell";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import { ThemeCard, ThemeCardContext, parseTheme } from "./ThemeCard";
import { HubImage } from "../library/HubImage";

// A small set keeps the bundle small; other fences are shown without colours.
const languages = {
  bash,
  css,
  diff,
  dockerfile,
  go,
  ini,
  java,
  javascript,
  json,
  markdown,
  powershell,
  python,
  rust,
  sql,
  typescript,
  xml,
  yaml,
};

/** Text content of rendered children (for the copy button of a code block). */
function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

export function copyToClipboard(text: string): Promise<boolean> {
  return (navigator.clipboard?.writeText(text) ?? Promise.reject(new Error("no clipboard"))).then(
    () => true,
    () => false,
  );
}

/**
 * Set where a conversation has a message box: a code block that is a shell command then offers
 * "Reply with output", which opens the box with a template to paste the command's output into
 * (the assistant reads it; the user runs the command in their own shell).
 */
export const ComposeContext = createContext<((text: string, cursorInside: number) => void) | null>(
  null,
);

const SHELL_LANGUAGES = ["bash", "sh", "shell", "zsh", "console", "powershell", "ps1"];

function languageOf(node: ReactNode): string {
  if (!isValidElement<{ className?: string }>(node)) return "";
  const m = /(?:^|\s)language-(\S+)/.exec(node.props.className ?? "");
  return m?.[1] ? m[1].toLowerCase() : "";
}

/** The command a shell block runs, for the template: the first line, without a prompt sign. */
function commandOf(text: string): string {
  const first = text.split("\n").find((l) => l.trim() !== "") ?? "";
  return first.replace(/^\s*[$#>]\s+/, "").trim().slice(0, 120);
}

/** Loads mermaid only when a page has a diagram (a dynamic import: the bundle stays small).
 *  Tests replace it. */
export const mermaidLoader = {
  load: async () => (await import("mermaid")).default,
};

let mermaidReady: Promise<Awaited<ReturnType<typeof mermaidLoader.load>>> | null = null;

function loadMermaid() {
  mermaidReady ??= mermaidLoader.load().then((m) => {
    // strict: no HTML labels, no click handlers, no scripts from the diagram's text
    m.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "neutral",
      flowchart: { htmlLabels: false }, // plain SVG text: it is shown as an image below
    });
    return m;
  });
  mermaidReady.catch(() => {
    mermaidReady = null;
  });
  return mermaidReady;
}

/** A ```mermaid block as a diagram; while it loads, or when the text is no valid diagram, the
 *  code block is shown instead. */
function MermaidBlock({ code, children }: { code: string; children?: ReactNode }) {
  const id = `mmd-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const [svg, setSvg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    loadMermaid()
      .then((m) => m.render(id, code))
      .then(
        (out) => {
          if (live) setSvg(out.svg);
        },
        () => {
          if (live) setFailed(true);
        },
      );
    return () => {
      live = false;
    };
  }, [id, code]);
  if (svg && !failed) {
    // shown as an image, never as markup: an SVG in <img> runs no script and loads nothing
    return (
      <img
        className="mermaid-diagram"
        data-testid="mermaid"
        alt={code.slice(0, 200)}
        src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`}
      />
    );
  }
  return <CodeBlock>{children}</CodeBlock>;
}

/** A code block; a ```mermaid block is a diagram (the wiki, a chat answer, a report). */
export function Pre({ children }: { children?: ReactNode }) {
  if (languageOf(children) === "mermaid") return <MermaidBlock code={textOf(children)}>{children}</MermaidBlock>;
  return <CodeBlock>{children}</CodeBlock>;
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const compose = useContext(ComposeContext);
  const code = textOf(children);
  const shell = SHELL_LANGUAGES.includes(languageOf(children));
  const theme = languageOf(children) === "json" ? parseTheme(code) : null;
  return (
    <div className="code-block">
      <div className="code-actions">
        {shell && compose && (
          <button
            type="button"
            className="reply-output"
            onClick={() => {
              const head = t("chat.outputOf", { cmd: commandOf(code) }) + "\n```\n";
              compose(head + "\n```\n", head.length);
            }}
          >
            {t("chat.replyOutput")}
          </button>
        )}
        <button
          type="button"
          className="copy-code"
          onClick={() => void copyToClipboard(code).then(setCopied)}
        >
          {copied ? t("chat.copied") : t("chat.copyCode")}
        </button>
      </div>
      <pre>{children}</pre>
      {theme && <ThemeCard theme={theme} />}
    </div>
  );
}

const HUB_PICTURE = /^\/api\/reports\/images\/[0-9a-f-]{36}$/;

function Picture({ src, alt }: { src: string; alt: string }) {
  const api = useContext(ThemeCardContext);
  const load = useCallback((path: string) => (api ? api.reportImage(path) : Promise.reject(new Error("no client"))), [api]);
  if (api && HUB_PICTURE.test(src)) return <HubImage path={src} alt={alt} load={load} />;
  return <span className="muted">[{alt || "image"}]</span>;
}

const components: Components = {
  pre: ({ children }) => <Pre>{children}</Pre>,
  // Links open elsewhere and do not tell the target where they came from.
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow">
      {children}
    </a>
  ),
  // Model output must not load anything by itself (tracking pixels): show the alt text. Only a
  // picture the hub keeps, where the hub's client is at hand (a project's result), is shown.
  img: ({ src, alt }) => <Picture src={typeof src === "string" ? src : ""} alt={alt ?? ""} />,
  table: ({ children }) => (
    <div className="table-wrap">
      <table>{children}</table>
    </div>
  ),
};

/** The id of a heading in a document (the same rule as the wiki's contents, `wiki/doc.ts`). */
function headingId(node: ReactNode): string {
  return (
    textOf(node)
      .toLowerCase()
      .replace(/ä/g, "ae")
      .replace(/ö/g, "oe")
      .replace(/ü/g, "ue")
      .replace(/ß/g, "ss")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "section"
  );
}

function scrollTo(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** A document (a wiki page): headings carry ids, `#cite-E3` is a small mark that jumps to its
 *  source, other `#` links jump inside the page; everything else as in an answer. */
const docComponents: Components = {
  ...components,
  h2: ({ children }) => <h2 id={headingId(children)}>{children}</h2>,
  h3: ({ children }) => <h3 id={headingId(children)}>{children}</h3>,
  a: ({ href, children }) => {
    if (href?.startsWith("#cite-")) {
      const id = href.slice(1);
      return (
        <sup className="cite">
          <a
            href={href}
            onClick={(e) => {
              e.preventDefault();
              scrollTo(id);
            }}
          >
            {children}
          </a>
        </sup>
      );
    }
    if (href?.startsWith("#") && !href.startsWith("#/")) {
      return (
        <a
          href={href}
          onClick={(e) => {
            e.preventDefault();
            scrollTo(href.slice(1));
          }}
        >
          {children}
        </a>
      );
    }
    return (
      <a href={href} target="_blank" rel="noopener noreferrer nofollow">
        {children}
      </a>
    );
  },
};

/**
 * Markdown of an answer with code highlighting. react-markdown builds React elements and
 * ignores raw HTML, so nothing a model writes becomes markup (no innerHTML); `javascript:`
 * links are dropped by its default URL transform.
 */
export function Markdown({ text, doc = false }: { text: string; doc?: boolean }) {
  return (
    <div className={doc ? "markdown markdown-doc" : "markdown"}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { languages, detect: false }]]}
        components={doc ? docComponents : components}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
