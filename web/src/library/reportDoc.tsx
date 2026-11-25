import { type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import { renderToStaticMarkup } from "react-dom/server";
import remarkGfm from "remark-gfm";
import { Pre } from "../chat/Markdown";
import { HubImage, type ImageLoader } from "./HubImage";

/** The anchor of a heading, made the way the hub makes it (``report_doc.slug``): lower case,
 *  letters and digits, a hyphen for the rest. The table of contents of a report links to it. */
export function slug(text: string): string {
  const out = text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}_]+/gu, "-")
    .replace(/^[-_]+|[-_]+$/g, "");
  return out || "section";
}

/** The Markdown of a report without its front matter (the title page is made from the metadata). */
export function stripFrontMatter(text: string): string {
  if (!text.startsWith("---\n")) return text;
  const end = text.indexOf("\n---\n", 4);
  return end === -1 ? text : text.slice(end + 5).replace(/^\n+/, "");
}

/** The chapters of a report, for the comment form and the contents: the `##` headings. */
export function headingsOf(text: string): { id: string; title: string }[] {
  const out: { id: string; title: string }[] = [];
  for (const line of stripFrontMatter(text).split("\n")) {
    const m = /^##\s+(.+?)\s*$/.exec(line);
    if (m) out.push({ id: slug(m[1]!), title: m[1]! });
  }
  return out;
}

/** The text of a node of the parsed Markdown (a heading with a link or emphasis in it). */
function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && typeof node === "object" && "props" in node) {
    return textOf((node as { props: { children?: ReactNode } }).props.children);
  }
  return "";
}

/** The look of a report: a white page with a dark text whatever the theme is, a table of contents
 *  that links inside the page, tables that do not run over the edge, and a print style that drops
 *  everything that is not the document. One string, used by the page and by the export. */
export const PAPER_CSS = `
.report-paper{background:#fff;color:#1b1b1b;max-width:46rem;margin:0 auto;padding:2.2rem 2.6rem;
  font:16px/1.55 Georgia,"Times New Roman",serif;border-radius:4px;box-shadow:0 1px 8px rgba(0,0,0,.25);}
.report-paper h1{font-size:1.9rem;line-height:1.2;margin:0 0 1rem;}
.report-paper h2{font-size:1.35rem;margin:2rem 0 .6rem;padding-bottom:.2rem;border-bottom:1px solid #d6d6d6;}
.report-paper h3{font-size:1.1rem;margin:1.4rem 0 .4rem;}
.report-paper a{color:#1a55a3;}
.report-paper table{border-collapse:collapse;width:100%;margin:.8rem 0;font-size:.92rem;}
.report-paper th,.report-paper td{border:1px solid #cfcfcf;padding:.3rem .5rem;vertical-align:top;text-align:left;
  overflow-wrap:anywhere;}
.report-paper th{background:#f1f1f1;}
.report-paper blockquote{margin:1rem 0;padding:.4rem 1rem;border-left:4px solid #b9770e;background:#fdf6e8;}
.report-paper blockquote:first-of-type{border-left-color:#1a55a3;background:#eef3fb;}
.report-paper blockquote strong:first-child{display:block;margin-bottom:.2rem;letter-spacing:.02em;}
.report-paper tbody tr:nth-child(even){background:#fafafa;}
.report-paper .mermaid-diagram{display:block;max-width:100%;margin:1rem auto;}
.report-paper figure.report-figure{margin:1.2rem 0;text-align:center;}
.report-paper figure.report-figure img{max-width:100%;max-height:28rem;border:1px solid #ddd;}
.report-paper figcaption{font-size:.85rem;color:#555;margin-top:.3rem;}
.report-paper .report-figure-wait{padding:1.2rem;border:1px dashed #bbb;color:#666;font-size:.9rem;}
.report-paper code{font-family:Consolas,monospace;font-size:.9em;background:#f3f3f3;padding:0 .2em;}
.report-paper pre{background:#f3f3f3;padding:.7rem;overflow:auto;}
.report-paper p.report-banner{border:2px solid #b9770e;background:#fdf6e8;padding:.6rem .9rem;}
.report-paper ul,.report-paper ol{padding-left:1.4rem;}
@media print{
  body *{visibility:hidden;}
  .report-paper,.report-paper *{visibility:visible;}
  .report-paper{position:absolute;left:0;top:0;box-shadow:none;max-width:none;padding:0;}
  .report-paper h2{break-after:avoid;}
  .report-paper table,.report-paper blockquote{break-inside:avoid;}
}
`;


interface BodyProps {
  text: string;
  /** Loads the hub's pictures (the app; the export has none). */
  loadImage?: ImageLoader;
  /** Prefix of the heading ids: in the app the page has other ids too; in the export none. */
  idPrefix?: string;
  /** In the app a link to a heading scrolls inside the page (the address bar belongs to the
   *  router); in the export it is a plain anchor. */
  inApp?: boolean;
}

/** The report as a page. */
export function ReportBody({ text, idPrefix = "", inApp = false, loadImage }: BodyProps) {
  const components: Components = {
    h2: ({ children }) => <h2 id={`${idPrefix}${slug(textOf(children))}`}>{children}</h2>,
    h3: ({ children }) => <h3 id={`${idPrefix}${slug(textOf(children))}`}>{children}</h3>,
    a: ({ href, children }) => {
      if (inApp && href?.startsWith("#") && !href.startsWith("#/")) {
        const target = `${idPrefix}${href.slice(1)}`;
        return (
          <a
            href={href}
            onClick={(e) => {
              e.preventDefault();
              document.getElementById(target)?.scrollIntoView({ behavior: "smooth" });
            }}
          >
            {children}
          </a>
        );
      }
      if (href?.startsWith("#")) return <a href={href}>{children}</a>;
      return (
        <a href={href} rel="noreferrer noopener" target="_blank">
          {children}
        </a>
      );
    },
    // a ```mermaid block is a diagram (2026-11-13); in the export it stays its code
    pre: ({ children }) => <Pre>{children}</Pre>,
    // a picture the hub keeps for the report, with its caption; a picture on another server is
    // not loaded from there (the viewer's address would go along): it stays a link
    img: ({ src, alt }) => {
      const url = typeof src === "string" ? src : "";
      if (url.startsWith("/api/reports/images/")) {
        return <HubImage path={url} alt={alt ?? ""} load={loadImage} />;
      }
      if (!url.startsWith("data:image/")) {
        return url ? (
          <a href={url} rel="noreferrer noopener" target="_blank">
            {alt || url}
          </a>
        ) : null;
      }
      return (
        <figure className="report-figure">
          <img src={url} alt={alt ?? ""} loading="lazy" />
          {alt && <figcaption>{alt}</figcaption>}
        </figure>
      );
    },
    // a report holds no raw HTML (D102): react-markdown does not render it either
    p: ({ children }) => {
      const s = textOf(children);
      const banner = /^(Diese Recherche ist unvollständig|This research is incomplete)/.test(s);
      return <p className={banner ? "report-banner" : undefined}>{children}</p>;
    },
  };
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {stripFrontMatter(text)}
    </ReactMarkdown>
  );
}

/** The export: one HTML file that stands alone (no scripts, nothing loaded from outside), with the
 *  title, the contents with links, the print style. Open it in a browser or print it to PDF. */
export function exportHtml(title: string, text: string, language: string): string {
  const body = renderToStaticMarkup(
    <article className="report-paper">
      <ReportBody text={text} />
    </article>,
  );
  const safe = title.replace(/[<>&"]/g, (c) => `&#${c.charCodeAt(0)};`);
  return (
    `<!doctype html><html lang="${language === "de" ? "de" : "en"}"><head><meta charset="utf-8">` +
    `<meta name="viewport" content="width=device-width, initial-scale=1">` +
    `<meta name="generator" content="Topcoat"><title>${safe}</title>` +
    `<style>body{margin:0;padding:1.5rem;background:#e9e9e9}${PAPER_CSS}</style></head>` +
    `<body>${body}</body></html>`
  );
}

/** A file name for a download: the title without what a file system dislikes. */
export function fileName(title: string, extension: string): string {
  const base = title.replace(/[\\/:*?"<>|\s]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80);
  return `${base || "report"}.${extension}`;
}
