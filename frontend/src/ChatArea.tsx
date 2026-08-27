/**
 * ChatArea — Main chat area with header, messages, streaming, input.
 *
 * Extracted from App.tsx in Phase 5C.
 *
 * Props are deliberately kept flat and explicit rather than bundling
 * state objects, making the component's data dependencies clear.
 * This also makes it easier to test: you can verify exactly which
 * callbacks fire for each interaction.
 *
 * The parent (App.tsx) manages all state and passes handlers down.
 * ChatArea is a mostly-presentational component.
 */

import { useRef, useEffect, useCallback } from "react";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ModelSelector } from "./ModelSelector";
import type { Session, Message, Citation, RetryTarget } from "./types";

// ── Helpers ───────────────────────────────────────────────

function formatTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderContent(
  msg: Message,
  onCitationClick: (citation: Citation) => void
) {
  if (msg.role !== "assistant") {
    return <div className="message-content">{msg.content}</div>;
  }

  let parsedCitations: Citation[] = [];
  try {
    parsedCitations = msg.citations ? JSON.parse(msg.citations) : [];
  } catch {
    parsedCitations = [];
  }

  return (
    <div className="message-content">
      <MarkdownRenderer
        content={msg.content}
        citations={parsedCitations}
        onCitationClick={onCitationClick}
      />
    </div>
  );
}

// ── Types ─────────────────────────────────────────────────

interface ChatAreaProps {
  // Session
  activeSession: Session | null;

  // Messages
  messages: Message[];
  sourcesUsedIds: Set<number>;
  memoriesUsedIds: Set<number>;
  onCitationClick: (citation: Citation) => void;

  // Input
  input: string;
  onInputChange: (value: string) => void;
  onSend: () => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
  inputRef: React.RefObject<HTMLTextAreaElement> | React.MutableRefObject<HTMLTextAreaElement | null>;

  // Streaming
  isStreaming: boolean;
  streamedContent: string;
  generationStopped: boolean;
  onCancelStream: () => void;

  // Error / Retry
  chatError: string | null;
  retryTarget: RetryTarget | null;
  onRetry: () => void;

  // Model selector
  sessionModel: string | null;
  onModelChange: (model: string | null) => void;

  // System prompt
  onOpenSystemPrompt: () => void;

  // Document panel toggle
  showDocs: boolean;
  docCount: number;
  onToggleDocs: () => void;

  // Memory toggle group
  memorySettingLoaded: boolean;
  memoryEnabled: boolean;
  togglePending: boolean;
  memoriesLength: number;
  showMemories: boolean;
  onToggleMemory: () => void;
  onToggleMemories: () => void;
}

// ── Component ─────────────────────────────────────────────

export function ChatArea({
  activeSession,
  messages,
  sourcesUsedIds,
  memoriesUsedIds,
  onCitationClick,
  input,
  onInputChange,
  onSend,
  onKeyDown,
  inputRef,
  isStreaming,
  streamedContent,
  generationStopped,
  onCancelStream,
  chatError,
  retryTarget,
  onRetry,
  sessionModel,
  onModelChange,
  onOpenSystemPrompt,
  showDocs,
  docCount,
  onToggleDocs,
  memorySettingLoaded,
  memoryEnabled,
  togglePending,
  memoriesLength,
  showMemories,
  onToggleMemory,
  onToggleMemories,
}: ChatAreaProps) {
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const streamingEndRef = useRef<HTMLDivElement>(null);

  // Scroll on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Scroll during streaming
  useEffect(() => {
    if (isStreaming && streamedContent) {
      streamingEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [streamedContent, isStreaming]);

  // Auto-resize textarea
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = "auto";
      inputRef.current.style.height =
        Math.min(inputRef.current.scrollHeight, 120) + "px";
    }
  }, [input, inputRef]);

  if (!activeSession) {
    return (
      <main className="chat-area">
        <div className="messages-container">
          <div className="messages-empty">
            <div className="messages-empty-icon">💬</div>
            <div className="messages-empty-text">Select or create a session</div>
            <div className="messages-empty-hint">
              Click &quot;✚ New&quot; to start a new research conversation
            </div>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="chat-area">
      {/* Chat header */}
      <div className="chat-header">
        <span className="chat-header-icon">💬</span>
        <span className="chat-header-title">{activeSession.title}</span>
        <span className="chat-header-count">
          {messages.length} msg{messages.length !== 1 ? "s" : ""}
        </span>

        <ModelSelector
          key={activeSession.id}
          sessionId={activeSession.id}
          currentModel={sessionModel}
          onModelChange={onModelChange}
        />

        <button
          className="sp-toggle-btn"
          onClick={onOpenSystemPrompt}
          title="Edit system prompt"
        >
          ⚙
        </button>

        <button
          className={`doc-toggle-btn ${showDocs ? "active" : ""}`}
          onClick={onToggleDocs}
          title="Toggle document panel"
        >
          📄{docCount > 0 && <span className="doc-count-badge">{docCount}</span>}
        </button>

        <div className="mem-toggle-group">
          <span
            className={`mem-status ${
              memorySettingLoaded ? (memoryEnabled ? "enabled" : "disabled") : "loading"
            }`}
          >
            {!memorySettingLoaded
              ? "⏳ Sync…"
              : memoryEnabled
                ? "🧠 On"
                : "🧠 Off"}
          </span>
          <button
            className="mem-power-btn"
            onClick={onToggleMemory}
            disabled={togglePending || !memorySettingLoaded}
            title={
              !memorySettingLoaded
                ? "Loading…"
                : memoryEnabled
                  ? "Disable memory"
                  : "Enable memory"
            }
          >
            {togglePending
              ? "⏳"
              : !memorySettingLoaded
                ? "⋯"
                : memoryEnabled
                  ? "⏻"
                  : "⏼"}
          </button>
          <button
            className={`mem-toggle-btn ${showMemories ? "active" : ""}`}
            onClick={onToggleMemories}
            title="Open memory panel"
            disabled={!memorySettingLoaded || !memoryEnabled}
          >
            📝{memoriesLength > 0 && <span className="mem-count-badge">{memoriesLength}</span>}
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="messages-container">
        {messages.length === 0 ? (
          <div className="messages-empty">
            <div className="messages-empty-icon" style={{ fontSize: "2rem" }}>
              🤖
            </div>
            <div className="messages-empty-text">Start a conversation</div>
            <div className="messages-empty-hint">
              Ask anything about your research topic
            </div>
          </div>
        ) : (
          messages.map((msg) => (
            <div key={msg.id} className={`message-wrapper ${msg.role}`}>
              <div className="message-bubble">
                {renderContent(msg, onCitationClick)}
                <div className="message-time">
                  {formatTime(msg.created_at)}
                  {msg.role === "assistant" && sourcesUsedIds.has(msg.id) && (
                    <span className="badge rag-badge">📄 RAG</span>
                  )}
                  {msg.role === "assistant" && memoriesUsedIds.has(msg.id) && (
                    <span className="badge mem-badge">🧠 Memory</span>
                  )}
                </div>
              </div>
            </div>
          ))
        )}

        {isStreaming && (
          <div className="message-wrapper assistant">
            <div className="message-bubble streaming">
              <div className="message-content streaming-text">
                {streamedContent ? (
                  <>
                    {streamedContent}
                    <span className="streaming-cursor">▊</span>
                  </>
                ) : (
                  <span className="streaming-cursor">▊</span>
                )}
              </div>
              <div className="message-time">
                <span className="streaming-badge">⏳ Generating…</span>
              </div>
            </div>
          </div>
        )}

        {generationStopped && (
          <div className="generation-stopped">
            ⏹ Generation stopped — partial response was not saved
          </div>
        )}

        {chatError && (
          <div className="error-message">
            <div className="error-message-content">
              <span className="error-message-icon">⚠️</span>
              <span>{chatError}</span>
            </div>
            {retryTarget && !isStreaming && (
              <button
                className="retry-btn"
                onClick={onRetry}
                title="Retry with the same message"
              >
                🔄 Retry
              </button>
            )}
          </div>
        )}

        <div ref={messagesEndRef} />
        <div ref={streamingEndRef} />
      </div>

      {/* Input */}
      <div className="input-area">
        <div className="input-row">
          <textarea
            ref={inputRef}
            className="input-field"
            placeholder={isStreaming ? "Generating response…" : "Type your message…"}
            value={input}
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={isStreaming}
            rows={1}
          />
          {isStreaming ? (
            <button className="stop-btn" onClick={onCancelStream} title="Stop generation">
              ⏹ Stop
            </button>
          ) : (
            <button
              className="send-btn"
              onClick={onSend}
              disabled={!input.trim() || isStreaming}
              title="Send message"
            >
              ➤
            </button>
          )}
        </div>
        {isStreaming && (
          <div className="streaming-hint">
            Click Stop to cancel — partial response will not be saved
          </div>
        )}
      </div>
    </main>
  );
}

export default ChatArea;
