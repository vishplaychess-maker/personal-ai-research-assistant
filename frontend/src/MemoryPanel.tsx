/**
 * MemoryPanel — Long-Term Memory CRUD panel.
 *
 * Extracted from App.tsx in Phase 5C.
 */

import type { Memory } from "./types";

// ── Constants ─────────────────────────────────────────────

const CATEGORY_ICON: Record<string, string> = {
  fact: "💡",
  preference: "⭐",
  research_interest: "🔬",
  project_context: "📋",
};

const CATEGORY_LABEL: Record<string, string> = {
  fact: "Fact",
  preference: "Preference",
  research_interest: "Interest",
  project_context: "Context",
};

// ── Helpers ───────────────────────────────────────────────

function formatDate(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 86_400_000)
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// ── Types ─────────────────────────────────────────────────

interface MemoryPanelProps {
  memories: Memory[];
  memoryEnabled: boolean;
  memorySettingLoaded: boolean;
  memoryError: string | null;
  togglePending: boolean;
  addingMemory: boolean;
  newMemoryContent: string;
  newMemoryCategory: string;
  editingMemoryId: number | null;
  editMemoryContent: string;
  editMemoryCategory: string;
  showClearConfirm: boolean;
  onToggleMemory: () => void;
  onSetAddingMemory: (v: boolean) => void;
  onSetNewMemoryContent: (v: string) => void;
  onSetNewMemoryCategory: (v: string) => void;
  onAddMemory: () => void;
  onEditMemory: (id: number) => void;
  onSetEditingMemoryId: (id: number | null) => void;
  onSetEditMemoryContent: (v: string) => void;
  onSetEditMemoryCategory: (v: string) => void;
  onDeleteMemory: (id: number) => void;
  onClearAllMemories: () => void;
  onSetShowClearConfirm: (v: boolean) => void;
}

// ── Component ─────────────────────────────────────────────

export function MemoryPanel({
  memories,
  memoryEnabled,
  memorySettingLoaded,
  memoryError,
  togglePending,
  addingMemory,
  newMemoryContent,
  newMemoryCategory,
  editingMemoryId,
  editMemoryContent,
  editMemoryCategory,
  showClearConfirm,
  onToggleMemory,
  onSetAddingMemory,
  onSetNewMemoryContent,
  onSetNewMemoryCategory,
  onAddMemory,
  onEditMemory,
  onSetEditingMemoryId,
  onSetEditMemoryContent,
  onSetEditMemoryCategory,
  onDeleteMemory,
  onClearAllMemories,
  onSetShowClearConfirm,
}: MemoryPanelProps) {
  return (
    <div className="mem-panel">
      <div className="mem-panel-header">
        <span className="mem-panel-title">🧠 Long-Term Memory</span>
        <span className="mem-panel-subtitle">
          {memories.length} saved · Stored locally in SQLite
        </span>
        <span
          className={`mem-toggle-label ${
            memorySettingLoaded
              ? memoryEnabled
                ? "enabled"
                : "disabled"
              : "loading"
          }`}
        >
          {!memorySettingLoaded
            ? "⏳ Syncing…"
            : memoryEnabled
              ? "🟢 Memory enabled"
              : "🔴 Memory disabled"}
        </span>
      </div>

      {memoryError && <div className="mem-error">{memoryError}</div>}

      {addingMemory ? (
        <div className="mem-add-form">
          <select
            className="mem-category-select"
            value={newMemoryCategory}
            onChange={(e) => onSetNewMemoryCategory(e.target.value)}
          >
            <option value="fact">💡 Fact</option>
            <option value="preference">⭐ Preference</option>
            <option value="research_interest">🔬 Research Interest</option>
            <option value="project_context">📋 Project Context</option>
          </select>
          <input
            className="mem-add-input"
            placeholder="What should I remember?"
            value={newMemoryContent}
            onChange={(e) => onSetNewMemoryContent(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onAddMemory();
              }
              if (e.key === "Escape") {
                onSetAddingMemory(false);
                onSetNewMemoryContent("");
              }
            }}
            autoFocus
          />
          <div className="mem-add-actions">
            <button className="mem-save-btn" onClick={onAddMemory}>
              Save
            </button>
            <button
              className="mem-cancel-btn"
              onClick={() => {
                onSetAddingMemory(false);
                onSetNewMemoryContent("");
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="mem-add-row">
          <button
            className="mem-add-btn"
            onClick={() => onSetAddingMemory(true)}
          >
            ✚ Add Memory
          </button>
          {memories.length > 0 && (
            <>
              {showClearConfirm ? (
                <div className="mem-clear-confirm">
                  <span>Clear all memories?</span>
                  <button
                    className="mem-confirm-yes"
                    onClick={onClearAllMemories}
                  >
                    Yes
                  </button>
                  <button
                    className="mem-confirm-no"
                    onClick={() => onSetShowClearConfirm(false)}
                  >
                    No
                  </button>
                </div>
              ) : (
                <button
                  className="mem-clear-btn"
                  onClick={() => onSetShowClearConfirm(true)}
                >
                  🗑 Clear All
                </button>
              )}
            </>
          )}
        </div>
      )}

      <div className="mem-list">
        {memories.length === 0 ? (
          <div className="mem-empty">
            No saved memories yet. Memories are automatically saved when you
            share durable facts or preferences.
          </div>
        ) : (
          memories.map((mem) => (
            <div key={mem.id} className="mem-item">
              {editingMemoryId === mem.id ? (
                <div className="mem-edit-form">
                  <select
                    className="mem-category-select"
                    value={editMemoryCategory}
                    onChange={(e) => onSetEditMemoryCategory(e.target.value)}
                  >
                    <option value="fact">💡 Fact</option>
                    <option value="preference">⭐ Preference</option>
                    <option value="research_interest">🔬 Research Interest</option>
                    <option value="project_context">📋 Project Context</option>
                  </select>
                  <input
                    className="mem-edit-input"
                    value={editMemoryContent}
                    onChange={(e) => onSetEditMemoryContent(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        onEditMemory(mem.id);
                      }
                      if (e.key === "Escape") {
                        onSetEditingMemoryId(null);
                      }
                    }}
                    autoFocus
                  />
                  <div className="mem-edit-actions">
                    <button
                      className="mem-save-btn"
                      onClick={() => onEditMemory(mem.id)}
                    >
                      Save
                    </button>
                    <button
                      className="mem-cancel-btn"
                      onClick={() => onSetEditingMemoryId(null)}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="mem-icon">
                    {CATEGORY_ICON[mem.category] || "💡"}
                  </div>
                  <div className="mem-details">
                    <span className="mem-content">{mem.content}</span>
                    <span className="mem-meta">
                      <span
                        className={`mem-category-tag mem-cat-${mem.category}`}
                      >
                        {CATEGORY_LABEL[mem.category] || mem.category}
                      </span>
                      <span> · Saved {formatDate(mem.created_at)}</span>
                    </span>
                  </div>
                  <div className="mem-actions">
                    <button
                      className="mem-action-btn"
                      title="Edit"
                      onClick={() => {
                        onSetEditingMemoryId(mem.id);
                        onSetEditMemoryContent(mem.content);
                        onSetEditMemoryCategory(mem.category);
                      }}
                    >
                      ✎
                    </button>
                    <button
                      className="mem-action-btn delete"
                      title="Delete"
                      onClick={() => onDeleteMemory(mem.id)}
                    >
                      ✕
                    </button>
                  </div>
                </>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export default MemoryPanel;
