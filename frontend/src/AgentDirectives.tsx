/**
 * AgentDirectives — UI for managing the AI's learned "Lessons Learned"
 * (F6 Capability 3). Lets the user see, toggle on/off, and delete the
 * persistent behavioural directives the agent saves across sessions.
 */
import { useCallback, useEffect, useState } from "react";
import { Trash2, BookOpen, CheckCircle2, CircleDashed } from "lucide-react";
import { API } from "./api";
import { Button } from "./components/ui/button";
import { cn } from "./lib/utils";
import type { AgentDirective } from "./types";

export function AgentDirectives() {
  const [directives, setDirectives] = useState<AgentDirective[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const loadDirectives = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDirectives(await API.listDirectives());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load directives");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDirectives();
  }, [loadDirectives]);

  const handleToggle = async (d: AgentDirective) => {
    setTogglingId(d.id);
    setError(null);
    try {
      const updated = await API.toggleDirective(d.id, !d.is_active);
      setDirectives((prev) =>
        prev.map((x) => (x.id === d.id ? updated : x))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to toggle directive");
    } finally {
      setTogglingId(null);
    }
  };

  const handleDelete = async (d: AgentDirective) => {
    if (!window.confirm("Delete this learned directive?")) return;
    setDeletingId(d.id);
    setError(null);
    try {
      await API.deleteDirective(d.id);
      setDirectives((prev) => prev.filter((x) => x.id !== d.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete directive");
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div>
      <div className="mb-4">
        <p className="text-sm text-muted-foreground">
          These are the standing rules the agent has learned across your chats
          ("Lessons Learned"). Active directives are injected into every future
          reply, so the agent remembers them. Turn a directive off to stop
          applying it, or delete it entirely.
        </p>
      </div>

      {error && <p className="mb-4 text-sm text-destructive">{error}</p>}

      {loading ? (
        <p className="py-4 text-center text-sm text-muted-foreground">Loading…</p>
      ) : directives.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-8 text-center">
          <BookOpen className="h-8 w-8 text-muted-foreground/60" />
          <p className="text-sm text-muted-foreground">
            No learned directives yet. They appear here automatically as the
            agent saves "lessons learned" during your conversations.
          </p>
        </div>
      ) : (
        <div className="grid max-h-[40vh] gap-2 overflow-y-auto pr-1">
          {directives.map((d) => (
            <div
              key={d.id}
              className={cn(
                "flex items-start gap-3 rounded-xl border p-3",
                d.is_active && "border-primary/40 bg-primary/5"
              )}
            >
              <div className="min-w-0 flex-1">
                <p className="text-sm leading-snug">{d.content}</p>
                <div className="mt-1.5 flex items-center gap-1.5">
                  {d.is_active ? (
                    <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                      <CheckCircle2 className="h-3 w-3" /> Active
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                      <CircleDashed className="h-3 w-3" /> Inactive
                    </span>
                  )}
                </div>
              </div>

              <Button
                size="sm"
                variant={d.is_active ? "default" : "outline"}
                onClick={() => handleToggle(d)}
                disabled={togglingId === d.id}
                title={d.is_active ? "Deactivate" : "Activate"}
              >
                {togglingId === d.id ? "…" : d.is_active ? "Disable" : "Enable"}
              </Button>

              <Button
                size="icon"
                variant="ghost"
                onClick={() => handleDelete(d)}
                disabled={deletingId === d.id}
                title="Delete"
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default AgentDirectives;
