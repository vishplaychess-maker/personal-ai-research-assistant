import { useCallback, useEffect, useState } from "react";
import {
  Bot,
  Brain,
  ChevronRight,
  Globe,
  Microscope,
  Puzzle,
  RefreshCw,
  Sparkles,
  Terminal,
  type LucideIcon,
} from "lucide-react";
import { API } from "../../api";
import { cn } from "../../lib/utils";
import type { Capability } from "../../types";
import { Switch } from "../ui/switch";
import type { SettingsTab } from "./SettingsSidebar";

interface PluginDef {
  id: string;
  name: string;
  description: string;
  icon: LucideIcon;
  accent: string;
  capabilityId?: string;
  memorySetting?: boolean;
  navigateTo?: SettingsTab;
}

const PLUGINS: PluginDef[] = [
  {
    id: "mem0-memory",
    name: "Mem0 Semantic Memory",
    description: "Long-term user context and semantic ranking.",
    icon: Brain,
    accent: "bg-violet-500/10 text-violet-500 ring-violet-500/20 dark:text-violet-300",
    memorySetting: true,
  },
  {
    id: "openbot-agent",
    name: "OpenBot Autonomous Agent",
    description: "Delegates complex, multi-step research to an isolated AI agent.",
    icon: Bot,
    accent: "bg-fuchsia-500/10 text-fuchsia-500 ring-fuchsia-500/20 dark:text-fuchsia-300",
    capabilityId: "openbot_research",
  },
  {
    id: "custom-skills",
    name: "Custom Skill Marketplace",
    description: "Dynamic plugin loading for user-defined Python skills.",
    icon: Sparkles,
    accent: "bg-pink-500/10 text-pink-500 ring-pink-500/20 dark:text-pink-300",
    navigateTo: "skills",
  },
  {
    id: "crawl4ai-scraper",
    name: "Crawl4AI Deep-Dive Scraper",
    description: "Fetch and convert web pages into clean Markdown.",
    icon: Globe,
    accent: "bg-teal-500/10 text-teal-600 ring-teal-500/20 dark:text-teal-300",
    capabilityId: "web_scraper",
  },
  {
    id: "rea-tools",
    name: "REA Reverse Engineering",
    description: "229+ advanced tools for binary and package analysis.",
    icon: Microscope,
    accent: "bg-rose-500/10 text-rose-600 ring-rose-500/20 dark:text-rose-300",
    capabilityId: "rea_tools",
  },
  {
    id: "open-interpreter",
    name: "Open Interpreter",
    description: "LLM-driven code execution with strict permission controls.",
    icon: Terminal,
    accent: "bg-amber-500/10 text-amber-600 ring-amber-500/20 dark:text-amber-300",
    capabilityId: "open_interpreter",
  },
];

export function PluginsPanel({
  onNavigate,
}: {
  onNavigate: (tab: SettingsTab) => void;
}) {
  const [capabilities, setCapabilities] = useState<Record<string, Capability>>({});
  const [memoryEnabled, setMemoryEnabled] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    const [capsResult, memoryResult] = await Promise.allSettled([
      API.getCapabilities(),
      API.getMemorySetting(),
    ]);
    if (capsResult.status === "fulfilled") {
      setCapabilities(
        Object.fromEntries(capsResult.value.map((item) => [item.id, item]))
      );
    } else {
      setError(
        capsResult.reason instanceof Error
          ? capsResult.reason.message
          : "Failed to load plugins"
      );
    }
    setMemoryEnabled(
      memoryResult.status === "fulfilled" ? memoryResult.value.enabled : null
    );
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const capabilityOf = (plugin: PluginDef) =>
    plugin.capabilityId ? capabilities[plugin.capabilityId] : undefined;

  const isEnabled = (plugin: PluginDef) => {
    if (plugin.memorySetting) return memoryEnabled === true;
    return capabilityOf(plugin)?.enabled === true;
  };

  const toggleable = PLUGINS.filter(
    (plugin) => plugin.memorySetting || plugin.capabilityId
  );
  const enabledCount = toggleable.filter(isEnabled).length;

  const toggle = async (plugin: PluginDef) => {
    if (pendingId) return;
    setPendingId(plugin.id);
    setError(null);

    if (plugin.memorySetting) {
      const previous = memoryEnabled;
      const next = !previous;
      setMemoryEnabled(next);
      try {
        const setting = await API.setMemorySetting(next);
        setMemoryEnabled(setting.enabled);
      } catch (err) {
        setMemoryEnabled(previous);
        setError(err instanceof Error ? err.message : "Failed to update Mem0");
      } finally {
        setPendingId(null);
      }
      return;
    }

    const capability = capabilityOf(plugin);
    if (!capability) {
      setPendingId(null);
      return;
    }
    const previous = capabilities;
    setCapabilities((current) => ({
      ...current,
      [capability.id]: { ...capability, enabled: !capability.enabled },
    }));
    try {
      const updated = await API.updateCapability(capability.id, {
        enabled: !capability.enabled,
      });
      setCapabilities((current) => ({ ...current, [updated.id]: updated }));
    } catch (err) {
      setCapabilities(previous);
      setError(
        err instanceof Error ? err.message : "Failed to update the plugin"
      );
    } finally {
      setPendingId(null);
    }
  };

  return (
    <section className="space-y-6" data-testid="plugins-panel">
      <div className="relative overflow-hidden rounded-2xl border border-border/60 bg-gradient-to-br from-primary/[0.08] via-card/70 to-fuchsia-500/[0.06] p-5 sm:p-7">
        <div
          aria-hidden
          className="pointer-events-none absolute -right-20 -top-24 h-56 w-56 rounded-full bg-fuchsia-500/10 blur-3xl"
        />
        <div className="relative flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
              <Puzzle className="h-3.5 w-3.5 text-primary" />
              Extension center
            </div>
            <h2 className="mt-2 text-3xl font-semibold tracking-tight text-foreground">
              Plugins
            </h2>
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted-foreground">
              Manage the advanced capabilities that extend Thunder AI — memory,
              autonomous agents, scraping, reverse engineering, and code execution.
            </p>
          </div>
          {!loading && (
            <span className="hidden shrink-0 items-center gap-1.5 self-start rounded-full border border-border/60 bg-background/60 px-3 py-1.5 text-[11px] font-medium text-muted-foreground backdrop-blur lg:inline-flex">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              {enabledCount} of {toggleable.length} enabled
            </span>
          )}
        </div>
      </div>

      {error && (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-destructive/30 bg-destructive/5 px-3 py-2">
          <p className="text-xs text-destructive">{error}</p>
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border/60 px-2 py-1 text-[11px] text-muted-foreground hover:bg-secondary hover:text-foreground"
          >
            <RefreshCw className="h-3 w-3" />
            Retry
          </button>
        </div>
      )}

      <div className="overflow-hidden rounded-2xl border border-border/60 bg-card/40">
        {loading ? (
          <div className="divide-y divide-border/60">
            {PLUGINS.map((plugin) => (
              <div key={plugin.id} className="flex items-center gap-4 px-4 py-5 sm:px-5">
                <span className="h-11 w-11 shrink-0 animate-pulse rounded-full bg-muted" />
                <div className="min-w-0 flex-1 space-y-2">
                  <div className="h-3.5 w-40 animate-pulse rounded bg-muted" />
                  <div className="h-2.5 w-64 max-w-full animate-pulse rounded bg-muted" />
                </div>
                <span className="h-6 w-11 animate-pulse rounded-full bg-muted" />
              </div>
            ))}
          </div>
        ) : (
          PLUGINS.map((plugin, index) => {
            const Icon = plugin.icon;
            const capability = capabilityOf(plugin);
            const enabled = isEnabled(plugin);
            const available = plugin.navigateTo || plugin.memorySetting || capability;
            return (
              <div
                key={plugin.id}
                className={cn(
                  "group flex items-start gap-3.5 px-4 py-4 transition-colors hover:bg-secondary/30 sm:gap-4 sm:px-5 sm:py-5",
                  index > 0 && "border-t border-border/60"
                )}
              >
                <span
                  className={cn(
                    "flex h-11 w-11 shrink-0 items-center justify-center rounded-full ring-1 transition-transform duration-200 group-hover:scale-105",
                    plugin.accent
                  )}
                >
                  <Icon className="h-[18px] w-[18px]" />
                </span>

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-[15px] font-semibold tracking-tight text-foreground">
                      {plugin.name}
                    </h3>
                    {pendingId === plugin.id && (
                      <span className="text-[10px] text-muted-foreground">Saving…</span>
                    )}
                    {!available && (
                      <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                        Unavailable
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    {plugin.description}
                  </p>
                  {plugin.capabilityId === "rea_tools" && enabled && (
                    <p className="mt-1 text-[11px] text-muted-foreground">
                      Requires <code className="rounded bg-muted px-1 py-0.5">REA_MCP_ENABLED=true</code> on the server.
                    </p>
                  )}
                </div>

                <div className="flex shrink-0 items-center gap-2 pt-1.5">
                  {plugin.navigateTo ? (
                    <button
                      type="button"
                      onClick={() => onNavigate(plugin.navigateTo!)}
                      className="inline-flex items-center gap-1 rounded-lg border border-border/70 px-2.5 py-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
                    >
                      Manage
                      <ChevronRight className="h-3.5 w-3.5" />
                    </button>
                  ) : plugin.memorySetting ? (
                    <Switch
                      checked={memoryEnabled === true}
                      onCheckedChange={() => void toggle(plugin)}
                      disabled={memoryEnabled === null || pendingId !== null}
                      aria-label={`Enable ${plugin.name}`}
                    />
                  ) : capability ? (
                    <Switch
                      checked={capability.enabled}
                      onCheckedChange={() => void toggle(plugin)}
                      disabled={pendingId !== null}
                      aria-label={`Enable ${plugin.name}`}
                    />
                  ) : (
                    <span className="rounded-full bg-muted px-2 py-1 text-[10px] font-medium text-muted-foreground">
                      Unavailable
                    </span>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        Plugin toggles update the same capability preferences used by the agent.
        Custom skills and their individual playbooks are managed in Settings → Skills.
      </p>
    </section>
  );
}

export default PluginsPanel;
