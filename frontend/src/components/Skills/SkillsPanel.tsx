/**
 * SkillsPanel — capability grid for Settings > Skills.
 *
 * Phase 2: preferences are stored per user via /api/capabilities (the static
 * catalog lives on the server). Execution is still not wired — the panel says
 * so — this only persists the enabled flag and, for executable capabilities,
 * the permission level (ask / auto / full).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  FileText,
  Globe,
  Monitor,
  RefreshCw,
  Search,
  Shield,
  Sparkles,
  Terminal,
  Unlock,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { API } from "../../api";
import type { Capability } from "../../types";
import { Button } from "../ui/button";
import { Card, CardContent } from "../ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";
import { Input } from "../ui/input";
import { Switch } from "../ui/switch";
import { cn } from "../../lib/utils";

const ICON_MAP: Record<string, LucideIcon> = {
  globe: Globe,
  terminal: Terminal,
  "file-text": FileText,
  monitor: Monitor,
};

type PermissionValue = "ask_every_time" | "auto_approve" | "full_access";

const PERMISSION_LEVELS: {
  value: PermissionValue;
  label: string;
  short: string;
  description: string;
  icon: LucideIcon;
}[] = [
  {
    value: "ask_every_time",
    label: "Ask every time",
    short: "Ask",
    description: "Always confirm before executing any command or file access.",
    icon: Shield,
  },
  {
    value: "auto_approve",
    label: "Auto approve",
    short: "Auto",
    description:
      "Automatically allow safe operations; ask only for risky/destructive actions.",
    icon: Zap,
  },
  {
    value: "full_access",
    label: "Full access",
    short: "Full",
    description: "No confirmations. Execute all sandboxed operations immediately.",
    icon: Unlock,
  },
];

/** Capabilities whose execution is governed by a permission level. */
const PERMISSION_CAPABILITIES = new Set(["code_executor", "desktop_automation"]);

function permissionLevelOf(capability: Capability): PermissionValue {
  const raw = capability.config?.permission_level;
  return raw === "ask_every_time" || raw === "full_access" ? raw : "auto_approve";
}

function permissionOption(value: PermissionValue) {
  return PERMISSION_LEVELS.find((option) => option.value === value) ?? PERMISSION_LEVELS[1];
}

export function SkillsPanel() {
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  // Selecting Full access is a two-step action: acknowledge, then confirm.
  const [pendingFullAccess, setPendingFullAccess] = useState<{
    id: string;
    acknowledged: boolean;
  } | null>(null);

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
    if (pendingFullAccess?.id === capability.id) setPendingFullAccess(null);
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

  const savePermission = async (capability: Capability, level: PermissionValue) => {
    setPendingId(capability.id);
    setError(null);
    try {
      const updated = await API.updateCapability(capability.id, {
        enabled: capability.enabled,
        config: { ...capability.config, permission_level: level },
      });
      setCapabilities((current) =>
        current.map((c) => (c.id === updated.id ? updated : c))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update permission");
    } finally {
      setPendingId(null);
    }
  };

  const handlePermissionSelect = (capability: Capability, level: PermissionValue) => {
    if (level === permissionLevelOf(capability)) return;
    if (level === "full_access") {
      setPendingFullAccess({ id: capability.id, acknowledged: false });
      return;
    }
    setPendingFullAccess((current) => (current?.id === capability.id ? null : current));
    void savePermission(capability, level);
  };

  const confirmFullAccess = (capability: Capability) => {
    if (pendingFullAccess?.id !== capability.id || !pendingFullAccess.acknowledged) return;
    setPendingFullAccess(null);
    void savePermission(capability, "full_access");
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
            Turn individual capabilities on or off. Executable capabilities also
            take a permission level; wiring is still in progress, and changes are
            saved to your account.
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
            const supportsPermissions = PERMISSION_CAPABILITIES.has(capability.id);
            const level = permissionLevelOf(capability);
            const levelOption = permissionOption(level);
            const LevelIcon = levelOption.icon;
            const badgeClass =
              level === "full_access"
                ? "bg-destructive/10 text-destructive"
                : level === "auto_approve"
                  ? "bg-primary/10 text-primary"
                  : "bg-muted text-muted-foreground";
            return (
              <Card
                key={capability.id}
                className={cn(
                  "transition-colors",
                  capability.enabled && "border-primary/40 bg-primary/5"
                )}
              >
                <CardContent className="p-4">
                  <div className="flex items-start gap-3">
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
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate text-sm font-medium">
                          {capability.name}
                        </span>
                        {capability.id === "desktop_automation" && (
                          <span
                            className={cn(
                              "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium",
                              badgeClass
                            )}
                          >
                            {levelOption.short}
                          </span>
                        )}
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
                  </div>

                  {capability.enabled && supportsPermissions && (
                    <div className="mt-3 border-t border-white/5 pt-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-[11px] text-muted-foreground">
                          Permission level
                        </span>
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <button
                              type="button"
                              aria-label={`Permission level for ${capability.name}`}
                              disabled={pendingId !== null}
                              className="inline-flex items-center gap-1.5 rounded-md border border-white/10 px-2 py-1 text-[11px] text-foreground transition-colors hover:bg-white/5 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                            >
                              <LevelIcon className="h-3.5 w-3.5 text-muted-foreground" />
                              {levelOption.label}
                              <ChevronDown className="h-3 w-3 text-muted-foreground" />
                            </button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end" className="w-64">
                            {PERMISSION_LEVELS.map((option) => {
                              const OptionIcon = option.icon;
                              return (
                                <DropdownMenuItem
                                  key={option.value}
                                  onSelect={() =>
                                    handlePermissionSelect(capability, option.value)
                                  }
                                  className="items-start gap-2"
                                >
                                  <OptionIcon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                                  <span className="min-w-0">
                                    <span className="block text-sm">{option.label}</span>
                                    <span className="block text-[11px] text-muted-foreground">
                                      {option.description}
                                    </span>
                                  </span>
                                </DropdownMenuItem>
                              );
                            })}
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>

                      {pendingFullAccess?.id === capability.id ? (
                        <div className="mt-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3">
                          <div className="flex items-start gap-2">
                            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                            <div className="min-w-0">
                              <p className="text-xs font-medium text-destructive">
                                Full access allows the AI to modify files in the
                                workspace without confirmation. Use with caution.
                              </p>
                              <p className="mt-1 text-[11px] text-muted-foreground">
                                Workspace limits and the blocked-command list still
                                apply. Only enable this if you trust every task you run.
                              </p>
                            </div>
                          </div>
                          <label className="mt-2 flex items-center gap-2 text-xs text-foreground">
                            <input
                              type="checkbox"
                              checked={pendingFullAccess.acknowledged}
                              onChange={(e) =>
                                setPendingFullAccess({
                                  id: capability.id,
                                  acknowledged: e.target.checked,
                                })
                              }
                              className="h-3.5 w-3.5 rounded border"
                            />
                            I understand the risks
                          </label>
                          <div className="mt-2 flex justify-end gap-2">
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => setPendingFullAccess(null)}
                            >
                              Cancel
                            </Button>
                            <Button
                              size="sm"
                              variant="destructive"
                              disabled={!pendingFullAccess.acknowledged}
                              onClick={() => confirmFullAccess(capability)}
                            >
                              Enable full access
                            </Button>
                          </div>
                        </div>
                      ) : level === "full_access" ? (
                        <div className="mt-2 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-2.5">
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
                          <p className="text-[11px] text-muted-foreground">
                            <span className="font-medium text-destructive">
                              Full access is on.
                            </span>{" "}
                            The AI can modify files in the workspace without
                            confirmation. Use with caution; workspace limits still
                            apply.
                          </p>
                        </div>
                      ) : null}
                    </div>
                  )}
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
