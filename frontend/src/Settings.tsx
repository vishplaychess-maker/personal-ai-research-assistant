import { useCallback, useEffect, useState } from "react";
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
import type { ModelInfo, UserSettings } from "./types";

interface SettingsProps {
  onClose: () => void;
}

const PROVIDERS = [
  { value: "openrouter", label: "OpenRouter" },
  { value: "ollama", label: "Ollama" },
  { value: "nvidia", label: "NVIDIA" },
  { value: "huggingface", label: "Hugging Face" },
  { value: "google", label: "Google AI" },
  { value: "modelslab", label: "ModelsLab" },
];

const selectClass =
  "flex h-9 w-full rounded-md border bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

export function Settings({ onClose }: SettingsProps) {
  const [provider, setProvider] = useState("openrouter");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Provider-specific model list (fetched dynamically per provider)
  const [modelOptions, setModelOptions] = useState<ModelInfo[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);

  const fetchModelsForProvider = useCallback(async (prov: string) => {
    setModelsLoading(true);
    setModelsError(null);
    try {
      const data = await API.listChatModels(prov);
      if (data.error) {
        setModelsError(data.error);
        setModelOptions([]);
      } else {
        // Exclude embedding-only models from the chat model dropdown.
        setModelOptions((data.models || []).filter((m) => !/embed/i.test(m.name)));
      }
    } catch (err) {
      setModelsError(err instanceof Error ? err.message : "Failed to load models");
      setModelOptions([]);
    } finally {
      setModelsLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    API.getUserSettings()
      .then((s: UserSettings) => {
        if (cancelled) return;
        setProvider(s.llm_provider || "openrouter");
        setApiKey(s.api_key || "");
        setModel(s.model || "");
      })
      .catch(() => {
        if (!cancelled) setError("Failed to load settings");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Load models for the selected provider once settings are loaded, and
  // whenever the user changes the provider dropdown.
  useEffect(() => {
    if (loading) return;
    fetchModelsForProvider(provider);
  }, [provider, loading, fetchModelsForProvider]);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      await API.updateUserSettings({
        llm_provider: provider,
        api_key: apiKey,
        model,
      });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save settings");
      setSaving(false);
    }
  };

  const savedModelMissing =
    !!model && !modelOptions.some((m) => m.name === model);

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Settings</DialogTitle>
          <DialogDescription>
            Configure your default LLM provider, API key, and model. Changes
            apply immediately without restarting.
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <p className="py-4 text-sm text-muted-foreground">Loading settings…</p>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleSave();
            }}
            className="grid gap-4 py-2"
          >
            <div className="grid gap-2">
              <Label htmlFor="llm-provider">LLM Provider</Label>
              <select
                id="llm-provider"
                value={provider}
                onChange={(e) => {
                  setProvider(e.target.value);
                  // Model from the previous provider is not relevant; clear it
                  // so the user picks one from the new provider's list.
                  setModel("");
                }}
                className={selectClass}
              >
                {PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="api-key">API Key</Label>
              <Input
                id="api-key"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="Leave blank to use server default"
                autoComplete="off"
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor="model-name">Model</Label>
              <select
                id="model-name"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className={selectClass}
                disabled={modelsLoading}
              >
                {!model && <option value="">Default model</option>}
                {savedModelMissing && (
                  <option value={model}>{model} (saved)</option>
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
              {!modelsLoading && modelsError && (
                <p className="text-xs text-destructive">{modelsError}</p>
              )}
              {!modelsLoading && !modelsError && modelOptions.length === 0 && (
                <p className="text-xs text-muted-foreground">
                  No free models found or invalid API key
                </p>
              )}
            </div>

            {error && <p className="text-sm text-destructive">{error}</p>}
          </form>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={saving || loading}>
            {saving ? "Saving…" : "Save changes"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default Settings;
