/**
 * ModelSelector — compact pill button + popover listing models from ALL the
 * user's configured providers, grouped by provider.
 *
 * Selecting a model marks that provider active (PUT /api/providers/{id}) and
 * persists the choice on the session (PATCH /api/sessions/{id}/model).
 */

import { useState, useEffect, useRef, useCallback } from "react";
import { Zap, ChevronDown, RefreshCw } from "lucide-react";
import { API } from "./api";
import { cn } from "./lib/utils";
import type { ModelInfo, ProviderModelGroup } from "./types";

// ── Types ─────────────────────────────────────────────────

interface ModelSelectorProps {
  sessionId: number;
  currentModel: string | null;
  onModelChange: (model: string | null) => void;
}

// ── Helpers ───────────────────────────────────────────────

/** Compact display name: "CohereLabs/c4ai-command-r7b-12-2024" -> "command r7b". */
function shortName(id: string): string {
  const seg = id.includes("/") ? id.split("/").pop()! : id;
  return seg.replace(/[-_]+/g, " ").replace(/\s+[\d.]+$/, "").trim() || id;
}

/** Small heuristic badges: "Fast" for small models, "Powerful" for big ones. */
function badgeFor(id: string): string | null {
  if (/(70b|120b|110b|405b|large|pro|ultra|max|super|nemotron)/i.test(id)) {
    return "Powerful";
  }
  if (/(3b|7b|8b|small|mini|nano|flash|lightning)/i.test(id)) {
    return "Fast";
  }
  return null;
}

// ── Component ─────────────────────────────────────────────

export function ModelSelector({
  sessionId,
  currentModel,
  onModelChange,
}: ModelSelectorProps) {
  const [groups, setGroups] = useState<ProviderModelGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const fetchModels = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await API.listAllProviderModels();
      setGroups(
        (data || []).map((g) => ({
          ...g,
          models: (g.models || []).filter((m) => !/embed/i.test(m.name)),
        }))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load models");
      setGroups([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchModels();
  }, [fetchModels]);

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const handleSelect = async (group: ProviderModelGroup | null, modelName: string) => {
    setSaving(true);
    setError(null);
    try {
      if (group) {
        // Routing: mark the owning provider active so chat uses its API key.
        await API.updateProvider(group.provider_id, { is_active: true });
      }
      await API.updateSessionModel(sessionId, modelName === "" ? null : modelName);
      onModelChange(modelName === "" ? null : modelName);
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save model");
    } finally {
      setSaving(false);
    }
  };

  const displayName = currentModel ? shortName(currentModel) : "Default model";

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        className="flex items-center gap-1.5 rounded-full border bg-background/70 px-3 py-1.5 text-xs font-medium shadow-sm backdrop-blur transition-colors hover:bg-accent/60"
        onClick={() => {
          setOpen(!open);
          if (!open) fetchModels();
        }}
        disabled={saving}
        title={currentModel ? `Model: ${currentModel}` : "Click to select a model"}
      >
        <Zap className="h-3.5 w-3.5 text-primary" />
        <span className="max-w-[140px] truncate">
          {saving ? "Saving…" : displayName}
        </span>
        <ChevronDown
          className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")}
        />
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-2 flex max-h-[70vh] w-80 flex-col overflow-hidden rounded-xl border bg-popover text-popover-foreground shadow-lg">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <span className="text-xs font-medium">Select model</span>
            <button
              className="rounded p-1 hover:bg-accent"
              onClick={fetchModels}
              disabled={loading}
              title="Refresh"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            </button>
          </div>

          {error && (
            <div className="border-b px-3 py-2 text-xs text-destructive">{error}</div>
          )}

          <div className="flex-1 overflow-y-auto p-2">
            <button
              className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-sm hover:bg-accent"
              onClick={() => handleSelect(null, "")}
            >
              <span>Default model</span>
              <span className="text-xs text-muted-foreground">Server default</span>
            </button>

            {loading && groups.length === 0 && (
              <p className="px-2 py-4 text-center text-xs text-muted-foreground">
                Loading models…
              </p>
            )}
            {!loading && groups.length === 0 && !error && (
              <p className="px-2 py-4 text-center text-xs text-muted-foreground">
                No providers configured. Add one in Settings.
              </p>
            )}

            {groups.map((g) => (
              <div key={g.provider_id} className="mt-2">
                <div className="flex items-center justify-between px-2 pb-1">
                  <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                    {g.provider_label}
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    {g.models.length}
                  </span>
                </div>
                {g.models.length === 0 ? (
                  <p className="px-2 pb-1 text-xs text-muted-foreground">
                    No models (check API key)
                  </p>
                ) : (
                  g.models.map((m) => {
                    const badge = badgeFor(m.name);
                    return (
                      <button
                        key={m.name}
                        className={cn(
                          "flex w-full items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left text-sm hover:bg-accent",
                          currentModel === m.name && "bg-accent/60"
                        )}
                        onClick={() => handleSelect(g, m.name)}
                      >
                        <span className="truncate">{shortName(m.name)}</span>
                        {badge && (
                          <span
                            className={cn(
                              "shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium",
                              badge === "Fast"
                                ? "bg-green-500/10 text-green-600 dark:text-green-400"
                                : "bg-primary/10 text-primary"
                            )}
                          >
                            {badge}
                          </span>
                        )}
                      </button>
                    );
                  })
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default ModelSelector;
