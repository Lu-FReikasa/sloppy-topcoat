import { fireEvent, render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ComposeContext, Markdown, mermaidLoader } from "./Markdown";

describe("Markdown", () => {
  it("draws a mermaid block as an image and falls back to the code when it is broken", async () => {
    const init = vi.fn();
    mermaidLoader.load = (async () => ({
      initialize: init,
      render: async (_id: string, code: string) => {
        if (code.includes("broken")) throw new Error("parse error");
        return { svg: `<svg><text>${code.trim()}</text></svg>` };
      },
    })) as unknown as typeof mermaidLoader.load;
    const good = render(<Markdown text={"```mermaid\ngraph TD; A-->B\n```"} />);
    const img = await waitFor(() => {
      const found = good.container.querySelector<HTMLImageElement>("img[data-testid='mermaid']");
      expect(found).not.toBeNull();
      return found as HTMLImageElement;
    });
    expect(decodeURIComponent(img.src)).toContain("<svg><text>graph TD; A-->B</text></svg>");
    expect(init).toHaveBeenCalledWith(expect.objectContaining({ securityLevel: "strict" }));

    const bad = render(<Markdown text={"```mermaid\nbroken ->\n```"} />);
    await waitFor(() => expect(init).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(bad.container.querySelector("img")).toBeNull();
    expect(bad.container.querySelector("pre")?.textContent).toContain("broken ->");
  });

  it("renders headings, lists and inline code", () => {
    const { container } = render(<Markdown text={"# Title\n\n- one\n- two\n\nuse `x`"} />);
    expect(container.querySelector("h1")?.textContent).toBe("Title");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("code")?.textContent).toBe("x");
  });

  it("highlights known languages and offers a copy button per code block", () => {
    const { container } = render(<Markdown text={"```python\ndef f():\n    return 1\n```"} />);
    expect(container.querySelector(".hljs-keyword")?.textContent).toBe("def");
    expect(container.querySelector("button.copy-code")).not.toBeNull();
  });

  it("shows unknown languages without colours", () => {
    const { container } = render(<Markdown text={"```brainfuck\n+++\n```"} />);
    expect(container.querySelector("pre")?.textContent).toContain("+++");
    expect(container.querySelector("[class*='hljs-']")).toBeNull();
  });

  it("never turns model output into markup", () => {
    const { container } = render(
      <Markdown text={'<script>window.x=1</script><img src=x onerror="window.x=1"> text'} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("replaces images by their alt text so nothing loads by itself", () => {
    const { container } = render(<Markdown text="![tracker](https://evil.example/p.png)" />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("[tracker]");
  });

  it("drops javascript: links and opens others safely", () => {
    const { container } = render(
      <Markdown text={"[bad](javascript:alert(1)) [good](https://example.org)"} />,
    );
    const links = [...container.querySelectorAll("a")];
    expect(links.find((a) => a.textContent === "bad")?.getAttribute("href") ?? "").toBe("");
    const good = links.find((a) => a.textContent === "good");
    expect(good?.getAttribute("target")).toBe("_blank");
    expect(good?.getAttribute("rel")).toContain("noopener");
  });
});

describe("Markdown shell blocks", () => {
  it("offers 'Reply with output' on a shell block only where there is a message box", () => {
    const text = "```bash\n$ sudo ss -tlnp\n```";
    const without = render(<Markdown text={text} />);
    expect(without.container.querySelector("button.reply-output")).toBeNull();
    without.unmount();
    const compose = vi.fn();
    const { container } = render(
      <ComposeContext.Provider value={compose}>
        <Markdown text={text} />
      </ComposeContext.Provider>,
    );
    fireEvent.click(container.querySelector("button.reply-output") as HTMLButtonElement);
    const [tpl, cursor] = compose.mock.calls[0] as [string, number];
    expect(tpl).toContain("sudo ss -tlnp");
    // the cursor sits between the two fence lines, where the output is pasted
    expect(tpl.slice(cursor, cursor + 4)).toBe("\n```");
  });

  it("does not offer it on other languages", () => {
    const { container } = render(
      <ComposeContext.Provider value={vi.fn()}>
        <Markdown text={"```python\nprint(1)\n```"} />
      </ComposeContext.Provider>,
    );
    expect(container.querySelector("button.reply-output")).toBeNull();
  });
});
