import { useCallback, useEffect, useState } from "react";
import { Plus, Pencil, Trash2, Globe } from "lucide-react";
import { API } from "./api";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./components/ui/dialog";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import { cn } from "./lib/utils";
import type { ModelInfo, ProviderConfig } from "./types";
import { ScheduledTasks } from "./ScheduledTasks";
import { MCPTab } from "./MCPTab";
import { AgentDirectives } from "./AgentDirectives";
import { SkillsTab } from "./SkillsTab";
import { SkillsPanel } from "./components/Skills/SkillsPanel";
import { SettingsSidebar, type SettingsTab } from "./components/Settings/SettingsSidebar";
import { GrokOAuthCard } from "./GrokOAuth";
import { DesktopAutomation } from "./DesktopAutomation";

const PROVIDER_OPTIONS = [
  { value: "glm", label: "GLM 5.3 Flash (Free)" },
  { value: "openrouter", label: "OpenRouter" },
  { value: "nvidia", label: "NVIDIA" },
  { value: "huggingface", label: "Hugging Face" },
  { value: "google", label: "Google AI" },
  { value: "modelslab", label: "ModelsLab" },
  { value: "groq", label: "Groq" },
  { value: "grok", label: "Grok (xAI)" },
  { value: "together", label: "Together AI" },
  { value: "mistral", label: "Mistral" },
  { value: "cohere", label: "Cohere" },
  { value: "venice", label: "Venice.ai" },
  { value: "ollama", label: "Ollama" },
];

const PROVIDER_LABELS: Record<string, string> = Object.fromEntries(
  PROVIDER_OPTIONS.map((o) => [o.value, o.label])
);

const selectClass =
  "flex h-9 w-full rounded-md border bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

interface SettingsProps {
  onClose: () => void;
  sessions: any[]; // Session[]
  activeSessionId: number | null;
}

export function Settings({ onClose, sessions, activeSessionId }: SettingsProps) {
  const [activeTab, setActiveTab] = useState<SettingsTab>("skills");

  // ── Providers state ──────────────────────────────────────
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Add/Edit dialog state
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ProviderConfig | null>(null);
  const [formProvider, setFormProvider] = useState("openrouter");
  const [formApiKey, setFormApiKey] = useState("");
  const [formModel, setFormModel] = useState("");
  const [formActive, setFormActive] = useState(false);
  const [saving, setSaving] = useState(false);
  const [modelOptions, setModelOptions] = useState<ModelInfo[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);

  const loadProviders = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setProviders(await API.listProviders());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load providers");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadProviders();
  }, [loadProviders]);

  const fetchModelsForProvider = useCallback(async (prov: string) => {
    setModelsLoading(true);
    try {
      const data = await API.listChatModels(prov);
      setModelOptions((data.models || []).filter((m) => !/embed/i.test(m.name)));
    } catch {
      setModelOptions([]);
    } finally {
      setModelsLoading(false);
    }
  }, []);

  const openAdd = () => {
    setEditing(null);
    setFormProvider("glm");
    setFormApiKey("");
    setFormModel("");
    setFormActive(false);
    setDialogOpen(true);
    fetchModelsForProvider("glm");
  };

  const openEdit = (p: ProviderConfig) => {
    setEditing(p);
    setFormProvider(p.provider_name);
    setFormApiKey(p.api_key || "");
    setFormModel(p.default_model || "");
    setFormActive(p.is_active);
    setDialogOpen(true);
    fetchModelsForProvider(p.provider_name);
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const payload = {
        provider_name: formProvider,
        api_key: formApiKey,
        default_model: formModel,
        is_active: formActive,
      };
      if (editing) await API.updateProvider(editing.id, payload);
      else await API.createProvider(payload);
      setDialogOpen(false);
      await loadProviders();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save provider");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (p: ProviderConfig) => {
    const label = PROVIDER_LABELS[p.provider_name] || p.provider_name;
    if (!window.confirm(`Delete ${label}? This removes the saved API key.`)) return;
    try {
      await API.deleteProvider(p.id);
      await loadProviders();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete provider");
    }
  };

  // ── Render ───────────────────────────────────────────────
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="h-[80vh] max-h-[85vh] w-[calc(100vw-2rem)] max-w-5xl gap-0 overflow-hidden p-0 sm:rounded-2xl">
        <div className="flex h-full min-h-0 flex-col md:flex-row">
          <SettingsSidebar activeTab={activeTab} onSelect={setActiveTab} />

          <div
            role="tabpanel"
            id={`settings-panel-${activeTab}`}
            aria-labelledby={`settings-tab-${activeTab}`}
            className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden bg-background"
          >
            {/* ── Skills (default) ── */}
            {activeTab === "skills" && (
              <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-8 sm:py-8">
                <SkillsPanel />
                <div className="mt-8 border-t border-border/60 pt-8">
                  <SkillsTab />
                </div>
              </div>
            )}

            {/* ── Providers ── */}
            {activeTab === "providers" && (
              <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-8 sm:py-8">
                <DialogHeader>
                  <DialogTitle className="text-2xl font-semibold tracking-tight">
                    Providers
                  </DialogTitle>
                  <DialogDescription>
                    Manage your LLM API keys and models. Only one provider is active at
                    a time — the active one is used for chat.
                  </DialogDescription>
                </DialogHeader>

                <div className="mb-4 mt-5 flex justify-end">
                  <Button size="sm" onClick={openAdd}>
                    <Plus className="h-4 w-4" />
                    Add Provider
                  </Button>
                </div>

                {error && <p className="mb-4 text-sm text-destructive">{error}</p>}

                {/* Subscription sign-in costs no API credits, so it sits above the
                    API-key provider list rather than inside it. */}
                <div className="mb-4">
                  <GrokOAuthCard />
                </div>

                {loading ? (
                  <p className="py-4 text-center text-sm text-muted-foreground">Loading…</p>
                ) : providers.length === 0 ? (
                  <p className="py-4 text-center text-sm text-muted-foreground">
                    No providers yet. Add your first provider to start chatting.
                  </p>
                ) : (
                  <div className="grid max-h-[45vh] gap-2 overflow-y-auto pr-1">
                    {providers.map((p) => (
                      <div
                        key={p.id}
                        className={cn(
                          "flex items-center gap-3 rounded-2xl border border-border/60 bg-card/50 p-3 transition-colors hover:bg-secondary/30",
                          p.is_active && "border-primary/40 bg-primary/5"
                        )}
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className="truncate text-sm font-medium">
                              {PROVIDER_LABELS[p.provider_name] || p.provider_name}
                            </span>
                            {p.is_active && (
                              <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                                Active
                              </span>
                            )}
                          </div>
                          <div className="truncate text-xs text-muted-foreground">
                            {p.default_model || "Default model"}
                          </div>
                        </div>
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => openEdit(p)}
                          title="Edit"
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => handleDelete(p)}
                          title="Delete"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    ))}
                  </div>
                )}

                {/* Add / Edit dialog */}
                <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
                  <DialogContent className="sm:max-w-md">
                    <DialogHeader>
                      <DialogTitle>{editing ? "Edit Provider" : "Add Provider"}</DialogTitle>
                      <DialogDescription>
                        Configure the provider, API key, and default model.
                      </DialogDescription>
                    </DialogHeader>
                    <div className="grid gap-4 py-2">
                      <div className="grid gap-2">
                        <Label>Provider</Label>
                        <select
                          value={formProvider}
                          onChange={(e) => {
                            setFormProvider(e.target.value);
                            setFormModel("");
                            fetchModelsForProvider(e.target.value);
                          }}
                          className={selectClass}
                        >
                          {PROVIDER_OPTIONS.map((o) => (
                            <option key={o.value} value={o.value}>
                              {o.label}
                            </option>
                          ))}
                        </select>
                      </div>

                      {formProvider === "venice" && (
                        <div className="flex items-start gap-2 rounded-lg border bg-muted/30 p-2.5 text-xs text-muted-foreground">
                          <Globe className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                          <div className="min-w-0">
                            <p>
                              Base URL:{" "}
                              <span className="font-mono">https://api.venice.ai/api/v1</span>
                            </p>
                            <p className="mt-0.5">
                              Get your API key from{" "}
                              <a
                                href="https://venice.ai/settings/api"
                                target="_blank"
                                rel="noreferrer"
                                className="text-primary hover:underline"
                              >
                                venice.ai/settings/api
                              </a>
                            </p>
                          </div>
                        </div>
                      )}

                      <div className="grid gap-2">
                        <Label>API Key</Label>
                        <Input
                          type="password"
                          value={formApiKey}
                          onChange={(e) => setFormApiKey(e.target.value)}
                          placeholder="Paste your API key (leave blank for Ollama)"
                          autoComplete="off"
                        />
                      </div>

                      <div className="grid gap-2">
                        <Label>Default Model</Label>
                        <select
                          value={formModel}
                          onChange={(e) => setFormModel(e.target.value)}
                          className={selectClass}
                          disabled={modelsLoading}
                        >
                          {!formModel && <option value="">Default model</option>}
                          {formModel &&
                            !modelOptions.some((m) => m.name === formModel) && (
                              <option value={formModel}>{formModel} (saved)</option>
                            )}
                          {modelOptions.map((m) => (
                            <option key={m.name} value={m.name}>
                              {m.name}
                            </option>
                          ))}
                        </select>
                        {modelsLoading && (
                          <p className="text-xs text-muted-foreground">Loading models…</p>
                        )}
                        {!modelsLoading && modelOptions.length === 0 && (
                          <p className="text-xs text-muted-foreground">
                            No free models found or invalid API key
                          </p>
                        )}
                      </div>

                      <label className="flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={formActive}
                          onChange={(e) => setFormActive(e.target.checked)}
                          className="h-4 w-4 rounded border"
                        />
                        Set as active provider
                      </label>
                    </div>
                    <DialogFooter>
                      <Button variant="outline" onClick={() => setDialogOpen(false)}>
                        Cancel
                      </Button>
                      <Button onClick={handleSave} disabled={saving}>
                        {saving ? "Saving…" : "Save"}
                      </Button>
                    </DialogFooter>
                  </DialogContent>
                </Dialog>
              </div>
            )}

            {/* ── Scheduled Tasks ── */}
            {activeTab === "scheduler" && (
              <div className="h-full">
                <ScheduledTasks
                  sessions={sessions}
                  activeSessionId={activeSessionId}
                  onClose={() => {}}
                />
              </div>
            )}

            {/* ── MCP Servers ── */}
            {activeTab === "mcp" && (
              <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-8 sm:py-8">
                <DialogHeader>
                  <DialogTitle className="text-2xl font-semibold tracking-tight">
                    MCP Servers
                  </DialogTitle>
                  <DialogDescription>
                    Register Model Context Protocol servers the agent can call.
                  </DialogDescription>
                </DialogHeader>
                <div className="mt-5">
                  <MCPTab />
                </div>
              </div>
            )}

            {/* ── Lessons Learned ── */}
            {activeTab === "directives" && (
              <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-8 sm:py-8">
                <DialogHeader>
                  <DialogTitle className="text-2xl font-semibold tracking-tight">
                    Lessons Learned
                  </DialogTitle>
                  <DialogDescription>
                    Manage the standing rules the agent has saved across your chats.
                  </DialogDescription>
                </DialogHeader>
                <div className="mt-5">
                  <AgentDirectives />
                </div>
              </div>
            )}

            {/* ── Desktop ── */}
            {activeTab === "desktop" && (
              <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-8 sm:py-8">
                <DialogHeader>
                  <DialogTitle className="text-2xl font-semibold tracking-tight">
                    Desktop Automation
                  </DialogTitle>
                  <DialogDescription>
                    Files and commands inside one workspace folder. Off by default, and
                    nothing outside the workspace can be reached.
                  </DialogDescription>
                </DialogHeader>
                <div className="mt-5">
                  <DesktopAutomation />
                </div>
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default Settings;
