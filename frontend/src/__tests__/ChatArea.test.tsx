/**
 * Tests for the ChatArea component.
 *
 * Phase 5C — Search and Frontend Refactoring
 */
import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ChatArea } from "../ChatArea";
import type { Session, Message, Citation } from "../types";

// JSDOM does not implement scrollIntoView
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

// Mock MarkdownRenderer to avoid testing it here (it has its own tests)
vi.mock("../MarkdownRenderer", () => ({
  MarkdownRenderer: ({
    content,
    citations,
    onCitationClick,
  }: {
    content: string;
    citations: Citation[];
    onCitationClick: (c: Citation) => void;
  }) => (
    <div data-testid="markdown-renderer">
      <span data-testid="content">{content}</span>
      {citations.map((c) => (
        <button key={c.marker} onClick={() => onCitationClick(c)}>
          {c.marker}
        </button>
      ))}
    </div>
  ),
}));

// Mock ModelSelector
vi.mock("../ModelSelector", () => ({
  ModelSelector: () => <div data-testid="model-selector" />,
}));

describe("ChatArea", () => {
  const mockSession: Session = {
    id: 1,
    title: "Test Chat",
    user_id: 1,
    model: null,
    system_prompt: null,
    created_at: "2026-07-17T10:00:00Z",
    updated_at: "2026-07-17T10:30:00Z",
  };

  const mockMessages: Message[] = [
    {
      id: 1,
      session_id: 1,
      role: "user",
      content: "Hello, can you help me?",
      created_at: "2026-07-17T10:00:00Z",
    },
    {
      id: 2,
      session_id: 1,
      role: "assistant",
      content: "Of course! I'm here to help.",
      citations: JSON.stringify([
        {
          marker: "[1]",
          document_id: 1,
          filename: "doc.pdf",
          snippet: "Helpful info",
        },
      ]),
      created_at: "2026-07-17T10:01:00Z",
    },
  ];

  const baseProps = {
    activeSession: mockSession,
    messages: mockMessages,
    sourcesUsedIds: new Set<number>(),
    memoriesUsedIds: new Set<number>(),
    onCitationClick: vi.fn(),
    input: "",
    onInputChange: vi.fn(),
    onSend: vi.fn(),
    onKeyDown: vi.fn(),
    inputRef: { current: null } as React.RefObject<HTMLTextAreaElement>,
    isStreaming: false,
    streamedContent: "",
    generationStopped: false,
    onCancelStream: vi.fn(),
    chatError: null as string | null,
    retryTarget: null as { message: string; errorDetail: string } | null,
    onRetry: vi.fn(),
    sessionModel: null as string | null,
    onModelChange: vi.fn(),
    onOpenSystemPrompt: vi.fn(),
    showDocs: false,
    docCount: 0,
    onToggleDocs: vi.fn(),
    memorySettingLoaded: true,
    memoryEnabled: true,
    togglePending: false,
    memoriesLength: 2,
    showMemories: false,
    onToggleMemory: vi.fn(),
    onToggleMemories: vi.fn(),
  };

  // ── Empty State ────────────────────────────────────────

  it("shows empty state when no session is selected", () => {
    render(<ChatArea {...baseProps} activeSession={null} />);
    expect(screen.getByText("Select or create a session")).toBeTruthy();
  });

  it("shows start conversation prompt when session is selected but no messages", () => {
    render(<ChatArea {...baseProps} messages={[]} />);
    expect(screen.getByText("Start a conversation")).toBeTruthy();
  });

  // ── Chat Header ────────────────────────────────────────

  it("renders session title in header", () => {
    render(<ChatArea {...baseProps} />);
    expect(screen.getByText("Test Chat")).toBeTruthy();
  });

  it("shows message count", () => {
    render(<ChatArea {...baseProps} />);
    expect(screen.getByText("2 msgs")).toBeTruthy();
  });

  it("renders the system prompt button", () => {
    render(<ChatArea {...baseProps} />);
    const spBtn = screen.getByTitle("Edit system prompt");
    expect(spBtn).toBeTruthy();
  });

  it("renders the document toggle button", () => {
    render(<ChatArea {...baseProps} />);
    const docBtn = screen.getByTitle("Toggle document panel");
    expect(docBtn).toBeTruthy();
  });

  it("renders memory status", () => {
    render(<ChatArea {...baseProps} />);
    expect(screen.getByText("🧠 On")).toBeTruthy();
  });

  // ── Messages ───────────────────────────────────────────

  it("renders user messages", () => {
    render(<ChatArea {...baseProps} />);
    expect(screen.getByText("Hello, can you help me?")).toBeTruthy();
  });

  it("renders assistant messages with markdown", () => {
    render(<ChatArea {...baseProps} />);
    expect(screen.getByTestId("markdown-renderer")).toBeTruthy();
  });

  it("shows RAG badge for messages with sources", () => {
    render(<ChatArea {...baseProps} sourcesUsedIds={new Set([2])} />);
    expect(screen.getByText("📄 RAG")).toBeTruthy();
  });

  it("shows Memory badge for messages with memories used", () => {
    render(<ChatArea {...baseProps} memoriesUsedIds={new Set([2])} />);
    expect(screen.getByText("🧠 Memory")).toBeTruthy();
  });

  it("renders citation markers as clickable buttons", () => {
    render(<ChatArea {...baseProps} />);
    expect(screen.getByText("[1]")).toBeTruthy();
  });

  it("calls onCitationClick when a citation is clicked", () => {
    const onCitationClick = vi.fn();
    render(<ChatArea {...baseProps} onCitationClick={onCitationClick} />);
    fireEvent.click(screen.getByText("[1]"));
    expect(onCitationClick).toHaveBeenCalledOnce();
  });

  // ── Input ──────────────────────────────────────────────

  it("renders the input field", () => {
    render(<ChatArea {...baseProps} />);
    expect(screen.getByPlaceholderText("Type your message…")).toBeTruthy();
  });

  it("renders the send button", () => {
    render(<ChatArea {...baseProps} />);
    const sendBtn = screen.getByTitle("Send message");
    expect(sendBtn).toBeTruthy();
  });

  it("shows custom input text", () => {
    render(<ChatArea {...baseProps} input="Custom message text" />);
    const textarea = screen.getByPlaceholderText("Type your message…");
    expect((textarea as HTMLTextAreaElement).value).toBe("Custom message text");
  });

  // ── Streaming ──────────────────────────────────────────

  it("shows streaming indicator when isStreaming is true", () => {
    render(<ChatArea {...baseProps} isStreaming={true} streamedContent="Generating…" />);
    expect(screen.getByText("⏳ Generating…")).toBeTruthy();
  });

  it("shows stop button during streaming", () => {
    render(<ChatArea {...baseProps} isStreaming={true} />);
    expect(screen.getByTitle("Stop generation")).toBeTruthy();
  });

  it("disables input during streaming", () => {
    render(<ChatArea {...baseProps} isStreaming={true} />);
    const textarea = screen.getByPlaceholderText("Generating response…");
    expect((textarea as HTMLTextAreaElement).disabled).toBe(true);
  });

  // ── Generation Stopped ─────────────────────────────────

  it("shows generation stopped message", () => {
    render(<ChatArea {...baseProps} generationStopped={true} />);
    expect(screen.getByText(/Generation stopped/)).toBeTruthy();
  });

  // ── Error / Retry ──────────────────────────────────────

  it("shows chat error message", () => {
    render(<ChatArea {...baseProps} chatError="Failed to send message" />);
    expect(screen.getByText("Failed to send message")).toBeTruthy();
  });

  it("shows retry button when retryTarget is set", () => {
    render(
      <ChatArea
        {...baseProps}
        chatError="Ollama unavailable"
        retryTarget={{ message: "Hello", errorDetail: "Ollama unavailable" }}
      />
    );
    expect(screen.getByText("🔄 Retry")).toBeTruthy();
  });

  it("calls onRetry when retry button is clicked", () => {
    const onRetry = vi.fn();
    render(
      <ChatArea
        {...baseProps}
        chatError="Error"
        retryTarget={{ message: "Hello", errorDetail: "Error" }}
        onRetry={onRetry}
      />
    );
    fireEvent.click(screen.getByText("🔄 Retry"));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  // ── Model Selector ─────────────────────────────────────

  it("renders model selector", () => {
    render(<ChatArea {...baseProps} />);
    expect(screen.getByTestId("model-selector")).toBeTruthy();
  });

  // ── Open System Prompt ─────────────────────────────────

  it("calls onOpenSystemPrompt when gear button is clicked", () => {
    const onOpenSystemPrompt = vi.fn();
    render(<ChatArea {...baseProps} onOpenSystemPrompt={onOpenSystemPrompt} />);
    fireEvent.click(screen.getByTitle("Edit system prompt"));
    expect(onOpenSystemPrompt).toHaveBeenCalledOnce();
  });
});
