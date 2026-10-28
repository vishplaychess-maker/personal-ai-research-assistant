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
  Brain,
  ChevronDown,
  Database,
  FileText,
  GitBranch,
  Globe,
  HelpCircle,
  Monitor,
  NotebookText,
  Puzzle,
  RefreshCw,
  Search,
  Shield,
  Sparkles,
  Terminal,
  Unlock,
  Wrench,
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
  github: GitBranch,
  notebook: NotebookText,
  database: Database,
  brain: Brain,
  sparkles: Sparkles,
  puzzle: Puzzle,
  wrench: Wrench,
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
const PERMISSION_CAPABILITIES = new Set([
  "code_executor",
  "desktop_automation",
  "open_interpreter",
]);

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
  const [mcpForm, setMcpForm] = useState<{
    capability: Capability;
    values: Record<string, string>;
    enableOnSave: boolean;
  } | null>(null);
  const [mcpSaving, setMcpSaving] = useState(false);
  // Custom skill marketplace (kind="custom") — per-skill config drafts for
  // the inputs_schema form, plus the "How to add a skill" help toggle.
  const [customDrafts, setCustomDrafts] = useState<
    Record<string, Record<string, string>>
  >({});
  const [showCustomHelp, setShowCustomHelp] = useState(false);

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

  const handleMcpToggle = async (capability: Capability) => {
    setError(null);
    const configured = Boolean(capability.config?.configured);
    if (!capability.enabled && !configured) {
      setMcpForm({ capability, values: {}, enableOnSave: true });
      return;
    }
    setPendingId(capability.id);
    try {
      await API.updateOfficialMcpServer(capability.id, { enabled: !capability.enabled });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update MCP server");
    } finally {
      setPendingId(null);
    }
  };

  const openMcpConfig = (capability: Capability, enableOnSave: boolean) => {
    setError(null);
    setMcpForm({ capability, values: {}, enableOnSave });
  };

  const saveMcpConfig = async () => {
    if (!mcpForm) return;
    setMcpSaving(true);
    setError(null);
    try {
      await API.updateOfficialMcpServer(mcpForm.capability.id, {
        enabled: mcpForm.enableOnSave,
        config: mcpForm.values,
      });
      setMcpForm(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save MCP configuration");
    } finally {
      setMcpSaving(false);
    }
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matches = (c: Capability) =>
      !q || `${c.name} ${c.description}`.toLowerCase().includes(q);
    return {
      builtin: capabilities.filter((c) => c.kind !== "custom" && matches(c)),
      custom: capabilities.filter((c) => c.kind === "custom" && matches(c)),
    };
  }, [capabilities, query]);

  const customSkills = useMemo(
    () => capabilities.filter((c) => c.kind === "custom"),
    [capabilities]
  );

  const enabledCount = capabilities.filter((c) => c.enabled).length;

  /** Draft values for a custom skill's inputs_schema form: the saved config
   *  as defaults, overlaid with whatever the user is typing right now. */
  const customDraftOf = (capability: Capability): Record<string, string> => {
    const saved: Record<string, string> = {};
    for (const [key, value] of Object.entries(capability.config ?? {})) {
      if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
        saved[key] = String(value);
      }
    }
    return { ...saved, ...(customDrafts[capability.id] ?? {}) };
  };

  const saveCustomConfig = async (capability: Capability) => {
    setPendingId(capability.id);
    setError(null);
    try {
      const updated = await API.updateCapability(capability.id, {
        enabled: capability.enabled,
        config: { ...capability.config, ...customDraftOf(capability) },
      });
      setCapabilities((current) =>
        current.map((c) => (c.id === updated.id ? updated : c))
      );
      setCustomDrafts((prev) => {
        const next = { ...prev };
        delete next[capability.id];
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save skill configuration");
    } finally {
      setPendingId(null);
    }
  };

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
      ) : filtered.builtin.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No capabilities match “{query}”.
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {filtered.builtin.map((capability) => {
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
                        {capability.kind === "mcp_server" && (
                          <span
                            className={cn(
                              "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium",
                              capability.enabled
                                ? "bg-emerald-500/10 text-emerald-500"
                                : Boolean(capability.config?.configured)
                                  ? "bg-primary/10 text-primary"
                                  : "bg-muted text-muted-foreground"
                            )}
                          >
                            {capability.enabled
                              ? "Connected"
                              : Boolean(capability.config?.configured)
                                ? "Configured"
                                : "Not configured"}
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
                      onCheckedChange={() =>
                        capability.kind === "mcp_server"
                          ? handleMcpToggle(capability)
                          : handleToggle(capability)
                      }
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

                  {capability.enabled && capability.id === "open_interpreter" && (
                    <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2.5">
                      <p className="text-[11px] text-amber-400">
                        ⚠️ Open Interpreter allows the AI to execute code on your
                        machine. Use with caution.
                      </p>
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        In “Ask” mode proposed code is shown for your approval
                        before anything runs. “Auto” runs safe code without
                        asking; “Full access” runs everything with no
                        confirmation. Requires the optional{" "}
                        <code>open-interpreter</code> package on this machine.
                      </p>
                    </div>
                  )}

                  {capability.kind === "mcp_server" && (
                    <div className="mt-3 border-t border-white/5 pt-3">
                      {mcpForm?.capability.id === capability.id ? (
                        <div className="grid gap-2">
                          <p className="text-[11px] text-muted-foreground">
                            Required credentials
                          </p>
                          {(capability.required_config_keys ?? []).map((key) => (
                            <Input
                              key={key}
                              type="password"
                              value={mcpForm.values[key] ?? ""}
                              onChange={(e) =>
                                setMcpForm((prev) =>
                                  prev && prev.capability.id === capability.id
                                    ? {
                                        ...prev,
                                        values: { ...prev.values, [key]: e.target.value },
                                      }
                                    : prev
                                )
                              }
                              placeholder={key}
                              aria-label={key}
                              autoComplete="off"
                              className="h-8 text-xs"
                            />
                          ))}
                          <label className="flex items-center gap-2 text-xs text-foreground">
                            <input
                              type="checkbox"
                              checked={mcpForm.enableOnSave}
                              onChange={(e) =>
                                setMcpForm((prev) =>
                                  prev && prev.capability.id === capability.id
                                    ? { ...prev, enableOnSave: e.target.checked }
                                    : prev
                                )
                              }
                              className="h-3.5 w-3.5 rounded border"
                            />
                            Enable this server after saving
                          </label>
                          <p className="text-[11px] text-muted-foreground">
                            Credentials are validated against the provider and stored
                            encrypted.
                          </p>
                          <div className="flex justify-end gap-2">
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={mcpSaving}
                              onClick={() => setMcpForm(null)}
                            >
                              Cancel
                            </Button>
                            <Button size="sm" disabled={mcpSaving} onClick={saveMcpConfig}>
                              {mcpSaving ? "Saving…" : "Save & connect"}
                            </Button>
                          </div>
                        </div>
                      ) : (
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="text-[11px] text-muted-foreground">
                            {Boolean(capability.config?.configured)
                              ? "Credentials saved"
                              : "Credentials required to enable"}
                          </span>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={pendingId !== null}
                            onClick={() => openMcpConfig(capability, capability.enabled)}
                          >
                            {Boolean(capability.config?.configured)
                              ? "Reconfigure"
                              : "Configure"}
                          </Button>
                        </div>
                      )}
                    </div>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* ── Custom Skills — user-defined plugins from data/custom_skills ── */}
      <div className="mt-6 border-t border-white/5 pt-5" data-testid="custom-skills-section">
        <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-center gap-2">
            <Puzzle className="h-4 w-4 text-primary" />
            <h3 className="text-sm font-medium">Custom Skills</h3>
            {!loading && !error && customSkills.length > 0 && (
              <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {customSkills.filter((c) => c.enabled).length} of {customSkills.length} enabled
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={() => setShowCustomHelp((v) => !v)}
            aria-expanded={showCustomHelp}
            data-testid="custom-skills-help-toggle"
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-white/10 px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-white/5 hover:text-foreground"
          >
            <HelpCircle className="h-3.5 w-3.5" />
            How to add a skill
          </button>
        </div>

        {showCustomHelp && (
          <div
            className="mb-4 rounded-lg border border-white/10 bg-muted/30 p-3"
            data-testid="custom-skills-help"
          >
            <p className="text-xs font-medium">
              Add your own Python skills — no core code changes needed.
            </p>
            <p className="mt-1 text-[11px] text-muted-foreground">
              Create one folder per skill inside the app data directory, under{" "}
              <code className="rounded bg-white/10 px-1 py-0.5">data/custom_skills/</code>:
            </p>
            <pre className="mt-2 overflow-x-auto rounded-md bg-black/40 p-2.5 text-[11px] leading-relaxed text-foreground/90">
{`data/custom_skills/my_skill/
├── manifest.json
└── handler.py`}
            </pre>
            <p className="mt-2 text-[11px] text-muted-foreground">
              <span className="font-medium text-foreground">manifest.json</span> defines the
              identity and the static config fields the user is asked for:
            </p>
            <pre className="mt-2 overflow-x-auto rounded-md bg-black/40 p-2.5 text-[11px] leading-relaxed text-foreground/90">
{`{
  "id": "my_skill",
  "name": "My Skill",
  "description": "What this skill does.",
  "icon": "sparkles",
  "inputs_schema": {
    "api_key": { "type": "string", "label": "API Key", "required": true }
  }
}`}
            </pre>
            <p className="mt-2 text-[11px] text-muted-foreground">
              <span className="font-medium text-foreground">handler.py</span> must export one
              async function. It receives the caller&apos;s {"{"}user_id, inputs, config{"}"} and
              returns a dict:
            </p>
            <pre className="mt-2 overflow-x-auto rounded-md bg-black/40 p-2.5 text-[11px] leading-relaxed text-foreground/90">
{`async def execute(context: dict) -> dict:
    return {"message": "Done!"}`}
            </pre>
            <p className="mt-2 text-[11px] text-muted-foreground">
              The folder name must equal the manifest <code>id</code>. Restart the app after
              adding or editing a skill folder. A broken manifest or syntax error never
              crashes the app — that skill is skipped and simply never appears here.
            </p>
          </div>
        )}

        {loading ? null : filtered.custom.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No custom skills found. Use “How to add a skill” above to create your
            own in <code>data/custom_skills/</code> — they appear here automatically
            after a restart.
          </p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {filtered.custom.map((capability) => {
              const CustomIcon = ICON_MAP[capability.icon] ?? Puzzle;
              const schemaEntries = Object.entries(capability.inputs_schema ?? {});
              const draft = customDraftOf(capability);
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
                        <CustomIcon className="h-4 w-4" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="truncate text-sm font-medium">
                            {capability.name}
                          </span>
                          <span className="shrink-0 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                            Custom
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
                    </div>

                    {schemaEntries.length > 0 && (
                      <div className="mt-3 border-t border-white/5 pt-3">
                        {capability.enabled ? (
                          <div
                            className="grid gap-2"
                            data-testid={`custom-config-${capability.id}`}
                          >
                            <p className="text-[11px] text-muted-foreground">
                              Configuration (saved to your account)
                            </p>
                            {schemaEntries.map(([key, fieldSpec]) => {
                              const isSecret =
                                fieldSpec?.type === "password" ||
                                /key|token|secret|password/i.test(key);
                              return (
                                <Input
                                  key={key}
                                  type={isSecret ? "password" : "text"}
                                  value={draft[key] ?? ""}
                                  onChange={(e) =>
                                    setCustomDrafts((prev) => ({
                                      ...prev,
                                      [capability.id]: {
                                        ...(prev[capability.id] ?? {}),
                                        [key]: e.target.value,
                                      },
                                    }))
                                  }
                                  placeholder={fieldSpec?.placeholder ?? fieldSpec?.label ?? key}
                                  aria-label={fieldSpec?.label ?? key}
                                  autoComplete="off"
                                  className="h-8 text-xs"
                                />
                              );
                            })}
                            <div className="flex items-center justify-between gap-2">
                              <p className="text-[11px] text-muted-foreground">
                                {schemaEntries.some(([, f]) => f?.required)
                                  ? "Required fields are enforced when the skill runs."
                                  : "Optional — the skill can run without it."}
                              </p>
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={pendingId !== null}
                                onClick={() => void saveCustomConfig(capability)}
                              >
                                Save config
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <p className="text-[11px] text-muted-foreground">
                            Enable this skill to configure its{" "}
                            {schemaEntries.length} setting
                            {schemaEntries.length === 1 ? "" : "s"}.
                          </p>
                        )}
                      </div>
                    )}
                  </CardContent>
                </Card>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}

export default SkillsPanel;
