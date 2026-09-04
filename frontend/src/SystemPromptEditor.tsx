/**
 * SystemPromptEditor — Edit the per-session system prompt.
 *
 * Displays a modal/panel where the user can view and edit the system
 * prompt for the current session. The prompt is saved to the backend
 * via PATCH /api/sessions/{id}/system-prompt.
 */

import { useState, useCallback, useEffect } from "react";
import { getCsrfToken, getAuthHeadersAsync } from "./auth";
import { apiUrl } from "./apiBase";

// ── Types ─────────────────────────────────────────────────

interface SystemPromptEditorProps {
  sessionId: number;
  onClose: () => void;
}

// ── Constants ─────────────────────────────────────────────

const MAX_PROMPT_LENGTH = 2000;

// ── Component ─────────────────────────────────────────────

export function SystemPromptEditor({
  sessionId,
  onClose,
}: SystemPromptEditorProps) {
  const [prompt, setPrompt] = useState("");
  const [originalPrompt, setOriginalPrompt] = useState("");
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [usingDefault, setUsingDefault] = useState(true);

  // Load current system prompt
  const loadPrompt = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const auth = await getAuthHeadersAsync();
      const res = await fetch(apiUrl(`/api/sessions/${sessionId}/system-prompt`), {
        headers: auth.Authorization ? { Authorization: auth.Authorization } : {},
        credentials: "include",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setPrompt(data.system_prompt || "");
      setOriginalPrompt(data.system_prompt || "");
      setUsingDefault(data.using_default ?? true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load system prompt");
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    loadPrompt();
  }, [loadPrompt]);

  // Auto-resize textarea
  const textareaRefCallback = useCallback((el: HTMLTextAreaElement | null) => {
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 200) + "px";
    }
  }, []);

  const handleSave = async () => {
    const trimmed = prompt.trim();
    if (trimmed.length > MAX_PROMPT_LENGTH) {
      setError(`Prompt exceeds ${MAX_PROMPT_LENGTH} characters`);
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      // Phase 7C: attach the access token so the backend can identify the
      // session owner.
      const auth = await getAuthHeadersAsync();
      if (auth.Authorization) headers.Authorization = auth.Authorization;
      // Phase 7C: state-changing PATCH requires the double-submit CSRF token.
      const csrf = getCsrfToken();
      if (csrf) headers["X-CSRF-Token"] = csrf;

      const res = await fetch(apiUrl(`/api/sessions/${sessionId}/system-prompt`), {
        method: "PATCH",
        headers,
        credentials: "include",
        body: JSON.stringify({
          system_prompt: trimmed || null,
        }),
      });
      if (!res.ok) throw new Error("Failed to save system prompt");
      const data = await res.json();
      setOriginalPrompt(trimmed);
      setUsingDefault(data.using_default ?? false);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    setSaving(true);
    setError(null);
    try {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      // Phase 7C: attach the access token so the backend can identify the
      // session owner.
      const auth = await getAuthHeadersAsync();
      if (auth.Authorization) headers.Authorization = auth.Authorization;
      // Phase 7C: state-changing PATCH requires the double-submit CSRF token.
      const csrf = getCsrfToken();
      if (csrf) headers["X-CSRF-Token"] = csrf;

      const res = await fetch(apiUrl(`/api/sessions/${sessionId}/system-prompt`), {
        method: "PATCH",
        headers,
        credentials: "include",
        body: JSON.stringify({ system_prompt: null }),
      });
      if (!res.ok) throw new Error("Failed to reset system prompt");
      const data = await res.json();
      setPrompt(data.system_prompt || "");
      setOriginalPrompt(data.system_prompt || "");
      setUsingDefault(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reset");
    } finally {
      setSaving(false);
    }
  };

  const hasChanges = prompt !== originalPrompt;

  if (loading) {
    return (
      <div className="sp-editor-overlay" onClick={onClose}>
        <div className="sp-editor" onClick={(e) => e.stopPropagation()}>
          <div className="sp-editor-loading">Loading system prompt…</div>
        </div>
      </div>
    );
  }

  return (
    <div className="sp-editor-overlay" onClick={onClose}>
      <div className="sp-editor" onClick={(e) => e.stopPropagation()}>
        <div className="sp-editor-header">
          <span className="sp-editor-title">⚙ System Prompt</span>
          <span className={`sp-editor-badge ${usingDefault ? "default" : "custom"}`}>
            {usingDefault ? "Default" : "Custom"}
          </span>
          <button className="sp-editor-close" onClick={onClose}>✕</button>
        </div>

        <div className="sp-editor-body">
          <label className="sp-editor-label">
            Instructions for the AI assistant in this session:
          </label>

          <textarea
            ref={textareaRefCallback}
            className="sp-editor-textarea"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Enter custom system prompt instructions…"
            maxLength={MAX_PROMPT_LENGTH}
            rows={6}
            autoFocus
          />

          <div className="sp-editor-char-count">
            {prompt.length}/{MAX_PROMPT_LENGTH}
          </div>

          {error && <div className="sp-editor-error">{error}</div>}

          <div className="sp-editor-hint">
            The system prompt sets the behavior and personality of the AI assistant.
            It is sent with every message in this session.
          </div>
        </div>

        <div className="sp-editor-footer">
          <button
            className="sp-editor-reset-btn"
            onClick={handleReset}
            disabled={saving || usingDefault}
            title="Reset to default system prompt"
          >
            Reset to Default
          </button>
          <div className="sp-editor-footer-right">
            <button
              className="sp-editor-cancel-btn"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              className="sp-editor-save-btn"
              onClick={handleSave}
              disabled={saving || (!hasChanges && !usingDefault)}
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default SystemPromptEditor;
