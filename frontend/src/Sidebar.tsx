/**
 * Sidebar — Session list with CRUD, search bar, and health indicators.
 *
 * Extracted from App.tsx in Phase 5C.
 */

import { useState } from "react";
import type { Session, HealthStatus } from "./types";
import { API } from "./api";

// ── Helpers ───────────────────────────────────────────────

function formatDate(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 86_400_000) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// ── Types ─────────────────────────────────────────────────

interface SidebarProps {
  sessions: Session[];
  activeSessionId: number | null;
  loadingSessions: boolean;
  health: HealthStatus | null;
  onSelectSession: (id: number) => void;
  onCreateSession: () => void;
  onDeleteSession: (id: number) => void;
  onRenameSession: (id: number, title: string) => void;
}

// ── Component ─────────────────────────────────────────────

export function Sidebar({
  sessions,
  activeSessionId,
  loadingSessions,
  health,
  onSelectSession,
  onCreateSession,
  onDeleteSession,
  onRenameSession,
}: SidebarProps) {
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const healthOk = health
    ? Object.values(health).filter((v) => v === "ok").length
    : 0;

  const handleRenameStart = (session: Session) => {
    setRenamingId(session.id);
    setRenameValue(session.title);
  };

  const handleRenameSubmit = async (id: number) => {
    const title = renameValue.trim() || "Untitled";
    await onRenameSession(id, title);
    setRenamingId(null);
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <span className="sidebar-logo">🧠</span>
        <span className="sidebar-title">Research Sessions</span>
        <button className="new-session-btn" onClick={onCreateSession}>
          ✚ New
        </button>
      </div>

      <div className="sidebar-list">
        {loadingSessions ? (
          <div className="sidebar-empty">Loading…</div>
        ) : sessions.length === 0 ? (
          <div className="sidebar-empty">
            <div style={{ marginBottom: "0.5rem", fontSize: "1.2rem" }}>📂</div>
            No sessions yet
          </div>
        ) : (
          sessions.map((session) => (
            <div
              key={session.id}
              className={`sidebar-item ${session.id === activeSessionId ? "active" : ""}`}
              onClick={() => onSelectSession(session.id)}
            >
              <span className="sidebar-item-icon">💬</span>
              {renamingId === session.id ? (
                <input
                  className="rename-input"
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onBlur={() => handleRenameSubmit(session.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleRenameSubmit(session.id);
                    if (e.key === "Escape") setRenamingId(null);
                  }}
                  autoFocus
                  onClick={(e) => e.stopPropagation()}
                />
              ) : (
                <>
                  <span className="sidebar-item-title">{session.title}</span>
                  <span
                    style={{
                      fontSize: "0.65rem",
                      color: "var(--text-secondary)",
                      flexShrink: 0,
                    }}
                  >
                    {formatDate(session.updated_at)}
                  </span>
                  <div
                    className="sidebar-item-actions"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      className="sidebar-action-btn"
                      title="Rename"
                      onClick={() => handleRenameStart(session)}
                    >
                      ✎
                    </button>
                    <button
                      className="sidebar-action-btn delete"
                      title="Delete"
                      onClick={() => onDeleteSession(session.id)}
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

      <div className="sidebar-footer">
        <span
          className={`health-indicator ${health?.backend === "ok" ? "ok" : "unavailable"}`}
          title={`Backend: ${health?.backend ?? "?"}`}
        >
          <span className="health-dot" />
          API
        </span>
        <span
          className={`health-indicator ${health?.ollama === "ok" ? "ok" : "unavailable"}`}
          title={`Ollama: ${health?.ollama ?? "?"}`}
        >
          <span className="health-dot" />
          LLM
        </span>
        <span style={{ marginLeft: "auto", fontSize: "0.65rem", color: "var(--text-secondary)" }}>
          {healthOk === 3 ? "All OK" : `${healthOk}/3`}
        </span>
      </div>
    </aside>
  );
}

export default Sidebar;
