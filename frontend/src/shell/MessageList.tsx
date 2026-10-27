import { Square } from "lucide-react";
import type { Message, Citation } from "../types";
import { AssistantMessage, ThoughtAccordion, UserMessage, type TurnMeta } from "./Message";

/**
 * Message stage: user pills, assistant blocks, and the live streaming bubble.
 *
 * The Thought accordion is attached to the assistant message of the turn that
 * produced the frames. Frames are not persisted, so after a reload the
 * accordion is simply absent — which is honest: there is no trace to show.
 */
export interface MessageListProps {
  messages: Message[];
  streamedContent: string;
  isStreaming: boolean;
  agentProgress?: string | null;
  onCitationClick: (citation: Citation) => void;
  onCancel?: () => void;
  turnMeta?: TurnMeta;
  onOpenInspector?: () => void;
  sourcesUsedIds?: Set<number>;
  memoriesUsedIds?: Set<number>;
}

const PROGRESS_LABELS: Record<string, string> = {
  agent_researching: "Researching…",
  agent_research_complete: "Research complete",
  agent_solving: "Developing solution…",
  agent_solution_complete: "Solution complete",
  agent_reviewing: "Reviewing…",
  agent_finalizing: "Finalizing…",
};

function citationCount(raw: string | null | undefined): number {
  if (!raw) return 0;
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.length : 0;
  } catch {
    return 0;
  }
}

export function MessageList({
  messages,
  streamedContent,
  isStreaming,
  agentProgress,
  onCitationClick,
  onCancel,
  turnMeta,
  onOpenInspector,
  sourcesUsedIds,
  memoriesUsedIds,
}: MessageListProps) {
  const lastAssistantId = [...messages].reverse().find((m) => m.role === "assistant")?.id ?? null;
  const lastAssistant = messages.find((m) => m.id === lastAssistantId) ?? null;
  const liveCitations = citationCount(lastAssistant?.citations);

  return (
    <div className="mx-auto max-w-3xl space-y-8 py-6">
      {messages.map((message) => {
        if (message.role === "user") {
          return (
            <UserMessage
              key={message.id}
              content={message.content}
              imageUrl={message.image_url}
            />
          );
        }
        const isLast = message.id === lastAssistantId;
        return (
          <AssistantMessage
            key={message.id}
            content={message.content}
            citations={message.citations}
            onCitationClick={onCitationClick}
            createdAt={message.created_at}
            showRag={Boolean(sourcesUsedIds?.has(message.id))}
            showMemory={Boolean(memoriesUsedIds?.has(message.id))}
            confidence={message.confidence ?? null}
            confidenceReason={message.confidence_reason ?? null}
            thought={
              isLast && !isStreaming && turnMeta ? (
                <ThoughtAccordion
                  meta={turnMeta}
                  citationsCount={citationCount(message.citations)}
                  streaming={false}
                  onOpenInspector={onOpenInspector}
                />
              ) : null
            }
          />
        );
      })}

      {isStreaming ? (
        <div className="space-y-2">
          <AssistantMessage
            content={streamedContent}
            streaming
            onCitationClick={onCitationClick}
            thought={
              turnMeta ? (
                <ThoughtAccordion
                  meta={turnMeta}
                  citationsCount={liveCitations}
                  streaming
                  onOpenInspector={onOpenInspector}
                />
              ) : null
            }
          />
          <div className="flex items-center gap-2 pl-11 text-[11px] text-muted-foreground">
            <span role="status">
              {PROGRESS_LABELS[agentProgress ?? ""] ?? "Generating…"}
            </span>
            {onCancel ? (
              <button
                type="button"
                onClick={onCancel}
                className="ml-auto flex items-center gap-1.5 rounded-full border border-border px-2.5 py-1 hover:bg-secondary hover:text-foreground"
                title="Stop generating"
              >
                <Square className="h-3 w-3 fill-current" />
                Stop
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
