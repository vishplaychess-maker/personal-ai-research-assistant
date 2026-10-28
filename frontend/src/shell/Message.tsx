import { useState } from "react";
import { Brain, Check, ChevronDown, ChevronRight, Copy, FileText, Volume2, Zap } from "lucide-react";
import { ConfidenceBadge } from "../ConfidenceBadge";
import { MarkdownRenderer } from "../MarkdownRenderer";
import { cn } from "../lib/utils";
import type { Citation } from "../types";

/** One `agent_status` SSE frame as the stream produced it. */
export interface AgentEvent {
  agent: string;
  message: string;
  at: number;
}

export interface TurnMeta {
  startedAt: number | null;
  agentEvents: AgentEvent[];
  planSteps: Array<Record<string, unknown>>;
}

function parseCitations(raw: string | null | undefined): Citation[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function toPlainText(content: string): string {
  return content
    .replace(/```[\s\S]*?```/g, " code block ")
    .replace(/\[(\d+)\]/g, "")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/[*_#>~|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * The turn's real execution trace.
 *
 * Everything shown here comes from frames the server actually sent:
 * `agent_status {agent, message}` hand-offs, the `plan` steps, the elapsed
 * time between send and completion, and the citation count. There is no
 * "self-healed" counter because no frame carries that signal — inventing one
 * would be fake telemetry. The accordion renders nothing when a turn produced
 * neither agent frames nor a plan.
 */
export function ThoughtAccordion({
  meta,
  citationsCount,
  streaming,
  onOpenInspector,
}: {
  meta: TurnMeta;
  citationsCount: number;
  streaming: boolean;
  onOpenInspector?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const hasTrace = meta.agentEvents.length > 0 || meta.planSteps.length > 0;
  if (!hasTrace) return null;

  const elapsed = meta.startedAt ? Math.max(0, Math.round((Date.now() - meta.startedAt) / 1000)) : 0;
  const summary = streaming ? "Working" : `Thought for ${elapsed}s`;

  return (
    <div className="rounded-xl border border-border bg-card">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between px-3.5 py-2 text-xs text-muted-foreground"
      >
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-medium text-foreground">{summary}</span>
          {meta.agentEvents.length > 0 ? (
            <>
              <span>·</span>
              <span>{meta.agentEvents.length} agent step{meta.agentEvents.length === 1 ? "" : "s"}</span>
            </>
          ) : null}
          {meta.planSteps.length > 0 ? (
            <>
              <span>·</span>
              <span>{meta.planSteps.length} planned</span>
            </>
          ) : null}
          {citationsCount > 0 ? (
            <>
              <span>·</span>
              <span>{citationsCount} source{citationsCount === 1 ? "" : "s"}</span>
            </>
          ) : null}
        </span>
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
      </button>

      {open ? (
        <div className="space-y-2.5 border-t border-border px-3.5 py-2.5 text-xs">
          {meta.agentEvents.map((event, index) => (
            <div key={`${event.at}-${index}`} className="flex items-start gap-2">
              <Zap className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <div>
                <span className="font-medium text-foreground">{event.agent}</span>
                <span className="text-muted-foreground"> — {event.message}</span>
              </div>
            </div>
          ))}
          {meta.planSteps.map((step, index) => (
            <div key={`plan-${index}`} className="text-muted-foreground">
              {index + 1}. {String(step.step ?? step.action ?? "")}
              {step.target ? <span className="opacity-70"> → {String(step.target)}</span> : null}
            </div>
          ))}
          {onOpenInspector ? (
            <button
              type="button"
              onClick={onOpenInspector}
              className="text-[11px] text-muted-foreground underline underline-offset-2 hover:text-foreground"
            >
              Open the Inspector
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function UserMessage({ content, imageUrl }: { content: string; imageUrl?: string | null }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-3xl bg-secondary px-5 py-3 text-[15px] leading-relaxed text-foreground sm:max-w-[78%]">
        {imageUrl ? (
          <img src={imageUrl} alt="Attached" className="mb-2 max-w-xs rounded-lg" />
        ) : null}
        <div className="whitespace-pre-wrap">{content}</div>
      </div>
    </div>
  );
}

export function AssistantMessage({
  content,
  citations,
  streaming = false,
  onCitationClick,
  thought,
  createdAt,
  showRag = false,
  showMemory = false,
  confidence = null,
  confidenceReason = null,
}: {
  content: string;
  citations?: string | null;
  streaming?: boolean;
  onCitationClick: (citation: Citation) => void;
  thought?: React.ReactNode;
  createdAt?: string;
  showRag?: boolean;
  showMemory?: boolean;
  confidence?: number | null;
  confidenceReason?: string | null;
}) {
  const [copied, setCopied] = useState(false);
  const parsed = parseCitations(citations);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard needs a secure context; leave the text selectable instead.
    }
  };

  const speak = () => {
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.speak(
      new SpeechSynthesisUtterance(toPlainText(content))
    );
  };

  return (
    <div className="flex items-start gap-4">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-border bg-card">
        <Zap className="h-3.5 w-3.5" />
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        {createdAt || showRag || showMemory || confidence != null ? (
          <div className="flex flex-wrap items-center gap-2 text-[10px] text-secondary">
            {createdAt ? <time dateTime={createdAt}>{new Date(createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time> : null}
            {showRag ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 font-medium text-primary">
                <FileText className="h-3 w-3" /> RAG
              </span>
            ) : null}
            {showMemory ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 font-medium text-primary">
                <Brain className="h-3 w-3" /> Memory
              </span>
            ) : null}
            {confidence != null ? (
              <ConfidenceBadge confidence={confidence} reason={confidenceReason ?? undefined} />
            ) : null}
          </div>
        ) : null}
        {thought}
        <div className="prose max-w-none text-[15px] leading-7">
          <MarkdownRenderer
            content={content}
            citations={parsed}
            onCitationClick={onCitationClick}
          />
          {streaming ? (
            <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-primary align-text-bottom" />
          ) : null}
        </div>

        {!streaming && content ? (
          <div className={cn("flex items-center gap-1 pt-1 text-muted-foreground")}>
            <button
              type="button"
              onClick={copy}
              className="rounded-lg p-1.5 hover:bg-secondary hover:text-foreground"
              title={copied ? "Copied" : "Copy response"}
              aria-label="Copy response"
            >
              {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
            </button>
            <button
              type="button"
              onClick={speak}
              className="rounded-lg p-1.5 hover:bg-secondary hover:text-foreground"
              title="Read aloud"
              aria-label="Read aloud"
            >
              <Volume2 className="h-4 w-4" />
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
