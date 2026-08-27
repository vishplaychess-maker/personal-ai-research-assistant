import { useEffect, useState, type CSSProperties } from "react";
import { API } from "./api";
import type { UserSettings } from "./types";

interface SettingsProps {
  onClose: () => void;
}

const PROVIDERS = [
  { value: "openrouter", label: "OpenRouter" },
  { value: "ollama", label: "Ollama" },
  { value: "nvidia", label: "NVIDIA" },
];

const overlayStyle: CSSProperties = {
  position: "fixed",
  top: 0,
  left: 0,
  right: 0,
  bottom: 0,
  background: "rgba(0, 0, 0, 0.55)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 1000,
};

const panelStyle: CSSProperties = {
  background: "#1e1e2e",
  color: "#e0e0e0",
  padding: "1.5rem",
  borderRadius: "12px",
  width: "min(420px, 92vw)",
  boxShadow: "0 8px 40px rgba(0, 0, 0, 0.4)",
};

const inputStyle: CSSProperties = {
  background: "#16161f",
  border: "1px solid #3a3a4a",
  color: "#e0e0e0",
  padding: "0.5rem 0.7rem",
  borderRadius: "6px",
};

const buttonStyle: CSSProperties = {
  background: "#3a3a4a",
  border: "none",
  color: "#ffffff",
  padding: "0.5rem 1rem",
  borderRadius: "6px",
  cursor: "pointer",
};

export function Settings({ onClose }: SettingsProps) {
  const [provider, setProvider] = useState("openrouter");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

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

  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={panelStyle} onClick={(e) => e.stopPropagation()}>
        <h2 style={{ margin: "0 0 1rem", fontSize: "1.25rem" }}>Settings</h2>
        {loading ? (
          <p>Loading settings...</p>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleSave();
            }}
            style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }}
          >
            <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
              <span>LLM Provider</span>
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                style={inputStyle}
              >
                {PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>

            <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
              <span>API Key</span>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="Leave blank to use server default"
                autoComplete="off"
                style={inputStyle}
              />
            </label>

            <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
              <span>Model Name</span>
              <input
                type="text"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="e.g. meta-llama/llama-3.2-3b-instruct"
                style={inputStyle}
              />
            </label>

            {error && <p style={{ color: "#ff6b6b", margin: 0 }}>{error}</p>}

            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                gap: "0.6rem",
                marginTop: "0.4rem",
              }}
            >
              <button type="button" onClick={onClose} style={buttonStyle}>
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving}
                style={{ ...buttonStyle, background: "#4f6ef7" }}
              >
                {saving ? "Saving..." : "Save"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

export default Settings;
