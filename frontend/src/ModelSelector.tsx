/**
 * ModelSelector — Dropdown for selecting the AI model for a session.
 *
 * Fetches available models from GET /api/models and allows the user
 * to choose which model to use for a specific session.
 * Dispatches PATCH /api/sessions/{id}/model to persist the selection.
 */

import { useState, useEffect, useRef, useCallback } from "react";

// ── Types ─────────────────────────────────────────────────

interface ModelInfo {
  name: string;
  size?: string | null;
  modified_at?: string | null;
}

interface ModelSelectorProps {
  sessionId: number;
  currentModel: string | null;
  onModelChange: (model: string | null) => void;
}

// ── Component ─────────────────────────────────────────────

export function ModelSelector({
  sessionId,
  currentModel,
  onModelChange,
}: ModelSelectorProps) {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Fetch available models on mount
  const fetchModels = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/models");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.error) {
        setError(data.error);
        setModels([]);
      } else {
        setModels(data.models || []);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load models");
      setModels([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchModels();
  }, [fetchModels]);

  // Close dropdown on outside click
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const handleSelect = async (modelName: string) => {
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`/api/sessions/${sessionId}/model`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelName === "" ? null : modelName }),
      });
      if (!res.ok) throw new Error("Failed to update model");
      onModelChange(modelName === "" ? null : modelName);
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save model");
    } finally {
      setSaving(false);
    }
  };

  const displayName = currentModel || null;

  return (
    <div className="model-selector" ref={dropdownRef}>
      <button
        className="model-selector-trigger"
        onClick={() => { setOpen(!open); if (!open) fetchModels(); }}
        disabled={saving}
        title={
          displayName
            ? `Model: ${displayName}`
            : "Click to select a model"
        }
      >
        <span className="model-selector-icon">🤖</span>
        <span className="model-selector-label">
          {saving
            ? "Saving…"
            : displayName
              ? displayName
              : "Default model"}
        </span>
        <span className={`model-selector-arrow ${open ? "open" : ""}`}>▾</span>
      </button>

      {open && (
        <div className="model-selector-dropdown">
          <div className="model-selector-header">
            <span>Select Model</span>
            <button
              className="model-selector-refresh"
              onClick={fetchModels}
              disabled={loading}
              title="Refresh model list"
            >
              {loading ? "⟳" : "↻"}
            </button>
          </div>

          {error && (
            <div className="model-selector-error">{error}</div>
          )}

          <div className="model-selector-list">
            {/* Default option */}
            <button
              className={`model-selector-item ${!currentModel ? "active" : ""}`}
              onClick={() => handleSelect("")}
            >
              <span className="model-selector-item-name">Default (llama3.2:3b)</span>
              <span className="model-selector-item-desc">Use server default</span>
            </button>

            {loading && models.length === 0 && (
              <div className="model-selector-loading">Loading models…</div>
            )}

            {!loading && models.length === 0 && !error && (
              <div className="model-selector-empty">
                No models available
              </div>
            )}

            {models.map((model) => (
              <button
                key={model.name}
                className={`model-selector-item ${
                  currentModel === model.name ? "active" : ""
                }`}
                onClick={() => handleSelect(model.name)}
              >
                <span className="model-selector-item-name">{model.name}</span>
                {model.size && (
                  <span className="model-selector-item-size">{model.size}</span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default ModelSelector;
