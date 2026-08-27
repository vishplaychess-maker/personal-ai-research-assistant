/**
 * ModelSelector — Dropdown for selecting the AI model for a session.
 *
 * Fetches available models from GET /api/models and allows the user
 * to choose which model to use for a specific session.
 * Dispatches PATCH /api/sessions/{id}/model to persist the selection.
 */

import { useState, useEffect, useRef, useCallback } from "react";
import { API } from "./api";
import type { ModelInfo } from "./types";

// ── Types ─────────────────────────────────────────────────

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
      const data = await API.listChatModels();
      if (data.error) {
        setError(data.error);
        setModels([]);
      } else {
        // Exclude embedding-only models (e.g. nomic-embed-text) so they can
        // never be chosen for chat generation.
        const chatModels = (data.models || []).filter(
          (m: ModelInfo) => !/embed/i.test(m.name)
        );
        setModels(chatModels);
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
      // api.ts attaches Authorization + CSRF headers and handles 401 refresh.
      await API.updateSessionModel(sessionId, modelName === "" ? null : modelName);
      onModelChange(modelName === "" ? null : modelName);
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save model");
    } finally {
      setSaving(false);
    }
  };

  const displayName = currentModel || null;

  // Show a warning when the saved model is no longer available, without
  // silently resetting the saved setting.
  const selectedUnavailable =
    !!currentModel && !loading && models.length > 0 &&
    !models.some((m) => m.name === currentModel);

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

      {selectedUnavailable && (
        <div className="model-selector-unavailable">
          ⚠️ Selected model is unavailable
        </div>
      )}

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
              <span className="model-selector-item-name">Default model</span>
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
