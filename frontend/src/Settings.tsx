import { useCallback, useEffect, useState } from "react";
import { Plus, Pencil, Trash2, Clock, Calendar } from "lucide-react";
import { API } from "./api";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./components/ui/dialog";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "./components/ui/tabs";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import { cn } from "./lib/utils";
import type { ModelInfo, ProviderConfig, ScheduledTask } from "./types";
import { ScheduledTasks } from "./ScheduledTasks";
import { MCPTab } from "./MCPTab";
import { AgentDirectives } from "./AgentDirectives";

const PROVIDER_OPTIONS = [
  { value: "openrouter", label: "OpenRouter" },
  { value: "nvidia", label: "NVIDIA" },
  { value: "huggingface", label: "Hugging Face" },
  { value: "google", label: "Google AI" },
  { value: "modelslab", label: "ModelsLab" },
  { value: "groq", label: "Groq" },
  { value: "together", label: "Together AI" },
  { value: "mistral", label: "Mistral" },
  { value: "cohere", label: "Cohere" },
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
  const [activeTab, setActiveTab] = useState<"providers" | "scheduler" | "mcp" | "directives">("providers");
  
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
    setFormProvider("openrouter");
    setFormApiKey("");
    setFormModel("");
    setFormActive(false);
    setDialogOpen(true);
    fetchModelsForProvider("openrouter");
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
      <DialogContent className="sm:max-w-2xl max-h-[85vh] overflow-y-auto pr-1">
        <DialogHeader>
          <DialogTitle>Settings</DialogTitle>
        </DialogHeader>

        <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as "providers" | "scheduler" | "mcp" | "directives")} className="w-full">
          <TabsList className="grid w-full grid-cols-4">
            <TabsTrigger value="providers">Providers</TabsTrigger>
            <TabsTrigger value="scheduler">Scheduled Tasks</TabsTrigger>
            <TabsTrigger value="mcp">MCP Servers</TabsTrigger>
            <TabsTrigger value="directives">Lessons Learned</TabsTrigger>
          </TabsList>

          {/* ── Providers Tab ── */}
          <TabsContent value="providers">
            <DialogHeader>
              <DialogTitle>Providers</DialogTitle>
              <DialogDescription>
                Manage your LLM API keys and models. Only one provider is active at
                a time — the active one is used for chat.
              </DialogDescription>
            </DialogHeader>

            <div className="flex justify-end mb-4">
              <Button size="sm" onClick={openAdd}>
                <Plus className="h-4 w-4" />
                Add Provider
              </Button>
            </div>

            {error && <p className="text-sm text-destructive mb-4">{error}</p>}

            {loading ? (
              <p className="py-4 text-center text-sm text-muted-foreground">Loading…</p>
            ) : providers.length === 0 ? (
              <p className="py-4 text-center text-sm text-muted-foreground">
                No providers yet. Add your first provider to start chatting.
              </p>
            ) : (
              <div className="grid max-h-[40vh] gap-2 overflow-y-auto pr-1">
                {providers.map((p) => (
                  <div
                    key={p.id}
                    className={cn(
                      "flex items-center gap-3 rounded-xl border p-3",
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
          </TabsContent>

          {/* ── Scheduler Tab ── */}
          <TabsContent value="scheduler">
            <ScheduledTasks
              sessions={sessions}
              activeSessionId={activeSessionId}
              onClose={() => {}}
            />
          </TabsContent>

          {/* ── MCP Servers Tab ── */}
          <TabsContent value="mcp">
            <MCPTab />
          </TabsContent>

          {/* ── Agent Directives (Lessons Learned) Tab ── */}
          <TabsContent value="directives">
            <DialogHeader>
              <DialogTitle>Lessons Learned</DialogTitle>
              <DialogDescription>
                Manage the standing rules the agent has saved across your chats.
              </DialogDescription>
            </DialogHeader>
            <AgentDirectives />
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

export default Settings;