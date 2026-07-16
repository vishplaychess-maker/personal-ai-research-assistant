/**
 * MarkdownRenderer — Renders assistant message content as safe Markdown
 * with syntax-highlighted code blocks, copy-to-clipboard, citation
 * integration, and XSS prevention.
 */

import React, { useState, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";

// Import only the languages we commonly encounter in LLM responses
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";

SyntaxHighlighter.registerLanguage("tsx", tsx);
SyntaxHighlighter.registerLanguage("typescript", typescript);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("css", css);
SyntaxHighlighter.registerLanguage("sql", sql);
SyntaxHighlighter.registerLanguage("yaml", yaml);
SyntaxHighlighter.registerLanguage("html", markup);
SyntaxHighlighter.registerLanguage("xml", markup);
SyntaxHighlighter.registerLanguage("jsx", tsx);

// ── Types ─────────────────────────────────────────────────

export interface Citation {
  marker: string;
  document_id: number;
  filename: string;
  page_number: number | null;
  chunk_id: number;
  snippet: string;
}

interface MarkdownRendererProps {
  content: string;
  citations?: Citation[] | null;
  onCitationClick?: (citation: Citation) => void;
}

// ── URL Validation ───────────────────────────────────────

const SAFE_PROTOCOLS = ["http:", "https:", "mailto:"];
const UNSAFE_PATTERNS = [
  /^javascript:/i,
  /^data:/i,
  /^vbscript:/i,
  /^file:/i,
];

function isSafeUrl(href: string | undefined): boolean {
  if (!href) return false;
  try {
    const url = new URL(href, "https://safe.tld");
    if (UNSAFE_PATTERNS.some((p) => p.test(href))) return false;
    return SAFE_PROTOCOLS.includes(url.protocol);
  } catch {
    // Invalid URL — treat as unsafe
    return false;
  }
}

// ── Copy Button Component ────────────────────────────────

interface CopyButtonProps {
  code: string;
}

function CopyButton({ code }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API may fail in non-HTTPS contexts or older browsers
      // Fallback: select the text manually
      setCopied(false);
    }
  }, [code]);

  return (
    <button className="code-copy-btn" onClick={handleCopy} title="Copy code">
      {copied ? (
        <>
          <span className="code-copy-icon">✓</span>
          <span className="code-copy-label">Copied!</span>
        </>
      ) : (
        <>
          <span className="code-copy-icon">⎘</span>
          <span className="code-copy-label">Copy</span>
        </>
      )}
    </button>
  );
}

// ── Code Block Component ─────────────────────────────────

interface CodeBlockProps {
  className?: string;
  children?: React.ReactNode;
}

function CodeBlock({ className, children }: CodeBlockProps) {
  const match = /language-(\w+)/.exec(className || "");
  const codeString = String(children || "").replace(/\n$/, "");

  if (!match) {
    // Inline code or unknown language — render as plain pre/code
    return (
      <pre className="code-block-plain">
        <code>{codeString}</code>
      </pre>
    );
  }

  const language = match[1];

  return (
    <div className="code-block-wrapper">
      <div className="code-block-header">
        <span className="code-block-lang">{language}</span>
        <CopyButton code={codeString} />
      </div>
      <SyntaxHighlighter
        style={oneDark}
        language={language}
        PreTag="div"
        customStyle={{
          margin: 0,
          borderTopLeftRadius: 0,
          borderTopRightRadius: 0,
          fontSize: "0.8rem",
          lineHeight: 1.5,
        }}
      >
        {codeString}
      </SyntaxHighlighter>
    </div>
  );
}

// ── Safe Link Component ──────────────────────────────────

interface SafeLinkProps {
  href?: string;
  children?: React.ReactNode;
}

function SafeLink({ href, children }: SafeLinkProps) {
  if (!href || !isSafeUrl(href)) {
    // Render unsafe URLs as plain text
    return <span className="unsafe-link">{children}</span>;
  }

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="markdown-link"
    >
      {children}
    </a>
  );
}

// ── Citation Pattern ────────────────────────────────────

const CITATION_PATTERN = /\[(\d+)\]/g;

function splitTextWithCitations(
  text: string,
  citations: Citation[],
  onCitationClick: (citation: Citation) => void
): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  // Reset regex state
  CITATION_PATTERN.lastIndex = 0;

  while ((match = CITATION_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }

    const markerNum = parseInt(match[1], 10);
    const citation = citations.find((c) => c.marker === `[${markerNum}]`);

    if (citation) {
      parts.push(
        <button
          key={`citation-${match.index}`}
          className="citation-btn"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onCitationClick(citation);
          }}
          title={`View source: ${citation.filename}`}
        >
          {match[0]}
        </button>
      );
    } else {
      parts.push(match[0]);
    }

    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts;
}

// ── Main Component ───────────────────────────────────────

export function MarkdownRenderer({
  content,
  citations,
  onCitationClick,
}: MarkdownRendererProps) {
  // Handle malformed or empty content safely
  if (!content) {
    return null;
  }

  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // The `code` override handles inline code only (no language-xxx className).
          // For inline code, render with inline-code styling.
          // For fenced code blocks (language-xxx className), pass through the
          // original element so the `pre` override (above) can intercept it
          // and render a full CodeBlock component with syntax highlighting.
          code({ className, children, ...props }) {
            const match = /language-(\w+)/.exec(className || "");

            if (!match) {
              // Inline code — render with inline-code style
              return (
                <code className="md-inline-code" {...props}>
                  {children}
                </code>
              );
            }

            // Fenced code block — preserve original className so the `pre`
            // override can detect the language and render with syntax highlighting
            return (
              <code className={className}>
                {children}
              </code>
            );
          },

          // Override `pre` to render CodeBlock for fenced code blocks.
          // Our CodeBlock includes its own container with syntax highlighting.
          // Without this override, react-markdown's default <pre> would wrap
          // our CodeBlock, creating a nested <pre><div>...</div></pre> structure.
          // This override detects fenced blocks and renders CodeBlock directly.
          pre({ children }) {
            const codeChild = children
              ? Array.isArray(children)
                ? children[0]
                : children
              : null;

            if (
              codeChild &&
              typeof codeChild === "object" &&
              "props" in (codeChild as React.ReactElement)
            ) {
              const childProps = (codeChild as React.ReactElement).props;
              const className = childProps?.className || "";
              if (/^language-\w+/.test(className)) {
                return (
                  <CodeBlock
                    className={className}
                    children={childProps.children}
                  />
                );
              }
            }

            // Fallback: render as plain pre for inline code wraps or unknown blocks
            return <pre className="code-block-plain">{children}</pre>;
          },

          // Links
          a({ href, children }) {
            return <SafeLink href={href}>{children}</SafeLink>;
          },

          // Paragraph with citation processing
          p({ children }) {
            if (!citations || citations.length === 0 || !onCitationClick) {
              return <p className="md-paragraph">{children}</p>;
            }

            // Process string children for citation markers
            const processed = React.Children.map(children, (child) => {
              if (typeof child === "string") {
                const parts = splitTextWithCitations(
                  child,
                  citations,
                  onCitationClick
                );
                return parts.length === 1 ? parts[0] : parts;
              }
              return child;
            });

            // Flatten nested arrays from the map
            const flat = Array.isArray(processed)
              ? processed.flat()
              : processed;

            return <p className="md-paragraph">{flat}</p>;
          },

          // Headings
          h1: ({ children }) => <h1 className="md-h1">{children}</h1>,
          h2: ({ children }) => <h2 className="md-h2">{children}</h2>,
          h3: ({ children }) => <h3 className="md-h3">{children}</h3>,
          h4: ({ children }) => <h4 className="md-h4">{children}</h4>,
          h5: ({ children }) => <h5 className="md-h5">{children}</h5>,
          h6: ({ children }) => <h6 className="md-h6">{children}</h6>,

          // Lists
          ul: ({ children }) => <ul className="md-ul">{children}</ul>,
          ol: ({ children }) => <ol className="md-ol">{children}</ol>,
          li: ({ children }) => <li className="md-li">{children}</li>,

          // Blockquotes
          blockquote: ({ children }) => (
            <blockquote className="md-blockquote">{children}</blockquote>
          ),

          // Tables
          table: ({ children }) => (
            <div className="md-table-wrapper">
              <table className="md-table">{children}</table>
            </div>
          ),
          thead: ({ children }) => <thead className="md-thead">{children}</thead>,
          tbody: ({ children }) => <tbody className="md-tbody">{children}</tbody>,
          tr: ({ children }) => <tr className="md-tr">{children}</tr>,
          th: ({ children }) => <th className="md-th">{children}</th>,
          td: ({ children }) => <td className="md-td">{children}</td>,

          // Horizontal rules
          hr: () => <hr className="md-hr" />,

          // Inline formatting
          strong: ({ children }) => (
            <strong className="md-strong">{children}</strong>
          ),
          em: ({ children }) => <em className="md-em">{children}</em>,
          del: ({ children }) => <del className="md-del">{children}</del>,

          // Images are not allowed for security
          img: () => null,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export default MarkdownRenderer;

// Export components for testing
export { CodeBlock, CopyButton, SafeLink, isSafeUrl };
