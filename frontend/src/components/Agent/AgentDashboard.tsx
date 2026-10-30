/**
 * AgentDashboard — live view of the OpenBot orchestrator and the current chat
 * turn's agent frames.
 *
 * Every frame comes from a real source: the task SSE stream
 * (`/api/agents/openbot/tasks/{id}/events`) or the chat turn's
 * `agent_status` / `plan` frames. There is no simulated activity.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Bot,
  Brain,
  ListTree,
  Loader2,
  Play,
  Search,
  Sparkles,
  Square,
  Terminal,
  Users,
  Wrench,
} from "lucide-react";
import { API } from "../../api";
import type { AgentEvent, OpenbotTaskStatusValue } from "../../types";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { cn } from "../../lib/utils";

interface TurnMetaLike {
  agentEvents: Array<{ agent: string; message: string; at: number }>;
  planSteps: Array<Record<string, unknown>>;
}

const EVENT_META: Record<
  AgentEvent["type"],
  { label: string; icon: typeof Activity; className: string }
> = {
  thinking: { label: "Thinking", icon: Brain, className: "text-sky-400" },
  plan: { label: "Plan", icon: ListTree, className: "text-indigo-400" },
  tool: { label: "Using tool", icon: Wrench, className: "text-amber-400" },
  output: { label: "Result", icon: Terminal, className: "text-emerald-400" },
  synthesis: { label: "Synthesizing", icon: Sparkles, className: "text-violet-400" },
  warning: { label: "Warning", icon: AlertTriangle, className: "text-destructive" },
  status: { label: "Status", icon: Activity, className: "text-muted-foreground" },
};

/** Swarm roles get their own icon + accent so handoffs are scannable. */
const ROLE_META: Record<
  string,
  { label: string; icon: typeof Activity; className: string; chip: string }
> = {
  planner: {
    label: "Planner",
    icon: Brain,
    className: "text-indigo-400",
    chip: "bg-indigo-500/10 text-indigo-500 ring-indigo-500/20 dark:text-indigo-300",
  },
  researcher: {
    label: "Researcher",
    icon: Search,
    className: "text-teal-400",
    chip: "bg-teal-500/10 text-teal-600 ring-teal-500/20 dark:text-teal-300",
  },
  synthesizer: {
    label: "Synthesizer",
    icon: Sparkles,
    className: "text-violet-400",
    chip: "bg-violet-500/10 text-violet-500 ring-violet-500/20 dark:text-violet-300",
  },
  swarm: {
    label: "Swarm",
    icon: Users,
    className: "text-fuchsia-400",
    chip: "bg-fuchsia-500/10 text-fuchsia-500 ring-fuchsia-500/20 dark:text-fuchsia-300",
  },
};

function statusClass(status: OpenbotTaskStatusValue | "idle") {
  if (status === "completed") return "bg-emerald-500/10 text-emerald-500";
  if (status === "running" || status === "pending")
    return "bg-primary/10 text-primary";
  if (status === "failed") return "bg-destructive/10 text-destructive";
  if (status === "cancelled") return "bg-muted text-muted-foreground";
  return "bg-muted text-muted-foreground";
}

export function AgentDashboard({ turnMeta }: { turnMeta?: TurnMetaLike }) {
  const [query, setQuery] = useState("");
  const [taskId, setTaskId] = useState<string | null>(null);
  const [status, setStatus] = useState<OpenbotTaskStatusValue | "idle">("idle");
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [launching, setLaunching] = useState(false);
  const [swarm, setSwarm] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const launch = useCallback(async () => {
    const prompt = query.trim();
    if (!prompt || launching) return;
    abortRef.current?.abort();
    setLaunching(true);
    setError(null);
    setEvents([]);
    setResult(null);
    setStatus("pending");
    setTaskId(null);
    try {
      const { task_id } = await API.startOpenbotResearch(prompt, swarm);
      setTaskId(task_id);
      setStatus("running");
      setLaunching(false);

      const controller = new AbortController();
      abortRef.current = controller;
      const seen = new Set<number>();
      await API.streamOpenbotEvents(
        task_id,
        (event) => {
          if (seen.has(event.seq)) return;
          seen.add(event.seq);
          setEvents((previous) => [...previous, event]);
          if (event.type === "status" && event.message) {
            setStatus(event.message as OpenbotTaskStatusValue);
          }
        },
        { signal: controller.signal }
      );
      const final = await API.getOpenbotTask(task_id);
      setStatus(final.status);
      setResult(final.result_text);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      const message = err instanceof Error ? err.message : "Could not run the agent";
      setError(
        message.includes("not enabled")
          ? "Autonomous Research (OpenBot) is not enabled. Turn it on under Settings → Skills."
          : message
      );
      setStatus("failed");
    } finally {
      setLaunching(false);
    }
  }, [launching, query, swarm]);

  const stop = useCallback(async () => {
    if (!taskId) return;
    try {
      const task = await API.cancelOpenbotTask(taskId);
      setStatus(task.status);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not cancel the task");
    }
  }, [taskId]);

  const running = status === "pending" || status === "running";
  const chatPlan = turnMeta?.planSteps ?? [];
  const chatAgents = turnMeta?.agentEvents ?? [];

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-border bg-card p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="flex items-center gap-1.5 text-[11px] font-medium uppercase text-muted-foreground">
            <Bot className="h-3.5 w-3.5" /> Autonomous agent
          </span>
          <span
            className={cn(
              "rounded-full px-1.5 py-0.5 text-[10px] font-medium",
              statusClass(status)
            )}
          >
            {status === "idle" ? "Idle" : status}
          </span>
        </div>
        <div className="flex gap-2">
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void launch();
            }}
            placeholder="e.g. Refactor the auth module"
            aria-label="Agent task"
            disabled={launching || running}
            className="h-8 text-xs"
          />
          <Button
            size="sm"
            variant={swarm ? "default" : "outline"}
            onClick={() => setSwarm((value) => !value)}
            disabled={launching || running}
            aria-pressed={swarm}
            title="Planner → Researcher → Synthesizer (falls back to single agent on failure)"
            className="shrink-0 gap-1.5"
          >
            <Users className="h-3.5 w-3.5" />
            Swarm
          </Button>
          <Button
            size="sm"
            onClick={() => void launch()}
            disabled={!query.trim() || launching || running}
            className="shrink-0"
          >
            {launching ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Play className="h-3.5 w-3.5" />
            )}
            Run
          </Button>
          {running ? (
            <Button
              size="sm"
              variant="outline"
              onClick={() => void stop()}
              className="shrink-0"
            >
              <Square className="h-3.5 w-3.5" />
              Stop
            </Button>
          ) : null}
        </div>
        {error ? <p className="mt-2 text-xs text-destructive">{error}</p> : null}
      </div>

      <div>
        <div className="mb-2 text-[11px] font-medium uppercase text-muted-foreground">
          Live activity
        </div>
        {events.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            Launch a task to stream the agent's plan, tool use, and results here.
          </p>
        ) : (
          <div className="max-h-[38vh] space-y-2 overflow-y-auto pr-1">
            {events.map((event) => {
              const meta = EVENT_META[event.type] ?? EVENT_META.status;
              const Icon = meta.icon;
              const role = ROLE_META[event.agent];
              const RoleIcon = role?.icon;
              return (
                <div
                  key={event.seq}
                  className="rounded-lg border border-border bg-card p-2.5"
                >
                  <div className="flex items-center gap-1.5 text-[11px] font-medium">
                    {role && RoleIcon ? (
                      <span
                        className={cn(
                          "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] ring-1",
                          role.chip
                        )}
                      >
                        <RoleIcon className="h-3 w-3" />
                        {role.label}
                      </span>
                    ) : null}
                    <Icon className={cn("h-3.5 w-3.5", meta.className)} />
                    <span className={meta.className}>{meta.label}</span>
                    <span className="truncate text-muted-foreground">
                      {role ? "" : event.agent}
                      {event.tool ? `${role ? "" : " · "}${event.tool}` : ""}
                    </span>
                  </div>
                  {event.message ? (
                    <p className="mt-1 text-[11px] text-foreground">{event.message}</p>
                  ) : null}
                  {event.output ? (
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/40 p-2 font-mono text-[10px] text-muted-foreground">
                      {event.output}
                    </pre>
                  ) : null}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {result ? (
        <div>
          <div className="mb-2 text-[11px] font-medium uppercase text-muted-foreground">
            Final result
          </div>
          <pre className="max-h-[40vh] overflow-auto whitespace-pre-wrap break-words rounded-xl border border-border bg-card p-2.5 text-[11px]">
            {result}
          </pre>
        </div>
      ) : null}

      <div>
        <div className="mb-2 text-[11px] font-medium uppercase text-muted-foreground">
          This chat turn
        </div>
        {chatPlan.length === 0 && chatAgents.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            No agent frames for this turn. Simple answers produce none.
          </p>
        ) : (
          <div className="space-y-1.5 rounded-xl border border-border bg-card p-3">
            {chatAgents.map((event, index) => (
              <div key={`agent-${event.at}-${index}`} className="text-[11px]">
                <span className="font-medium text-foreground">{event.agent}</span>
                <span className="text-muted-foreground"> · {event.message}</span>
              </div>
            ))}
            {chatPlan.map((step, index) => (
              <div key={`plan-${index}`} className="text-[11px] text-muted-foreground">
                {index + 1}. {String(step.step ?? step.action ?? "")}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default AgentDashboard;
