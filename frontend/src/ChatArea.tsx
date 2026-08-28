/**
 * ChatArea — Grok.com style chat interface.
 * Centered empty state with floating input, transitions to bottom input on first message.
 * Monochrome dark aesthetic with subtle indigo accents.
 */
import { useRef, useEffect, useMemo, useState } from "react";
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
  Mic,
  MicOff,
  Volume2,
  VolumeX,
  Sparkles,
  ChevronDown,
} from "lucide-react";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ModelSelector } from "./ModelSelector";
import { Button } from "./components/ui/button";
import { cn } from "./lib/utils";
import { useSpeechRecognition } from "./hooks/useSpeechRecognition";
import { useSpeechSynthesis } from "./hooks/useSpeechSynthesis";
import type { Session, Message, Citation, RetryTarget } from "./types";

// ── Helpers ───────────────────────────────────────────────

function formatTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function toPlainText(content: string): string {
  return content
    .replace(/```[\s\S]*?```/g, " code block ")
    .replace(/\[(Source:[^\]]*)\]/gi, "")
    .replace(/\[(\d+)\]/g, "")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_#>~|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function renderContent(
  msg: Message,
  onCitationClick: (citation: Citation) => void
) {
  if (msg.role !== "assistant") {
    return <div className="whitespace-pre-wrap text-sm leading-relaxed">{msg.content}</div>;
  }

  let parsedCitations: Citation[] = [];
  try {
    parsedCitations = msg.citations ? JSON.parse(msg.citations) : [];
  } catch {
    parsedCitations = [];
  }

  return (
    <div className="prose max-w-none text-sm leading-relaxed">
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
  const inputContainerRef = useRef<HTMLDivElement>(null);
  const [isScrolledToBottom, setIsScrolledToBottom] = useState(true);

  const indicators = useMemo(() => ({
    hasSources: sourcesUsedIds.size > 0,
    hasMemories: memoriesUsedIds.size > 0,
  }), [sourcesUsedIds, memoriesUsedIds]);

  const hasMessages = messages.length > 0;

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    setIsScrolledToBottom(true);
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
        Math.min(inputRef.current.scrollHeight, 160) + "px";
    }
  }, [input, inputRef]);

  // Track scroll position to show/hide scroll-to-bottom indicator
  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const { scrollTop, scrollHeight, clientHeight } = e.currentTarget;
    setIsScrolledToBottom(scrollHeight - scrollTop - clientHeight < 50);
  };

  // ── Voice input ─────────────────────
  const micPrefixRef = useRef("");
  const speech = useSpeechRecognition((text) => {
    onInputChange(
      micPrefixRef.current
        ? `${micPrefixRef.current} ${text}`.trim()
        : text
    );
  });
  const handleMicToggle = () => {
    if (!speech.isListening) micPrefixRef.current = input;
    else micPrefixRef.current = "";
    speech.toggle();
  };

  // ── Voice output ────────────────────
  const tts = useSpeechSynthesis();

  // ── Empty State (no active session) ──────
  if (!activeSession) {
    return (
      <main className="flex h-full min-w-0 flex-1 flex-col bg-background">
        <div className="flex flex-1 items-center justify-center px-4">
          <div className="text-center text-muted-foreground max-w-md">
            <div className="mb-4 text-6xl opacity-20">
              <MessageSquare className="mx-auto h-16 w-16" />
            </div>
            <h2 className="mb-2 text-xl font-medium text-foreground">Start a research conversation</h2>
            <p className="text-base">Select a session from the sidebar or create a new one to begin.</p>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="flex h-full min-w-0 flex-1 flex-col bg-background relative">
      {/* Header - Minimal */}
      <header className="relative z-10 flex items-center justify-between border-b px-4 py-3 bg-background/60 backdrop-blur-xl sticky top-0">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <MessageSquare className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <h1 className="truncate font-medium text-foreground">{activeSession.title}</h1>
            <p className="truncate text-xs text-muted-foreground">
              {messages.length} message{messages.length !== 1 ? "s" : ""}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-1">
          {/* Model Selector */}
          <ModelSelector
            key={activeSession.id}
            sessionId={activeSession.id}
            currentModel={sessionModel}
            onModelChange={onModelChange}
          />

          {/* System Prompt */}
          <Button
            size="icon"
            variant="ghost"
            onClick={onOpenSystemPrompt}
            title="Edit system prompt"
            className="h-9 w-9 rounded-xl transition-colors hover:bg-white/5"
          >
            <Settings2 className="h-4 w-4" />
          </Button>

          {/* Documents Toggle */}
          <Button
            size="icon"
            variant={showDocs ? "default" : "ghost"}
            onClick={onToggleDocs}
            title="Toggle document panel"
            className="h-9 w-9 rounded-xl relative transition-all"
          >
            <FileText className="h-4 w-4" />
            {docCount > 0 && (
              <span className="absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[9px] font-semibold text-primary-foreground">
                {docCount > 9 ? "9+" : docCount}
              </span>
            )}
          </Button>

          {/* Memory Controls - Minimal */}
          <div className="flex items-center gap-1 ml-1">
            <Button
              size="icon"
              variant="ghost"
              onClick={onToggleMemory}
              disabled={togglePending || !memorySettingLoaded}
              title={!memorySettingLoaded ? "Loading…" : memoryEnabled ? "Disable memory" : "Enable memory"}
              className="h-9 w-9 rounded-xl transition-colors hover:bg-white/5"
            >
              <Power className="h-4 w-4" />
            </Button>
            <Button
              size="icon"
              variant={showMemories ? "default" : "ghost"}
              onClick={onToggleMemories}
              title="Open memory panel"
              disabled={!memorySettingLoaded || !memoryEnabled}
              className="h-9 w-9 rounded-xl relative transition-all"
            >
              <NotebookPen className="h-4 w-4" />
              {memoriesLength > 0 && (
                <span className="absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[9px] font-semibold text-primary-foreground">
                  {memoriesLength > 9 ? "9+" : memoriesLength}
                </span>
              )}
            </Button>
          </div>
        </div>
      </header>

      {/* Messages Area */}
      <div className="relative z-10 flex flex-1 flex-col overflow-hidden">
        <div 
          className="flex flex-1 overflow-y-auto px-4 py-6"
          onScroll={handleScroll}
        >
          <div className="mx-auto w-full max-w-3xl">
            {/* Welcome State - Centered when no messages */}
            {!hasMessages && !isStreaming && (
              <div className="flex flex-1 items-center justify-center px-4 min-h-[calc(100vh-200px)]">
                <div className="w-full max-w-xl animate-fade-in-up">
                  {/* Centered Input Box - Grok Style */}
                  <div className="input-glass rounded-2xl p-1.5 shadow-2xl">
                    <div className="flex items-end gap-2">
                      <textarea
                        ref={inputRef}
                        className="max-h-[160px] min-h-[56px] w-full flex-1 resize-none bg-transparent px-5 py-4 text-base leading-relaxed text-foreground placeholder:text-muted-foreground/50 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
                        placeholder="Ask anything about your research…"
                        value={input}
                        onChange={(e) => onInputChange(e.target.value)}
                        onKeyDown={onKeyDown}
                        disabled={isStreaming}
                        rows={1}
                        spellCheck={false}
                      />
                      <Button
                        size="icon"
                        variant="ghost"
                        onClick={handleMicToggle}
                        disabled={isStreaming}
                        title={speech.isListening ? "Stop listening" : "Speak (voice input)"}
                        className="h-11 w-11 shrink-0 rounded-xl transition-colors hover:bg-white/5"
                      >
                        {speech.isListening ? (
                          <MicOff className="h-5 w-5" />
                        ) : (
                          <Mic className="h-5 w-5" />
                        )}
                      </Button>
                      <Button
                        className="h-11 w-11 shrink-0 rounded-xl bg-primary text-primary-foreground hover:bg-primary/90 transition-all active:scale-[0.98]"
                        onClick={onSend}
                        disabled={!input.trim() || isStreaming}
                        title="Send message"
                        aria-label="Send message"
                      >
                        <Send className="h-5 w-5" />
                        <span className="sr-only">Send</span>
                      </Button>
                    </div>
                    {(isStreaming || speech.error) && (
                      <div className="mt-2 text-center text-[11px]">
                        {speech.error ? (
                          <span className="text-destructive">{speech.error}</span>
                        ) : (
                          <span className="text-muted-foreground/50">Click Stop to cancel — partial response will not be saved</span>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Welcome Text Above Input */}
                  <div className="mt-6 text-center text-muted-foreground/60">
                    <p className="text-sm">Press <kbd className="px-1.5 py-0.5 rounded bg-white/5 font-mono text-xs">Enter</kbd> to send, <kbd className="px-1.5 py-0.5 rounded bg-white/5 font-mono text-xs">Shift+Enter</kbd> for new line</p>
                  </div>
                </div>
              </div>
            )}

            {/* Active Chat - Messages with bottom input */}
            {hasMessages && (
              <>
                {messages.map((msg) => (
                  <div
                    key={msg.id}
                    className={cn(
                      "animate-message-in",
                      msg.role === "user" ? "flex justify-end" : "flex justify-start"
                    )}
                  >
                    <div
                      className={cn(
                        "max-w-[85%] text-sm leading-relaxed",
                        msg.role === "user"
                          ? "rounded-2xl rounded-tr-md bg-white/5 px-4 py-2.5 text-foreground"
                          : "w-full px-1 text-white"
                      )}
                    >
                      {renderContent(msg, onCitationClick)}
                      <div
                        className={cn(
                          "mt-1.5 flex items-center gap-1.5 text-[10px]",
                          msg.role === "user"
                            ? "justify-end text-muted-foreground/50"
                            : "text-muted-foreground/60"
                        )}
                      >
                        <time dateTime={msg.created_at}>{formatTime(msg.created_at)}</time>
                        {msg.role === "assistant" && indicators.hasSources && sourcesUsedIds.has(msg.id) && (
                          <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
                            <FileText className="h-3 w-3" />
                            RAG
                          </span>
                        )}
                        {msg.role === "assistant" && indicators.hasMemories && memoriesUsedIds.has(msg.id) && (
                          <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
                            <Brain className="h-3 w-3" />
                            Memory
                          </span>
                        )}
                        {msg.role === "assistant" && (
                          <button
                            className={cn(
                              "inline-flex items-center gap-1 rounded-full px-2 py-0.5 transition-colors hover:text-primary",
                              tts.speakingId === msg.id ? "text-primary" : "text-muted-foreground/50 hover:text-muted-foreground"
                            )}
                            onClick={() => tts.speak(msg.id, toPlainText(msg.content))}
                            title={tts.speakingId === msg.id ? "Stop reading" : "Read aloud"}
                          >
                            {tts.speakingId === msg.id ? (
                              <>
                                <VolumeX className="h-3 w-3" />
                                <span className="hidden sm:inline">Stop</span>
                              </>
                            ) : (
                              <>
                                <Volume2 className="h-3 w-3" />
                                <span className="hidden sm:inline">Read</span>
                              </>
                            )}
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                ))}

                {/* Streaming Message */}
                {isStreaming && (
                  <div className="animate-message-in flex justify-start">
                    <div className="w-full max-w-[85%] px-1 py-1 text-sm leading-relaxed text-white">
                      {streamedContent ? (
                        <div className="whitespace-pre-wrap">
                          {streamedContent}
                          <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-primary align-text-bottom" />
                        </div>
                      ) : (
                        <div className="flex items-center gap-1.5 py-2">
                          <span className="typing-dot" />
                          <span className="typing-dot" />
                          <span className="typing-dot" />
                        </div>
                      )}
                      <div className="mt-1 text-[10px] text-muted-foreground/60">Generating…</div>
                    </div>
                  </div>
                )}

                {/* Generation Stopped */}
                {generationStopped && (
                  <div className="flex justify-center my-2 animate-message-in">
                    <span className="rounded-full bg-white/5 px-3 py-1 text-xs text-white/70">
                      ⏹ Generation stopped — partial response was not saved
                    </span>
                  </div>
                )}

                {/* Error Display */}
                {chatError && (
                  <div className="my-2 animate-message-in flex justify-center">
                    <div className="w-full max-w-[85%] rounded-xl border border-destructive/20 bg-destructive/5 p-3">
                      <div className="flex items-center gap-2 text-sm text-destructive">
                        <AlertTriangle className="h-4 w-4 shrink-0" />
                        <span className="text-destructive/90">{chatError}</span>
                      </div>
                      {retryTarget && !isStreaming && (
                        <Button
                          size="sm"
                          variant="outline"
                          className="mt-2 w-fit"
                          onClick={onRetry}
                          title="Retry with the same message"
                        >
                          <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
                          Retry
                        </Button>
                      )}
                    </div>
                  </div>
                )}

                <div ref={messagesEndRef} />
                <div ref={streamingEndRef} />
              </>
            )}

            {/* Scroll to bottom indicator */}
            {hasMessages && !isScrolledToBottom && !isStreaming && (
              <div className="fixed bottom-28 right-6 z-20 animate-fade-in-up">
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })}
                  className="h-10 w-10 rounded-full bg-white/5 border border-white/10 shadow-lg hover:bg-white/10"
                  aria-label="Scroll to bottom"
                >
                  <ChevronDown className="h-5 w-5" />
                </Button>
              </div>
            )}
          </div>
        </div>

        {/* Bottom Input Area - Only when messages exist */}
        {hasMessages && (
          <div className="relative z-10 px-4 pb-6">
            <div className="mx-auto max-w-3xl">
              <div className="input-glass rounded-2xl p-1.5 shadow-2xl">
                <div className="flex items-end gap-2">
                  <textarea
                    ref={inputRef}
                    className="max-h-[160px] min-h-[48px] w-full flex-1 resize-none bg-transparent px-5 py-3 text-sm leading-relaxed text-foreground placeholder:text-muted-foreground/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
                    placeholder={isStreaming ? "Generating response…" : "Type your message…"}
                    value={input}
                    onChange={(e) => onInputChange(e.target.value)}
                    onKeyDown={onKeyDown}
                    disabled={isStreaming}
                    rows={1}
                    spellCheck={false}
                  />
                  <Button
                    size="icon"
                    variant={speech.isListening ? "default" : "ghost"}
                    onClick={handleMicToggle}
                    disabled={isStreaming}
                    title={speech.isListening ? "Stop listening" : "Speak (voice input)"}
                    className="h-10 w-10 shrink-0 rounded-xl transition-colors hover:bg-white/5"
                  >
                    {speech.isListening ? (
                      <MicOff className="h-4 w-4" />
                    ) : (
                      <Mic className="h-4 w-4" />
                    )}
                  </Button>
                  {isStreaming ? (
                    <Button
                      variant="destructive"
                      className="h-10 px-4 rounded-xl"
                      onClick={onCancelStream}
                      title="Stop generation"
                    >
                      <Square className="h-4 w-4 mr-1.5" />
                      Stop
                    </Button>
                  ) : (
                    <Button
                      className="h-10 w-10 shrink-0 rounded-xl bg-primary text-primary-foreground hover:bg-primary/90 transition-all active:scale-[0.98]"
                      onClick={onSend}
                      disabled={!input.trim() || isStreaming}
                      title="Send message"
                      aria-label="Send message"
                    >
                      <Send className="h-4 w-4" />
                      <span className="sr-only">Send</span>
                    </Button>
                  )}
                </div>
                {(isStreaming || speech.error) && (
                  <div className="mt-2 text-center text-[11px]">
                    {speech.error ? (
                      <span className="text-destructive">{speech.error}</span>
                    ) : (
                      <span className="text-muted-foreground/50">Click Stop to cancel — partial response will not be saved</span>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}

export default ChatArea;