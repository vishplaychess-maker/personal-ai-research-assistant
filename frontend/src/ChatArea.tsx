/**
 * ChatArea — Main chat area with header, messages, streaming, input.
 * White/Blue theme (Tailwind + shadcn/ui): blue user bubbles, white AI cards,
 * blue focus ring, blue send button, fade-in messages, pulsing typing dots.
 */
import { useRef, useEffect } from "react";
import {
  MessageSquare,
  Bot,
  Settings2,
  FileText,
  Brain,
  Power,
  NotebookPen,
  Send,
  Square,
  AlertTriangle,
  RotateCcw,
} from "lucide-react";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ModelSelector } from "./ModelSelector";
import { Button } from "./components/ui/button";
import { cn } from "./lib/utils";
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
    return <div className="message-content whitespace-pre-wrap">{msg.content}</div>;
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
  activeSession: Session | null;
  messages: Message[];
  sourcesUsedIds: Set<number>;
  memoriesUsedIds: Set<number>;
  onCitationClick: (citation: Citation) => void;

  input: string;
  onInputChange: (value: string) => void;
  onSend: () => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
  inputRef: React.RefObject<HTMLTextAreaElement> | React.MutableRefObject<HTMLTextAreaElement | null>;

  isStreaming: boolean;
  streamedContent: string;
  generationStopped: boolean;
  onCancelStream: () => void;

  chatError: string | null;
  retryTarget: RetryTarget | null;
  onRetry: () => void;

  sessionModel: string | null;
  onModelChange: (model: string | null) => void;
  onOpenSystemPrompt: () => void;

  showDocs: boolean;
  docCount: number;
  onToggleDocs: () => void;

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

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (isStreaming && streamedContent) {
      streamingEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [streamedContent, isStreaming]);

  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = "auto";
      inputRef.current.style.height =
        Math.min(inputRef.current.scrollHeight, 120) + "px";
    }
  }, [input, inputRef]);

  if (!activeSession) {
    return (
      <main className="flex h-full min-w-0 flex-1 flex-col bg-background">
        <div className="flex flex-1 items-center justify-center">
          <div className="text-center text-muted-foreground">
            <div className="mb-3 text-5xl">
              <MessageSquare className="mx-auto h-12 w-12 opacity-40" />
            </div>
            <div className="text-lg font-medium">Select or create a session</div>
            <div className="text-sm">Click “New” to start a research conversation</div>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="flex h-full min-w-0 flex-1 flex-col bg-background">
      {/* Header */}
      <div className="flex items-center gap-2 border-b px-4 py-2">
        <span className="flex items-center gap-2 font-medium text-foreground">
          <MessageSquare className="h-4 w-4 text-primary" />
          <span className="max-w-[180px] truncate">{activeSession.title}</span>
          <span className="text-xs text-muted-foreground">
            {messages.length} msg{messages.length !== 1 ? "s" : ""}
          </span>
        </span>

        <div className="ml-auto flex items-center gap-1.5">
          <ModelSelector
            key={activeSession.id}
            sessionId={activeSession.id}
            currentModel={sessionModel}
            onModelChange={onModelChange}
          />

          <Button
            size="icon"
            variant="ghost"
            onClick={onOpenSystemPrompt}
            title="Edit system prompt"
          >
            <Settings2 className="h-4 w-4" />
          </Button>

          <Button
            size="icon"
            variant={showDocs ? "secondary" : "ghost"}
            onClick={onToggleDocs}
            title="Toggle document panel"
            className="relative"
          >
            <FileText className="h-4 w-4" />
            {docCount > 0 && (
              <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[9px] font-semibold text-primary-foreground">
                {docCount}
              </span>
            )}
          </Button>

          <div className="flex items-center gap-1">
            <span
              className={cn(
                "mr-0.5 flex items-center gap-1 text-xs",
                !memorySettingLoaded
                  ? "text-muted-foreground"
                  : memoryEnabled
                    ? "text-green-600 dark:text-green-400"
                    : "text-muted-foreground"
              )}
            >
              <Brain className="h-3.5 w-3.5" />
              {!memorySettingLoaded ? "Sync…" : memoryEnabled ? "On" : "Off"}
            </span>
            <Button
              size="icon"
              variant="ghost"
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
              <Power className="h-4 w-4" />
            </Button>
            <Button
              size="icon"
              variant={showMemories ? "secondary" : "ghost"}
              onClick={onToggleMemories}
              title="Open memory panel"
              disabled={!memorySettingLoaded || !memoryEnabled}
              className="relative"
            >
              <NotebookPen className="h-4 w-4" />
              {memoriesLength > 0 && (
                <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[9px] font-semibold text-primary-foreground">
                  {memoriesLength}
                </span>
              )}
            </Button>
          </div>
        </div>
      </div>

      {/* Messages */}
      <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-4 py-4">
        {messages.length === 0 ? (
          <div className="flex flex-1 items-center justify-center">
            <div className="text-center text-muted-foreground">
              <div className="mb-3">
                <Bot className="mx-auto h-12 w-12 opacity-40" />
              </div>
              <div className="text-lg font-medium">Start a conversation</div>
              <div className="text-sm">Ask anything about your research topic</div>
            </div>
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.id}
              className={cn(
                "flex animate-message-in",
                msg.role === "user" ? "justify-end" : "justify-start"
              )}
            >
              <div
                className={cn(
                  "max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
                  msg.role === "user"
                    ? "rounded-br-sm bg-gradient-to-br from-blue-600 to-blue-500 text-white shadow-sm"
                    : "rounded-bl-sm border bg-card text-card-foreground shadow-sm"
                )}
              >
                {renderContent(msg, onCitationClick)}
                <div
                  className={cn(
                    "mt-1 flex items-center gap-2 text-[10px]",
                    msg.role === "user" ? "text-blue-100/80" : "text-muted-foreground"
                  )}
                >
                  <span>{formatTime(msg.created_at)}</span>
                  {msg.role === "assistant" && sourcesUsedIds.has(msg.id) && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                      <FileText className="h-3 w-3" /> RAG
                    </span>
                  )}
                  {msg.role === "assistant" && memoriesUsedIds.has(msg.id) && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                      <Brain className="h-3 w-3" /> Memory
                    </span>
                  )}
                </div>
              </div>
            </div>
          ))
        )}

        {isStreaming && (
          <div className="flex animate-message-in justify-start">
            <div className="max-w-[85%] rounded-2xl rounded-bl-sm border bg-card px-4 py-2.5 text-sm leading-relaxed text-card-foreground shadow-sm">
              {streamedContent ? (
                <div className="whitespace-pre-wrap">
                  {streamedContent}
                  <span className="ml-0.5 inline-block h-4 w-2 animate-pulse bg-primary align-text-bottom" />
                </div>
              ) : (
                <div className="flex items-center gap-1.5 py-1">
                  <span className="typing-dot" />
                  <span className="typing-dot" />
                  <span className="typing-dot" />
                </div>
              )}
              <div className="mt-1 text-[10px] text-muted-foreground">
                Generating…
              </div>
            </div>
          </div>
        )}

        {generationStopped && (
          <div className="flex justify-center">
            <span className="rounded-full bg-muted px-3 py-1 text-xs text-muted-foreground">
              ⏹ Generation stopped — partial response was not saved
            </span>
          </div>
        )}

        {chatError && (
          <div className="flex flex-col gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-3">
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4" />
              <span>{chatError}</span>
            </div>
            {retryTarget && !isStreaming && (
              <Button
                size="sm"
                variant="outline"
                className="w-fit"
                onClick={onRetry}
                title="Retry with the same message"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                Retry
              </Button>
            )}
          </div>
        )}

        <div ref={messagesEndRef} />
        <div ref={streamingEndRef} />
      </div>

      {/* Input */}
      <div className="border-t p-3">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            className="max-h-[120px] min-h-[44px] w-full flex-1 resize-none rounded-xl border bg-transparent px-4 py-2.5 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
            placeholder={isStreaming ? "Generating response…" : "Type your message…"}
            value={input}
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={isStreaming}
            rows={1}
          />
          {isStreaming ? (
            <Button
              variant="destructive"
              className="h-11 px-4"
              onClick={onCancelStream}
              title="Stop generation"
            >
              <Square className="h-4 w-4" />
              Stop
            </Button>
          ) : (
            <Button
              className="h-11 w-11 rounded-xl"
              onClick={onSend}
              disabled={!input.trim() || isStreaming}
              title="Send message"
            >
              <Send className="h-4 w-4" />
            </Button>
          )}
        </div>
        {isStreaming && (
          <div className="mt-1.5 text-center text-[11px] text-muted-foreground">
            Click Stop to cancel — partial response will not be saved
          </div>
        )}
      </div>
    </main>
  );
}

export default ChatArea;
