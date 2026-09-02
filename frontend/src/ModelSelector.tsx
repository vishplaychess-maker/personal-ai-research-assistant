/**
 * ModelSelector — Minimal model picker (Grok style).
 * Clean dropdown, grouped by provider, subtle animations.
 * Monochrome dark aesthetic with indigo accents.
 * Uses a portal to render dropdown at body level for proper z-index.
 * Enhanced with FREE badge and Only Free toggle.
 */
import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Zap, ChevronDown, RefreshCw, Sparkles, Filter } from "lucide-react";
import { API } from "./api";
import { cn } from "./lib/utils";
import { createPortal } from "react-dom";
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

function isFreeModel(model: ModelInfo, provider: string): boolean {
  if (typeof model.is_free === "boolean") return model.is_free;
  // Fallback inference for backwards compatibility
  if (provider.toLowerCase() === "ollama" || provider.toLowerCase() === "local") return true;
  if (model.name.toLowerCase().includes(":free")) return true;
  return false;
}

// ── Dropdown Content Component (for portal) ───────────────

interface DropdownContentProps {
  groups: ProviderModelGroup[];
  currentModel: string | null;
  loading: boolean;
  error: string | null;
  open: boolean;
  onClose: () => void;
  onRefresh: () => void;
  onSelect: (group: ProviderModelGroup | null, modelName: string) => void;
  saving: boolean;
  triggerRect: DOMRect | null;
  showOnlyFree: boolean;
  onToggleFree: (v: boolean) => void;
}

function DropdownContent({
  groups,
  currentModel,
  loading,
  error,
  open,
  onClose,
  onRefresh,
  onSelect,
  saving,
  triggerRect,
  showOnlyFree,
  onToggleFree,
}: DropdownContentProps) {
  if (!open || !triggerRect) return null;

  // Calculate position: dropdown appears below the trigger, right-aligned
  const top = triggerRect.bottom + 8; // mt-2 = 8px
  const left = triggerRect.right - 288; // w-72 = 288px, right-aligned

  // Filter groups when toggle is on
  const filteredGroups = useMemo(() => {
    if (!showOnlyFree) return groups;
    return groups
      .map((g) => ({
        ...g,
        models: g.models.filter((m) => isFreeModel(m, g.provider)),
      }))
      .filter((g) => g.models.length > 0);
  }, [groups, showOnlyFree]);

  const totalModels = groups.reduce((acc, g) => acc + g.models.length, 0);
  const filteredTotal = filteredGroups.reduce((acc, g) => acc + g.models.length, 0);

  return createPortal(
    <div
      className="fixed z-[100] flex max-h-[70vh] w-72 flex-col overflow-hidden rounded-xl border border-white/5 bg-background text-foreground shadow-2xl backdrop-blur-xl pointer-events-auto"
      role="listbox"
      style={{ top: `${top}px`, left: `${left}px` }}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-white/10 px-3 py-2.5">
        <span className="text-xs font-semibold text-foreground">Select Model</span>
        <div className="flex items-center gap-1">
          <button
            className="h-7 w-7 rounded-lg icon-muted hover:icon-secondary hover:bg-white/3 transition-all"
            onClick={onRefresh}
            disabled={loading}
            title="Refresh models"
            aria-label="Refresh models"
          >
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
          </button>
        </div>
      </div>

      {/* Free Filter Toggle */}
      <div className="flex items-center justify-between border-b border-white/5 px-3 py-2 bg-white/[0.02]">
        <label className="flex items-center gap-2 cursor-pointer select-none">
          <button
            role="switch"
            aria-checked={showOnlyFree}
            onClick={() => onToggleFree(!showOnlyFree)}
            className={cn(
              "relative inline-flex h-4 w-7 items-center rounded-full transition-colors",
              showOnlyFree ? "bg-green-500" : "bg-white/10"
            )}
          >
            <span
              className={cn(
                "inline-block h-3 w-3 transform rounded-full bg-white transition-transform",
                showOnlyFree ? "translate-x-3.5" : "translate-x-0.5"
              )}
            />
          </button>
          <span className="text-xs font-medium text-secondary flex items-center gap-1">
            <Filter className="h-3 w-3" />
            Show Only Free Models
          </span>
        </label>
        <span className="text-[10px] text-muted">
          {showOnlyFree ? `${filteredTotal}/${totalModels}` : `${totalModels} models`}
        </span>
      </div>

      {/* Error */}
      {error && (
        <div className="border-b border-white/10 px-3 py-2 text-xs text-destructive bg-destructive/5">
          {error}
        </div>
      )}

      {/* Model List */}
      <div className="flex-1 overflow-y-auto p-1.5">
        {/* Default Option */}
        <button
          role="option"
          aria-selected={!currentModel}
          className={cn(
            "flex w-full items-center justify-between gap-2 rounded-lg px-2 py-2 text-left text-sm transition-colors",
            "hover:bg-white/5",
            !currentModel && "bg-white/5 text-primary"
          )}
          onClick={() => onSelect(null, "")}
        >
          <div className="flex items-center gap-2">
            <Sparkles className="h-3.5 w-3.5 icon-muted" />
            <span>Default model</span>
          </div>
          <span className="text-xs text-secondary">Server default</span>
        </button>

        {loading && groups.length === 0 && (
          <div className="px-2 py-4 text-center text-xs text-secondary">Loading models…</div>
        )}

        {!loading && filteredGroups.length === 0 && !error && (
          <div className="px-2 py-4 text-center text-xs text-secondary">
            {showOnlyFree ? "No free models found. Disable filter to see all." : "No providers configured. Add one in Settings."}
          </div>
        )}

        {filteredGroups.map((g) => {
          const freeCount = g.models.filter((m) => isFreeModel(m, g.provider)).length;
          return (
            <div key={g.provider_id} className="mt-2">
              <div className="flex items-center justify-between px-2 pb-1">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-secondary flex items-center gap-1.5">
                  {g.provider_label}
                  {freeCount > 0 && (
                    <span className="rounded-full bg-green-500/15 text-green-500 px-1.5 py-0.5 text-[8px] font-bold tracking-widest">FREE</span>
                  )}
                </span>
                <span className="text-[10px] text-muted">
                  {g.models.length} model{g.models.length !== 1 ? "s" : ""}
                </span>
              </div>
              {g.models.length === 0 ? (
                <p className="px-2 pb-1 text-xs text-muted">
                  No models available (check API key)
                </p>
              ) : (
                g.models.map((m) => {
                  const badge = badgeFor(m.name);
                  const isSelected = currentModel === m.name;
                  const free = isFreeModel(m, g.provider);
                  return (
                    <button
                      key={m.name}
                      role="option"
                      aria-selected={isSelected}
                      className={cn(
                        "flex w-full items-center justify-between gap-2 rounded-lg px-2 py-2 text-left text-sm transition-all duration-150",
                        "hover:bg-white/5",
                        isSelected && "bg-primary/10 text-primary"
                      )}
                      onClick={() => onSelect(g, m.name)}
                    >
                      <span className="truncate font-medium flex-1 min-w-0">{shortName(m.name)}</span>
                      <span className="flex items-center gap-1 shrink-0">
                        {free && (
                          <span className="shrink-0 rounded-full bg-green-500/15 text-green-500 border border-green-500/20 px-1.5 py-0.5 text-[9px] font-bold tracking-wide flex items-center gap-0.5">
                            🆓 FREE
                          </span>
                        )}
                        {badge && !free && (
                          <span
                            className={cn(
                              "shrink-0 rounded-full px-2 py-0.5 text-[9px] font-medium",
                              badge === "Fast"
                                ? "bg-green-500/10 text-green-500"
                                : "bg-primary/10 text-primary"
                            )}
                          >
                            {badge}
                          </span>
                        )}
                        {badge && free && (
                          <span
                            className={cn(
                              "hidden sm:inline-flex shrink-0 rounded-full px-1.5 py-0.5 text-[8px] font-medium",
                              badge === "Fast"
                                ? "bg-white/5 text-muted"
                                : "bg-white/5 text-muted"
                            )}
                          >
                            {badge}
                          </span>
                        )}
                      </span>
                    </button>
                  );
                })
              )}
            </div>
          );
        })}
      </div>
    </div>,
    document.body
  );
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
  const [showOnlyFree, setShowOnlyFree] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dropdownRectRef = useRef<DOMRect | null>(null);

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
      if (triggerRef.current && !triggerRef.current.contains(e.target as Node)) {
        // Check if click is inside the portal dropdown
        const dropdown = document.querySelector('[role="listbox"]');
        if (dropdown && !dropdown.contains(e.target as Node)) {
          setOpen(false);
        }
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

  // Calculate dropdown position when open
  useEffect(() => {
    if (open && triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect();
      dropdownRectRef.current = rect;
    }
  }, [open]);

  const displayName = currentModel ? shortName(currentModel) : "Default model";

  const isCurrentFree = useMemo(() => {
    if (!currentModel) return false;
    if (currentModel.toLowerCase().includes(":free")) return true;
    // Check if current model exists in groups and is free
    for (const g of groups) {
      const m = g.models.find((x) => x.name === currentModel);
      if (m) return isFreeModel(m, g.provider);
    }
    // Ollama local models are free
    if (!currentModel.includes("/")) return true; // simple local names like llama3.2
    return false;
  }, [currentModel, groups]);

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        className={cn(
          "flex items-center gap-2 rounded-xl bg-white/5 px-3 py-1.5 text-xs font-medium border border-white/10 transition-all",
          "hover:bg-white/5 hover:border-primary/20",
          open && "border-primary/30 bg-white/5"
        )}
        onClick={() => {
          setOpen(!open);
          if (!open) fetchModels();
        }}
        disabled={saving}
        title={currentModel ? `Model: ${currentModel}` : "Click to select a model"}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <Zap className="h-3.5 w-3.5 text-primary" />
        <span className="max-w-[140px] truncate text-secondary flex items-center gap-1.5">
          {saving ? "Saving…" : displayName}
          {isCurrentFree && !saving && (
            <span className="rounded-full bg-green-500/15 text-green-500 border border-green-500/20 px-1 py-0.5 text-[8px] font-bold leading-none">FREE</span>
          )}
        </span>
        <ChevronDown
          className={cn("h-3.5 w-3.5 transition-transform duration-150 icon-secondary", open && "rotate-180")}
        />
      </button>

      {open && dropdownRectRef.current && (
        <DropdownContent
          groups={groups}
          currentModel={currentModel}
          loading={loading}
          error={error}
          open={open}
          onClose={() => setOpen(false)}
          onRefresh={fetchModels}
          onSelect={handleSelect}
          saving={saving}
          triggerRect={dropdownRectRef.current}
          showOnlyFree={showOnlyFree}
          onToggleFree={setShowOnlyFree}
        />
      )}
    </div>
  );
}

export default ModelSelector;
