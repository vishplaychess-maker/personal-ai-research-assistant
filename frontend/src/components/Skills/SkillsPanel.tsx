/**
 * SkillsPanel — capability grid for Settings > Skills.
 *
 * Phase 2: preferences are stored per user via /api/capabilities (the static
 * catalog lives on the server). Execution is still not wired — the panel says
 * so — this only persists the enabled flag.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  FileText,
  Globe,
  RefreshCw,
  Search,
  Sparkles,
  Terminal,
  type LucideIcon,
} from "lucide-react";
import { API } from "../../api";
import type { Capability } from "../../types";
import { Button } from "../ui/button";
import { Card, CardContent } from "../ui/card";
import { Input } from "../ui/input";
import { Switch } from "../ui/switch";
import { cn } from "../../lib/utils";

const ICON_MAP: Record<string, LucideIcon> = {
  globe: Globe,
  terminal: Terminal,
  "file-text": FileText,
};

export function SkillsPanel() {
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setCapabilities(await API.getCapabilities());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load capabilities");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleToggle = async (capability: Capability) => {
    const nextEnabled = !capability.enabled;
    setPendingId(capability.id);
    setError(null);
    const previous = capabilities;
    // Optimistic flip; rollback on failure so the UI never claims a save
    // that the server rejected.
    setCapabilities((current) =>
      current.map((c) =>
        c.id === capability.id ? { ...c, enabled: nextEnabled } : c
      )
    );
    try {
      const updated = await API.updateCapability(capability.id, {
        enabled: nextEnabled,
      });
      setCapabilities((current) =>
        current.map((c) => (c.id === updated.id ? updated : c))
      );
    } catch (err) {
      setCapabilities(previous);
      setError(err instanceof Error ? err.message : "Failed to update capability");
    } finally {
      setPendingId(null);
    }
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return capabilities;
    return capabilities.filter((c) =>
      `${c.name} ${c.description}`.toLowerCase().includes(q)
    );
  }, [capabilities, query]);

  const enabledCount = capabilities.filter((c) => c.enabled).length;

  return (
    <section data-testid="capabilities-panel" className="mb-6">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            <h3 className="text-sm font-medium">Capabilities</h3>
            {!loading && !error && (
              <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {enabledCount} of {capabilities.length} enabled
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            Turn individual capabilities on or off. Preview only — execution is not
            wired yet. Changes are saved to your account.
          </p>
        </div>
        <div className="relative w-full shrink-0 sm:w-56">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search capabilities"
            aria-label="Search capabilities"
            className="h-8 pl-8 text-xs"
          />
        </div>
      </div>

      {error && (
        <div className="mb-3 flex items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2">
          <p className="text-xs text-destructive">{error}</p>
          <Button size="sm" variant="outline" onClick={load} className="shrink-0">
            <RefreshCw className="h-3.5 w-3.5" />
            Retry
          </Button>
        </div>
      )}

      {loading ? (
        <p className="py-6 text-center text-sm text-muted-foreground">Loading…</p>
      ) : capabilities.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No capabilities are available on this deployment.
        </p>
      ) : filtered.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No capabilities match “{query}”.
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {filtered.map((capability) => {
            const Icon = ICON_MAP[capability.icon] ?? Sparkles;
            return (
              <Card
                key={capability.id}
                className={cn(
                  "transition-colors",
                  capability.enabled && "border-primary/40 bg-primary/5"
                )}
              >
                <CardContent className="flex items-start gap-3 p-4">
                  <span
                    className={cn(
                      "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
                      capability.enabled
                        ? "bg-primary/10 text-primary"
                        : "bg-muted text-muted-foreground"
                    )}
                  >
                    <Icon className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">
                        {capability.name}
                      </span>
                      {pendingId === capability.id && (
                        <span className="shrink-0 text-[10px] text-muted-foreground">
                          Saving…
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                      {capability.description}
                    </p>
                  </div>
                  <Switch
                    checked={capability.enabled}
                    onCheckedChange={() => handleToggle(capability)}
                    disabled={pendingId !== null}
                    aria-label={`Enable ${capability.name}`}
                  />
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}

export default SkillsPanel;
