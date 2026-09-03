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
  Image as ImageIcon,
  X,
  Paperclip,
  Upload,
  File as FileIcon,
} from "lucide-react";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { PlanCard } from "./PlanCard";
import { ConfidenceBadge } from "./ConfidenceBadge";
import { ModelSelector } from "./ModelSelector";
import { ProviderSwitcher } from "./ProviderSwitcher";
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
    return (
      <div className="flex flex-col gap-2">
        {msg.image_url && (
          <img
            src={msg.image_url}
            alt="User attached image"
            className="max-w-xs rounded-lg self-end"
          />
        )}
        <div className="whitespace-pre-wrap text-sm leading-relaxed">{msg.content}</div>
      </div>
    );
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
  onSend: (imageUrl?: string) => void;
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

  // Document upload
  documents?: import("./types").Document[];
  uploading?: boolean;
  uploadError?: string | null;
  onDocumentUpload?: (file: File) => void;
  onDeleteDocument?: (id: number) => void;

  // Tool approval (MCP / terminal)
  pendingToolApproval?: { tool: string; args: any; server?: string } | null;
  onApproveTool?: () => void;
  onRejectTool?: () => void;

  // F6 Capability 1 — plan preview (v1, read-only + cancel)
  planSteps?: Array<Record<string, unknown>>;
  onPlanCancel?: () => void;
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
  documents = [],
  uploading = false,
  uploadError = null,
  onDocumentUpload,
  onDeleteDocument,
  pendingToolApproval = null,
  onApproveTool,
  onRejectTool,
  planSteps = [],
  onPlanCancel,
}: ChatAreaProps) {
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const streamingEndRef = useRef<HTMLDivElement>(null);
  const inputContainerRef = useRef<HTMLDivElement>(null);
  const [isScrolledToBottom, setIsScrolledToBottom] = useState(true);
  const [attachedImage, setAttachedImage] = useState<string | null>(null);  // Base64 image data URL
  const [isDragging, setIsDragging] = useState(false);
  const dragCounterRef = useRef(0);
  const [providerRefreshKey, setProviderRefreshKey] = useState(0);

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

  // ── Document upload helpers ─────────────────
  const activeDoc = documents && documents.length > 0 ? documents[0] : null;
  const getDocStatusLabel = () => {
    if (uploading) return "Uploading...";
    if (!activeDoc) return null;
    if (activeDoc.status === "processing") return "Processing / Embedding...";
    if (activeDoc.status === "ready") return "Ready ✓";
    if (activeDoc.status === "failed") return "Failed ✕";
    return activeDoc.status;
  };
  const handleDocumentFile = (file: File) => {
    if (!onDocumentUpload) return;
    const allowed = [".pdf", ".txt", ".md", ".docx"];
    const ext = "." + file.name.split(".").pop()?.toLowerCase();
    if (!allowed.includes(ext) && !allowed.some((a) => file.name.toLowerCase().endsWith(a))) {
      return;
    }
    onDocumentUpload(file);
  };

  // ── Drag and Drop ───────────────────────────
  const handleDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current++;
    if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
      setIsDragging(true);
    }
  };
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };
  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current--;
    if (dragCounterRef.current <= 0) {
      setIsDragging(false);
      dragCounterRef.current = 0;
    }
  };
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
    dragCounterRef.current = 0;
    if (!activeSession || !onDocumentUpload) return;
    const files = Array.from(e.dataTransfer.files);
    const valid = files.find((f) => /\.(pdf|txt|md|docx)$/i.test(f.name));
    if (valid) handleDocumentFile(valid);
  };

  // ── Empty State (no active session) ──────
  if (!activeSession) {
    return (
      <main className="flex h-full min-w-0 flex-1 flex-col bg-background">
        <div className="flex flex-1 items-center justify-center px-4">
          <div className="text-center text-muted-foreground max-w-md">
            <div className="mb-4 text-6xl opacity-20">
              <MessageSquare className="mx-auto h-16 w-16" />
            </div>
            <h2 className="mb-2 text-xl font-medium text-foreground">Start a conversation with Thunder AI</h2>
            <p className="text-base">Select a session from the sidebar or create a new one to begin.</p>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main
      className="flex h-full min-w-0 flex-1 flex-col bg-background relative"
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* Drag Overlay */}
      {isDragging && activeSession && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm border-2 border-dashed border-primary/50 rounded-xl m-2 pointer-events-none">
          <div className="flex flex-col items-center gap-3 text-primary">
            <Upload className="h-10 w-10 animate-bounce" />
            <p className="text-sm font-medium">Drop PDF, TXT, MD or DOCX to upload</p>
            <p className="text-xs text-secondary">Will be indexed for RAG</p>
          </div>
        </div>
      )}
      {/* Header - Minimal */}
      <header className="relative z-10 flex items-center justify-between border-b px-4 py-3 bg-background/60 backdrop-blur-xl sticky top-0">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <MessageSquare className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <h1 className="truncate font-medium text-foreground">{activeSession.title}</h1>
            <p className="truncate text-xs text-secondary">
              {messages.length} message{messages.length !== 1 ? "s" : ""}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-1">
          {/* Quick Provider Switcher */}
          <ProviderSwitcher
            onProviderSwitch={() => setProviderRefreshKey((k) => k + 1)}
          />

          {/* Model Selector */}
          <ModelSelector
            key={activeSession.id}
            sessionId={activeSession.id}
            currentModel={sessionModel}
            onModelChange={onModelChange}
            refreshKey={providerRefreshKey}
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
                  <h1 className="mb-6 text-center text-2xl font-medium text-foreground">Ask Thunder AI anything</h1>
                  {/* Centered Input Box - Grok Style */}
                  <div className="input-glass rounded-2xl p-1.5 shadow-2xl">
                    {/* Document Status Pill */}
                    {(uploading || activeDoc) && (
                      <div className="relative mb-2 flex items-center gap-2 px-1">
                        <div className="flex items-center gap-2 rounded-full bg-white/5 border border-white/10 px-3 py-1.5 text-xs shadow-sm">
                          <FileIcon className="h-3.5 w-3.5 text-primary shrink-0" />
                          <span className="max-w-[160px] truncate font-medium">
                            📄 {activeDoc?.filename || "Uploading..."}
                          </span>
                          <span className={cn("text-[10px] px-1.5 py-0.5 rounded-full font-medium", uploading ? "bg-primary/20 text-primary animate-pulse" : activeDoc?.status==="ready" ? "bg-green-500/15 text-green-400" : activeDoc?.status==="failed" ? "bg-destructive/15 text-destructive" : "bg-white/10 text-secondary")}>
                            {getDocStatusLabel()}
                          </span>
                          {activeDoc && onDeleteDocument && (
                            <button onClick={()=>onDeleteDocument(activeDoc.id)} className="ml-1 p-0.5 rounded-full hover:bg-white/10 hover:text-destructive transition-colors" title="Remove document">
                              <X className="h-3 w-3" />
                            </button>
                          )}
                        </div>
                        {uploadError && <span className="text-xs text-destructive truncate">{uploadError}</span>}
                      </div>
                    )}
                    {/* Image preview */}
                    {attachedImage && (
                      <div className="relative mb-2 flex items-center gap-2 px-1">
                        <img
                          src={attachedImage}
                          alt="Attached image"
                          className="h-16 w-auto rounded-lg object-cover"
                        />
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => setAttachedImage(null)}
                          className="h-6 w-6 rounded-lg hover:bg-white/5"
                          title="Remove image"
                        >
                          <X className="h-3 w-3" />
                        </Button>
                      </div>
                    )}
                    <div className="flex items-end gap-2">
                      <textarea
                        ref={inputRef}
                        className="max-h-[160px] min-h-[56px] w-full flex-1 resize-none bg-transparent px-5 py-4 text-base leading-relaxed text-foreground placeholder:text-placeholder focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
                        placeholder="Ask Thunder AI anything…"
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
                        className="h-11 w-11 shrink-0 rounded-xl transition-colors hover:bg-white/5"
                      >
                        {speech.isListening ? (
                          <MicOff className="h-5 w-5" />
                        ) : (
                          <Mic className="h-5 w-5" />
                        )}
                      </Button>
                      <input
                        type="file"
                        accept="image/*"
                        className="hidden"
                        id="image-upload-centered"
                        onChange={(e) => {
                          const file = e.target.files?.[0];
                          if (file) {
                            const reader = new FileReader();
                            reader.onload = () => setAttachedImage(reader.result as string);
                            reader.readAsDataURL(file);
                          }
                          e.target.value = "";
                        }}
                        disabled={isStreaming || !!attachedImage}
                      />
                      <Button
                        size="icon"
                        variant="ghost"
                        onClick={() => document.getElementById("image-upload-centered")?.click()}
                        disabled={isStreaming || !!attachedImage}
                        title="Attach image"
                        className="h-11 w-11 shrink-0 rounded-xl transition-colors hover:bg-white/5"
                      >
                        <ImageIcon className="h-4 w-4" />
                      </Button>
                      {/* Paperclip - Document Upload */}
                      <input
                        type="file"
                        accept=".pdf,.txt,.md,.docx"
                        className="hidden"
                        id="doc-upload-centered"
                        onChange={(e) => {
                          const file = e.target.files?.[0];
                          if (file) handleDocumentFile(file);
                          e.target.value = "";
                        }}
                        disabled={isStreaming}
                      />
                      <Button
                        size="icon"
                        variant="ghost"
                        onClick={() => document.getElementById("doc-upload-centered")?.click()}
                        disabled={isStreaming}
                        title="Attach document (PDF, TXT, MD, DOCX)"
                        className="h-11 w-11 shrink-0 rounded-xl bg-white/5 border border-white/10 hover:bg-white/10 hover:border-primary/30 transition-all"
                      >
                        <Paperclip className="h-4 w-4" />
                      </Button>
<Button
                      className="h-11 w-11 shrink-0 rounded-xl bg-primary text-primary-foreground hover:bg-primary/90 transition-all active:scale-[0.98]"
                      onClick={() => {
                        onSend(attachedImage || undefined);
                        setAttachedImage(null);
                      }}
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
                  <div className="mt-6 text-center text-secondary">
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
                          ? "justify-end text-secondary"
                          : "text-secondary"
                      )}
                    >
                      <time dateTime={msg.created_at} className="text-secondary">{formatTime(msg.created_at)}</time>
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
                        <ConfidenceBadge
                          confidence={msg.confidence}
                          reason={msg.confidence_reason}
                        />
                      )}
                      {msg.role === "assistant" && (
                        <button
                          className={cn(
                            "inline-flex items-center gap-1 rounded-full px-2 py-0.5 transition-colors hover:text-primary",
                            tts.speakingId === msg.id ? "text-primary" : "text-secondary hover:text-primary"
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

                {/* F6 Capability 1 — Plan preview card */}
                {isStreaming && planSteps.length > 0 && onPlanCancel && (
                  <div className="flex justify-start">
                    <PlanCard
                      steps={planSteps}
                      onRun={() => {}}
                      onCancel={onPlanCancel}
                    />
                  </div>
                )}

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
                      <div className="mt-1 text-[10px] text-secondary">Generating…</div>
                    </div>
                  </div>
                )}

                {/* Generation Stopped */}
                {generationStopped && (
                  <div className="flex justify-center my-2 animate-message-in">
                    <span className="rounded-full bg-white/5 px-3 py-1 text-xs text-secondary">
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

                {/* Tool Approval Card (MCP / Terminal) */}
                {pendingToolApproval && (
                  <div className="my-3 animate-message-in flex justify-center">
                    <div className="w-full max-w-[85%] rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
                      <div className="flex items-center gap-2 text-sm font-medium text-amber-400">
                        <AlertTriangle className="h-4 w-4" />
                        Tool approval required
                      </div>
                      <div className="mt-2 text-xs font-mono bg-black/30 rounded p-2 overflow-x-auto">
                        <div className="text-white"><span className="text-muted-foreground">Tool:</span> {pendingToolApproval.tool}</div>
                        {pendingToolApproval.server && <div className="text-white"><span className="text-muted-foreground">Server:</span> {pendingToolApproval.server}</div>}
                        <div className="text-white"><span className="text-muted-foreground">Args:</span> {JSON.stringify(pendingToolApproval.args, null, 2)}</div>
                      </div>
                      <div className="mt-3 flex gap-2">
                        <Button size="sm" onClick={onApproveTool} className="bg-green-600 hover:bg-green-700 text-white">Approve & Run</Button>
                        <Button size="sm" variant="outline" onClick={onRejectTool}>Reject</Button>
                      </div>
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
                  <ChevronDown className="h-5 w-5 icon-primary" />
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
                {/* Document Status Pill */}
                {(uploading || activeDoc) && (
                  <div className="relative mb-2 flex items-center gap-2 px-1">
                    <div className="flex items-center gap-2 rounded-full bg-white/5 border border-white/10 px-3 py-1.5 text-xs shadow-sm">
                      <FileIcon className="h-3.5 w-3.5 text-primary shrink-0" />
                      <span className="max-w-[160px] truncate font-medium">
                        📄 {activeDoc?.filename || "Uploading..."}
                      </span>
                      <span className={cn("text-[10px] px-1.5 py-0.5 rounded-full font-medium", uploading ? "bg-primary/20 text-primary animate-pulse" : activeDoc?.status==="ready" ? "bg-green-500/15 text-green-400" : activeDoc?.status==="failed" ? "bg-destructive/15 text-destructive" : "bg-white/10 text-secondary")}>
                        {getDocStatusLabel()}
                      </span>
                      {activeDoc && onDeleteDocument && (
                        <button onClick={()=>onDeleteDocument(activeDoc.id)} className="ml-1 p-0.5 rounded-full hover:bg-white/10 hover:text-destructive transition-colors" title="Remove document">
                          <X className="h-3 w-3" />
                        </button>
                      )}
                    </div>
                    {uploadError && <span className="text-xs text-destructive truncate">{uploadError}</span>}
                  </div>
                )}
                {/* Image preview */}
                {attachedImage && (
                  <div className="relative mb-2 flex items-center gap-2 px-1">
                    <img
                      src={attachedImage}
                      alt="Attached image"
                      className="h-16 w-auto rounded-lg object-cover"
                    />
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={() => setAttachedImage(null)}
                      className="h-6 w-6 rounded-lg hover:bg-white/5"
                      title="Remove image"
                    >
                      <X className="h-3 w-3" />
                    </Button>
                  </div>
                )}
                <div className="flex items-end gap-2">
                  <textarea
                    ref={inputRef}
                    className="max-h-[160px] min-h-[48px] w-full flex-1 resize-none bg-transparent px-5 py-3 text-sm leading-relaxed text-foreground placeholder:text-placeholder focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
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
                  {/* Paperclip - Document Upload */}
                  <input
                    type="file"
                    accept=".pdf,.txt,.md,.docx"
                    className="hidden"
                    id="doc-upload"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) handleDocumentFile(file);
                      e.target.value = "";
                    }}
                    disabled={isStreaming}
                  />
                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={() => document.getElementById("doc-upload")?.click()}
                    disabled={isStreaming}
                    title="Attach document (PDF, TXT, MD, DOCX)"
                    className="h-10 w-10 shrink-0 rounded-xl bg-white/5 border border-white/10 hover:bg-white/10 hover:border-primary/30 hover:text-primary transition-all"
                  >
                    <Paperclip className="h-4 w-4" />
                  </Button>
                  {/* Image upload button */}
                  <input
                    type="file"
                    accept="image/*"
                    className="hidden"
                    id="image-upload"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) {
                        const reader = new FileReader();
                        reader.onload = () => setAttachedImage(reader.result as string);
                        reader.readAsDataURL(file);
                      }
                      // Reset input so same file can be selected again
                      e.target.value = "";
                    }}
                    disabled={isStreaming || !!attachedImage}
                  />
                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={() => document.getElementById("image-upload")?.click()}
                    disabled={isStreaming || !!attachedImage}
                    title="Attach image"
                    className="h-10 w-10 shrink-0 rounded-xl transition-colors hover:bg-white/5"
                  >
                    <ImageIcon className="h-4 w-4" />
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
                      onClick={() => {
                        onSend(attachedImage || undefined);
                        setAttachedImage(null);
                      }}
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
                  <div className="mt-2 text-center text-[11px] text-secondary">
                    {speech.error ? (
                      <span className="text-destructive">{speech.error}</span>
                    ) : (
                      <span>Click Stop to cancel — partial response will not be saved</span>
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