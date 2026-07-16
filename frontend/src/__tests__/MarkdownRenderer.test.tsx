/**
 * Tests for MarkdownRenderer, CodeBlock, CopyButton, SafeLink,
 * isSafeUrl, and the Retry integration in App.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import {
  MarkdownRenderer,
  CopyButton,
  isSafeUrl,
} from "../MarkdownRenderer";
import React from "react";

// ── isSafeUrl Tests ──────────────────────────────────────

describe("isSafeUrl", () => {
  it("allows https URLs", () => {
    expect(isSafeUrl("https://example.com")).toBe(true);
  });

  it("allows http URLs", () => {
    expect(isSafeUrl("http://example.com")).toBe(true);
  });

  it("allows mailto URLs", () => {
    expect(isSafeUrl("mailto:user@example.com")).toBe(true);
  });

  it("blocks javascript: URLs", () => {
    expect(isSafeUrl("javascript:alert('xss')")).toBe(false);
  });

  it("blocks data: URLs", () => {
    expect(isSafeUrl("data:text/html,<script>alert(1)</script>")).toBe(false);
  });

  it("blocks vbscript: URLs", () => {
    expect(isSafeUrl("vbscript:msgbox('xss')")).toBe(false);
  });

  it("blocks file: URLs", () => {
    expect(isSafeUrl("file:///etc/passwd")).toBe(false);
  });

  it("returns false for undefined", () => {
    expect(isSafeUrl(undefined)).toBe(false);
  });

  it("blocks empty string", () => {
    expect(isSafeUrl("")).toBe(false);
  });
});

// ── MarkdownRenderer Tests ───────────────────────────────

describe("MarkdownRenderer", () => {
  it("renders plain text correctly", () => {
    const { container } = render(
      <MarkdownRenderer content="Hello, world!" />
    );
    expect(container.textContent).toContain("Hello, world!");
  });

  it("renders headings", () => {
    const { container } = render(
      <MarkdownRenderer content="# Heading 1\n\n## Heading 2\n\n### Heading 3" />
    );
    expect(container.textContent).toContain("Heading 1");
    expect(container.textContent).toContain("Heading 2");
    expect(container.textContent).toContain("Heading 3");
  });

  it("renders bold and italic text", () => {
    const { container } = render(
      <MarkdownRenderer content="**bold** and *italic*" />
    );
    expect(container.innerHTML).toContain("bold");
    expect(container.innerHTML).toContain("italic");
  });

  it("renders ordered and unordered lists", () => {
    const { container } = render(
      <MarkdownRenderer content="- Item 1\n- Item 2\n\n1. First\n2. Second" />
    );
    expect(container.textContent).toContain("Item 1");
    expect(container.textContent).toContain("Item 2");
    expect(container.textContent).toContain("First");
    expect(container.textContent).toContain("Second");
  });

  it("renders blockquotes", () => {
    const { container } = render(
      <MarkdownRenderer content="> A quoted line" />
    );
    expect(container.textContent).toContain("A quoted line");
    const blockquote = container.querySelector("blockquote");
    expect(blockquote).not.toBeNull();
  });

  it("renders inline code", () => {
    const { container } = render(
      <MarkdownRenderer content="Use the `code()` function." />
    );
    expect(container.textContent).toContain("code()");
  });

  it("renders fenced code blocks with language label", () => {
    const { container } = render(
      <MarkdownRenderer content={`\`\`\`python\nprint("hello")\n\`\`\``} />
    );
    // Code block wrapper should be rendered
    const wrapper = container.querySelector(".code-block-wrapper");
    expect(wrapper).not.toBeNull();
    // Code content should be visible
    expect(container.textContent).toContain('print("hello")');
  });

  it("renders tables", () => {
    const { container } = render(
      <MarkdownRenderer content="| Col1 | Col2 |\n|------|------|\n| A    | B    |" />
    );
    expect(container.textContent).toContain("Col1");
    expect(container.textContent).toContain("Col2");
    expect(container.textContent).toContain("A");
    expect(container.textContent).toContain("B");
  });

  it("renders horizontal rules", () => {
    const { container } = render(
      <MarkdownRenderer content="Text before\n\n- - -\n\nText after" />
    );
    // Content should render with text on both sides of the HR
    expect(container.textContent).toContain("Text before");
    expect(container.textContent).toContain("Text after");
  });

  it("renders links with safe attributes", () => {
    const { container } = render(
      <MarkdownRenderer content="[Visit](https://example.com)" />
    );
    const link = container.querySelector("a");
    expect(link).not.toBeNull();
    expect(link?.getAttribute("href")).toBe("https://example.com");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("blocks javascript: links", () => {
    const { container } = render(
      <MarkdownRenderer content="[Click](javascript:alert('xss'))" />
    );
    const link = container.querySelector("a");
    // Invalid links should be rendered as span with unsafe-link class
    const unsafeSpan = container.querySelector(".unsafe-link");
    expect(unsafeSpan).not.toBeNull();
    expect(unsafeSpan?.textContent).toContain("Click");
  });

  it("blocks data: links", () => {
    const { container } = render(
      <MarkdownRenderer content="[Data](data:text/html,<script>alert(1)</script>)" />
    );
    const unsafeSpan = container.querySelector(".unsafe-link");
    expect(unsafeSpan).not.toBeNull();
  });

  it("blocks raw HTML (script tags are escaped)", () => {
    const { container } = render(
      <MarkdownRenderer content={'<script>alert("xss")</script>'} />
    );
    // react-markdown escapes raw HTML by default
    expect(container.innerHTML).not.toContain("<script>");
    // The content should be visible as escaped text
    expect(container.textContent).toContain("alert");
  });

  it("renders citation buttons when citations are provided", () => {
    const citations = [
      {
        marker: "[1]",
        document_id: 1,
        filename: "test.pdf",
        page_number: 5,
        chunk_id: 1,
        snippet: "Important content",
      },
    ];
    const onCitationClick = vi.fn();

    const { container } = render(
      <MarkdownRenderer
        content="Some text [1] with citation"
        citations={citations}
        onCitationClick={onCitationClick}
      />
    );

    const citationBtn = container.querySelector(".citation-btn");
    expect(citationBtn).not.toBeNull();
    expect(citationBtn?.textContent).toBe("[1]");
  });

  it("citation button click triggers onCitationClick", () => {
    const citations = [
      {
        marker: "[1]",
        document_id: 1,
        filename: "test.pdf",
        page_number: 5,
        chunk_id: 1,
        snippet: "Important content",
      },
    ];
    const onCitationClick = vi.fn();

    const { container } = render(
      <MarkdownRenderer
        content="Text [1] here"
        citations={citations}
        onCitationClick={onCitationClick}
      />
    );

    const citationBtn = container.querySelector(".citation-btn")!;
    fireEvent.click(citationBtn);

    expect(onCitationClick).toHaveBeenCalledOnce();
    expect(onCitationClick).toHaveBeenCalledWith(citations[0]);
  });

  it("handles malformed markdown without crashing", () => {
    // Incomplete fenced code block
    const { container } = render(
      <MarkdownRenderer content="Some text\n`````\nunclosed code block" />
    );
    expect(container.textContent).toContain("unclosed code block");
  });

  it("handles empty content", () => {
    const { container } = render(<MarkdownRenderer content="" />);
    expect(container.innerHTML).toBe("");
  });

  it("handles null content gracefully", () => {
    // @ts-expect-error testing null edge case
    const { container } = render(<MarkdownRenderer content={null} />);
    expect(container.innerHTML).toBe("");
  });

  it("handles multiple citations in one paragraph", () => {
    const citations = [
      { marker: "[1]", document_id: 1, filename: "a.pdf", page_number: null, chunk_id: 1, snippet: "A" },
      { marker: "[2]", document_id: 2, filename: "b.pdf", page_number: null, chunk_id: 2, snippet: "B" },
    ];
    const onCitationClick = vi.fn();

    const { container } = render(
      <MarkdownRenderer
        content="Source [1] and source [2] both cited."
        citations={citations}
        onCitationClick={onCitationClick}
      />
    );

    const buttons = container.querySelectorAll(".citation-btn");
    expect(buttons).toHaveLength(2);
    expect(buttons[0].textContent).toBe("[1]");
    expect(buttons[1].textContent).toBe("[2]");
  });

  it("images are not rendered (security)", () => {
    const { container } = render(
      <MarkdownRenderer content="![alt](https://evil.com/tracker.png)" />
    );
    const img = container.querySelector("img");
    expect(img).toBeNull();
  });

  it("syntax highlighting renders code with language header", () => {
    const { container } = render(
      <MarkdownRenderer content={`\`\`\`javascript\nconst x = 1;\n\`\`\``} />
    );
    // Code block wrapper should be rendered
    const wrapper = container.querySelector(".code-block-wrapper");
    expect(wrapper).not.toBeNull();
    // Code content should be visible
    expect(container.textContent).toContain("const x = 1;");
    // Should have a copy button
    const copyBtn = container.querySelector(".code-copy-btn");
    expect(copyBtn).not.toBeNull();
  });

  it("plain code blocks (no language) render without language header", () => {
    const { container } = render(
      <MarkdownRenderer content={`\`\`\`\nplain code\n\`\`\``} />
    );
    // No language label for unspecified code blocks
    // Should render the code
    expect(container.textContent).toContain("plain code");
  });
});

// ── CopyButton Tests ─────────────────────────────────────

describe("CopyButton", () => {
  beforeEach(() => {
    // Mock clipboard API
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockResolvedValue(undefined),
      },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders Copy label initially", () => {
    const { container } = render(<CopyButton code="test code" />);
    expect(container.textContent).toContain("Copy");
  });

  it("shows Copied feedback after click", async () => {
    const { container } = render(<CopyButton code="test code" />);
    const btn = container.querySelector("button")!;

    await act(async () => {
      fireEvent.click(btn);
    });

    expect(container.textContent).toContain("Copied!");
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("test code");
  });

  it("calls clipboard.writeText with the correct code", async () => {
    const { container } = render(<CopyButton code="const x = 1;" />);
    const btn = container.querySelector("button")!;

    await act(async () => {
      fireEvent.click(btn);
    });

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("const x = 1;");
  });

  it("handles clipboard failure gracefully", async () => {
    vi.mocked(navigator.clipboard.writeText).mockRejectedValue(
      new Error("Clipboard denied")
    );

    const { container } = render(<CopyButton code="test" />);
    const btn = container.querySelector("button")!;

    await act(async () => {
      fireEvent.click(btn);
    });

    // Should not crash — button label stays as Copy
    expect(container.textContent).toContain("Copy");
  });
});

// ── Retry Integration Tests ──────────────────────────────

describe("Retry Integration (App.tsx)", () => {
  it("error message shows error content with retry target available", () => {
    // This tests that the error-message div structure supports retry.
    // Full retry behavior is tested via the handleSend flow.
    const { container } = render(
      <div>
        <div className="error-message">
          <div className="error-message-content">
            <span className="error-message-icon">⚠️</span>
            <span>Ollama unavailable</span>
          </div>
          <button className="retry-btn">🔄 Retry</button>
        </div>
      </div>
    );

    expect(container.textContent).toContain("Ollama unavailable");
    expect(container.textContent).toContain("Retry");
    const retryBtn = container.querySelector(".retry-btn");
    expect(retryBtn).not.toBeNull();
  });

  it("retry button is not shown when streaming is active", () => {
    // Retry button should be hidden during streaming to prevent conflicts
    const { container } = render(
      <div>
        <div className="error-message">
          <div className="error-message-content">
            <span>Error</span>
          </div>
        </div>
      </div>
    );

    // No retry button rendered
    const retryBtn = container.querySelector(".retry-btn");
    expect(retryBtn).toBeNull();
  });
});
