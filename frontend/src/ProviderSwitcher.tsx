/**
 * ProviderSwitcher — Quick active-provider switch in the chat header.
 * Premium dark dropdown (portal, matching ModelSelector). Shows the active
 * provider with a colored branded mark, lists all configured providers, and
 * lets the user flip active instantly via API.updateProvider(id, {is_active}).
 * Emits onProviderSwitch(providerId) so the caller can refresh the model list.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, Loader2, Boxes } from "lucide-react";
import { API } from "./api";
import { cn } from "./lib/utils";
import type { ProviderConfig } from "./types";

interface ProviderSwitcherProps {
  refresh?: number;
  onProviderSwitch: (providerId: number) => void;
}

// Branded marks per provider (colored monogram + ring). Kept local so this
// component stays dependency-free; matches the premium dark aesthetic.
const BRANDS: Record<string, { mark: string; ring: string }> = {
  openrouter: { mark: "OR", ring: "from-primary/30 to-primary/10 text-primary border-primary/30" },
  google: { mark: "G", ring: "from-blue-500/30 to-emerald-500/10 text-blue-300 border-blue-500/30" },
  nvidia: { mark: "NV", ring: "from-green-500/30 to-green-500/10 text-green-300 border-green-500/30" },
  huggingface: { mark: "HF", ring: "from-amber-500/30 to-amber-500/10 text-amber-300 border-amber-500/30" },
  modelslab: { mark: "ML", ring: "from-purple-500/30 to-purple-500/10 text-purple-300 border-purple-500/30" },
  ollama: { mark: "O", ring: "from-zinc-400/30 to-zinc-400/10 text-zinc-300 border-zinc-400/30" },
  local: { mark: "L", ring: "from-cyan-500/30 to-cyan-500/10 text-cyan-300 border-cyan-500/30" },
};

const LABELS: Record<string, string> = {
  openrouter: "OpenRouter",
  google: "Google AI",
  nvidia: "NVIDIA",
  huggingface: "Hugging Face",
  modelslab: "ModelsLab",
  ollama: "Ollama",
  local: "Local",
};

function brand(name: string) {
  const key = (name || "").toLowerCase().trim();
  return BRANDS[key] ?? BRANDS.openrouter;
}
function label(name: string) {
  const key = (name || "").toLowerCase().trim();
  return LABELS[key] ?? (name || "Provider");
}

export function ProviderSwitcher({ refresh, onProviderSwitch }: ProviderSwitcherProps) {
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const rectRef = useRef<DOMRect | null>(null);

  const fetchProviders = async () => {
    setLoading(true);
    setError(null);
    try {
      setProviders((await API.listProviders()) || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load providers");
      setProviders([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchProviders();
  }, [refresh]);

  // Close on outside click.
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (triggerRef.current && !triggerRef.current.contains(e.target as Node)) {
        const pop = document.querySelector('[data-provider-popover]');
        if (pop && !pop.contains(e.target as Node)) setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  useEffect(() => {
    if (open && triggerRef.current) rectRef.current = triggerRef.current.getBoundingClientRect();
  }, [open]);

  const active = providers.find((p) => p.is_active) ?? null;
  const activeLabel = active ? label(active.provider_name) : "Provider";

  const handleSwitch = async (p: ProviderConfig) => {
    if (p.is_active) return;
    setSwitching(p.id);
    setError(null);
    try {
      await API.updateProvider(p.id, { is_active: true });
      setProviders((prev) =>
        prev.map((x) => ({ ...x, is_active: x.id === p.id }))
      );
      onProviderSwitch(p.id);
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to switch provider");
    } finally {
      setSwitching(null);
    }
  };

  const b = brand(active?.provider_name ?? "");
  const top = rectRef.current ? rectRef.current.bottom + 8 : 0;
  const left = rectRef.current
    ? Math.max(8, Math.min(window.innerWidth - 256, rectRef.current.right - 256))
    : 0;

  return (
    <>
      <button
        ref={triggerRef}
        onClick={() => {
          setOpen(!open);
          if (!open) fetchProviders();
        }}
        className={cn(
          "flex items-center gap-2 rounded-xl bg-white/5 px-2.5 py-1.5 text-xs font-medium border border-white/10 transition-all",
          "hover:bg-white/5 hover:border-primary/30",
          open && "border-primary/30 bg-white/5"
        )}
        title={active ? `Active provider: ${activeLabel}` : "No provider configured"}
        aria-expanded={open}
        aria-haspopup="menu"
      >
        {active ? (
          <span
            className={cn(
              "flex h-5 w-5 items-center justify-center rounded-md border bg-gradient-to-br text-[9px] font-bold leading-none",
              b.ring
            )}
          >
            {b.mark}
          </span>
        ) : (
          <Boxes className="h-3.5 w-3.5 text-primary" />
        )}
        <span className="max-w-[90px] truncate text-secondary">
          {switching !== null ? "Switching…" : activeLabel}
        </span>
        <ChevronDown
          className={cn("h-3.5 w-3.5 transition-transform duration-150 icon-secondary", open && "rotate-180")}
        />
      </button>

      {open &&
        createPortal(
          <div
            data-provider-popover
            role="menu"
            className="fixed z-[100] flex w-64 flex-col overflow-hidden rounded-xl border border-white/10 bg-background text-foreground shadow-2xl backdrop-blur-xl"
            style={{ top: `${top}px`, left: `${left}px` }}
          >
            <div className="flex items-center justify-between border-b border-white/10 px-3 py-2.5">
              <span className="text-xs font-semibold text-foreground">Switch Provider</span>
              {loading && <Loader2 className="h-3.5 w-3.5 animate-spin text-secondary" />}
            </div>

            {error && (
              <div className="border-b border-white/10 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                {error}
              </div>
            )}

            <div className="max-h-[50vh] overflow-y-auto p-1.5">
              {!loading && providers.length === 0 && (
                <div className="px-2 py-6 text-center text-xs text-secondary">
                  No providers configured.
                  <div className="mt-1 text-[11px] text-muted">Go to Settings to add a provider.</div>
                </div>
              )}

              {providers.map((p) => {
                const pb = brand(p.provider_name);
                const isActive = p.is_active;
                const isBusy = switching === p.id;
                return (
                  <button
                    key={p.id}
                    role="menuitemradio"
                    aria-checked={isActive}
                    disabled={isActive || switching !== null}
                    onClick={() => handleSwitch(p)}
                    className={cn(
                      "flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left text-sm transition-colors",
                      "hover:bg-white/5",
                      isActive && "bg-primary/10 text-primary",
                      !isActive && "text-foreground"
                    )}
                  >
                    <span
                      className={cn(
                        "flex h-6 w-6 items-center justify-center rounded-lg border bg-gradient-to-br text-[10px] font-bold leading-none",
                        pb.ring
                      )}
                    >
                      {pb.mark}
                    </span>
                    <span className="flex-1 truncate font-medium">{label(p.provider_name)}</span>
                    {isBusy && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-secondary" />}
                    {isActive && (
                      <span className="flex shrink-0 items-center gap-1 text-green-500">
                        <span className="h-1.5 w-1.5 rounded-full bg-green-500" />
                        <Check className="h-3.5 w-3.5" />
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>,
          document.body
        )}
    </>
  );
}

export default ProviderSwitcher;
