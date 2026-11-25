import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  PAPER_CSS,
  ReportBody,
  exportHtml,
  fileName,
  headingsOf,
  slug,
  stripFrontMatter,
} from "./reportDoc";

afterEach(cleanup);

const DOC = `---
title: "Systemd"
version: 1
---

# Systemd

## Inhalt

- [Vorwort](#vorwort)
- [1. Geschichte](#1-geschichte)

## Vorwort

Der Bericht behandelt systemd [1].

## 1. Geschichte

### Anfang

| a | b |
|---|---|
| 1 | 2 |
`;

describe("the anchors of a report", () => {
  it("are made the way the hub makes them", () => {
    // the same cases as hub/tests/test_report_doc.py: a link of the contents must hit the heading
    expect(slug("1. Geschichte")).toBe("1-geschichte");
    expect(slug("Äußerer Rahmen!")).toBe("äußerer-rahmen");
    expect(slug("Grenzen dieser Recherche")).toBe("grenzen-dieser-recherche");
    expect(slug("   ")).toBe("section");
  });

  it("are found in the document, front matter left out", () => {
    expect(stripFrontMatter(DOC).startsWith("# Systemd")).toBe(true);
    expect(stripFrontMatter("no front matter")).toBe("no front matter");
    expect(headingsOf(DOC).map((h) => h.id)).toEqual(["inhalt", "vorwort", "1-geschichte"]);
  });
});

describe("the page", () => {
  it("gives headings ids and scrolls inside the page when the contents are used", () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    const before = window.location.hash;
    render(<ReportBody text={DOC} idPrefix="rep-" inApp />);
    expect(document.getElementById("rep-vorwort")?.tagName).toBe("H2");
    expect(document.getElementById("rep-anfang")?.tagName).toBe("H3");
    fireEvent.click(screen.getByRole("link", { name: "Vorwort" }));
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(window.location.hash).toBe(before); // the address bar belongs to the router
  });

  it("shows a table and no front matter", () => {
    render(<ReportBody text={DOC} />);
    expect(screen.getByRole("table")).toBeTruthy();
    expect(screen.queryByText(/version: 1/)).toBeNull();
  });

  it("marks the banner of an incomplete research", () => {
    render(
      <ReportBody
        text={"# T\n\n**Diese Recherche ist unvollständig.** Sie wurde vorzeitig beendet.\n"}
      />,
    );
    expect(document.querySelector("p.report-banner")).not.toBeNull();
  });

  it("does not render raw HTML from a report", () => {
    render(<ReportBody text={'# T\n\n<script>window.pwned = 1</script><b id="x">bold</b>\n'} />);
    expect(document.querySelector("script")).toBeNull();
    expect(document.getElementById("x")).toBeNull();
  });
});

describe("the export", () => {
  it("is a file that stands alone", () => {
    const html = exportHtml('A <b>"title"</b> & more', DOC, "de");
    expect(html.startsWith("<!doctype html><html lang=\"de\">")).toBe(true);
    expect(html).toContain("<title>A &#60;b&#62;&#34;title&#34;&#60;/b&#62; &#38; more</title>");
    expect(html).toContain('<a href="#vorwort">Vorwort</a>');
    expect(html).toContain('<h2 id="vorwort">Vorwort</h2>');
    expect(html).toContain("@media print");
    expect(html).not.toContain("<script");
    expect(html).not.toMatch(/https?:\/\/[^"'\s]*\.(css|js)/); // nothing is loaded from outside
    expect(html).toContain(PAPER_CSS.slice(0, 40));
  });

  it("names a download after the title", () => {
    expect(fileName('Systemd: "Rolle" / Geschichte', "html")).toBe("Systemd_Rolle_Geschichte.html");
    expect(fileName("", "md")).toBe("report.md");
  });
});

describe("pictures in a report", () => {
  it("loads the hub's picture with the token and leaves one of another server a link", async () => {
    const id = "11111111-2222-4333-8444-555555555555";
    const load = vi.fn().mockResolvedValue("blob:picture");
    const { container } = render(
      <ReportBody
        text={`![Eine Pixel-Figur](/api/reports/images/${id})

![fremd](https://example.org/a.png)`}
        loadImage={load}
      />,
    );
    await waitFor(() => expect(container.querySelector("figure img")?.getAttribute("src")).toBe("blob:picture"));
    expect(load).toHaveBeenCalledWith(`/api/reports/images/${id}`);
    expect(container.querySelector("figcaption")?.textContent).toBe("Eine Pixel-Figur");
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(container.querySelector("a[href='https://example.org/a.png']")?.textContent).toBe("fremd");
  });

  it("shows the caption in place of a picture in the export", () => {
    const html = exportHtml("T", "![Ein Bild](/api/reports/images/11111111-2222-4333-8444-555555555555)", "de");
    expect(html).toContain("Ein Bild");
    expect(html).not.toContain("<img");
  });
});
