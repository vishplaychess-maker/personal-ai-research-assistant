/**
 * ShareCard — public, unauthenticated landing page for a shared agent.
 *
 * Rendered directly from main.tsx when the URL matches /share/agents/{id},
 * *before* the auth gate, so anyone with the link can view it.
 *
 * Fetch is deliberately a plain `fetch` (no auth headers, no auto-refresh/
 * logout wiring from the API helper) because this page is public.
 */
import { useEffect, useState } from "react";
import {
  CalendarClock,
  MessageSquare,
  Eye,
  Wrench,
  Sparkles,
  ArrowUpRight,
} from "lucide-react";
import type { PublicSharedAgent } from "./types";

interface ShareCardProps {
  shareId: string;
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; agent: PublicSharedAgent };

function ShareCard({ shareId }: ShareCardProps) {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/share/agents/${encodeURIComponent(shareId)}`);
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: "Not found" }));
          throw new Error(body.detail || "Not found");
        }
        const agent: PublicSharedAgent = await res.json();
        if (!cancelled) setState({ status: "ready", agent });
      } catch (err) {
        if (!cancelled) {
          setState({
            status: "error",
            message: err instanceof Error ? err.message : "Could not load this agent",
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [shareId]);

  if (state.status === "loading") {
    return (
      <Shell>
        <div className="flex items-center justify-center py-24 text-muted-foreground">
          <span className="mr-2 h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
          Loading agent…
        </div>
      </Shell>
    );
  }

  if (state.status === "error") {
    return (
      <Shell>
        <div className="mx-auto max-w-md py-24 text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl border border-white/10 bg-card">
            <Sparkles className="h-6 w-6 text-muted-foreground" />
          </div>
          <h1 className="mb-2 text-xl font-semibold">Agent not found</h1>
          <p className="text-sm text-muted-foreground">
            This shared agent doesn&apos;t exist or the link is no longer valid.
          </p>
        </div>
      </Shell>
    );
  }

  const { agent } = state;
  const displayPrompt =
    agent.system_prompt || "You are a helpful research assistant. Answer clearly and concisely.";

  return (
    <Shell>
      <div className="mx-auto w-full max-w-2xl">
        {/* Badge */}
        <div className="mb-4 flex items-center justify-center gap-2">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-indigo-400/30 bg-indigo-500/10 px-3 py-1 text-xs font-medium text-indigo-300">
            <Sparkles className="h-3.5 w-3.5" />
            Shared Agent by Thunder AI
          </span>
        </div>

        {/* Card */}
        <div className="overflow-hidden rounded-3xl border border-white/10 bg-card shadow-xl shadow-black/20">
          {/* Cover strip */}
          <div className="h-2 bg-gradient-to-r from-indigo-500 via-violet-500 to-indigo-500" />

          <div className="p-6 sm:p-8">
            {/* Title */}
            <h1 className="text-2xl font-bold leading-tight text-foreground sm:text-3xl">
              {agent.title}
            </h1>

            {/* Model chip */}
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-white/5 px-3 py-1 text-xs text-foreground">
                Model: {agent.model || "Default"}
              </span>
              {agent.has_schedule && (
                <span className="inline-flex items-center gap-1.5 rounded-full bg-white/5 px-3 py-1 text-xs text-foreground">
                  <CalendarClock className="h-3.5 w-3.5 text-indigo-300" />
                  Scheduled
                </span>
              )}
            </div>

            {/* System prompt */}
            <div className="mt-6">
              <div className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
                System Prompt
              </div>
              <div className="whitespace-pre-wrap rounded-2xl bg-white/[0.03] border border-white/10 px-4 py-3 text-sm leading-relaxed text-foreground/90">
                {displayPrompt}
              </div>
            </div>

            {/* Preview message (first user message) */}
            {agent.preview_message && (
              <div className="mt-6">
                <div className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
                  <MessageSquare className="h-3.5 w-3.5" />
                  Conversation preview
                </div>
                <div className="rounded-2xl rounded-tl-sm border border-indigo-400/20 bg-indigo-500/[0.08] px-4 py-3 text-sm leading-relaxed text-foreground">
                  {agent.preview_message}
                </div>
              </div>
            )}

            {/* Stats */}
            <div className="mt-8 grid grid-cols-3 gap-3 border-t border-white/10 pt-5 text-center">
              <div>
                <div className="flex items-center justify-center gap-1 text-lg font-semibold text-foreground">
                  <Eye className="h-4 w-4 text-muted-foreground" />
                  {agent.views}
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground">Views</div>
              </div>
              <div>
                <div className="flex items-center justify-center gap-1 text-lg font-semibold text-foreground">
                  <Wrench className="h-4 w-4 text-muted-foreground" />
                  {agent.tool_count}
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground">Tools</div>
              </div>
              <div>
                <div className="flex items-center justify-center gap-1 text-lg font-semibold text-foreground">
                  <CalendarClock className="h-4 w-4 text-muted-foreground" />
                  {agent.has_schedule ? "On" : "Off"}
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground">Schedule</div>
              </div>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="mt-6 flex items-center justify-center gap-2 text-center text-sm text-muted-foreground">
          <ArrowUpRight className="h-4 w-4" />
          Built with Thunder AI — clone this agent and make it yours
        </div>
      </div>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-background text-foreground transition-colors duration-300">
      <div className="flex flex-col px-4 py-14 sm:px-6">
        {children}
      </div>
    </div>
  );
}

export default ShareCard;
